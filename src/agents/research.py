"""Agent 3 — Research: an orchestrator-worker system inside one agent.

lead plan → [human approves] → round loop:
    4 parallel category workers (no-code / low-code / full-code / SaaS):
        structured queries (capability × angle) → free-source search → read FULL pages
        → extract findings (every one cited) + tool options
    lead synthesis: similarity (matched/missing), costs (method + assumptions), scores
    coverage check → loop again (targeted at gaps) or exit
citation verification (reachability; demote/drop unsupported) → HTML + PPT reports

All knobs in config/research.yaml (RESEARCH_AGENT_SPEC.md §2). Hard caps on rounds,
workers, tool calls, wall clock, and cost — a breach checkpoints and pauses.

Standalone: python -m src.agents.research --input runs/<id>/casefile.json --budget 4h
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

from src.agents._common import (build_parser, checkpoint_and_exit, context_from_args,
                                gate, load_or_new, save_and_report)
from src.state.casefile import (CaseFile, CostEstimate, Finding, ResearchPlan,
                                SimilarityResult, Source, ToolOption)
from src.tools.costs import BudgetExceeded, Deadline
from src.tools.models import (CONFIG_DIR, RunContext, llm_json, load_prompt, load_yaml)
from src.tools.reports import render_html, render_ppt
from src.tools.search import check_url, is_denied, rate_reliability
from src.server.router import RouterSession
from src.server.sources import TIER_TO_RELIABILITY, list_sources, multi_search

PAGE_CHARS_PER_EXTRACTION = 6000
PAGES_PER_EXTRACTION_CALL = 4


def research_config() -> dict:
    return load_yaml(str(CONFIG_DIR / "research.yaml"))


# ── 1. lead plans, human approves ────────────────────────────────────────────

def _make_plan(case: CaseFile, ctx: RunContext, cfg: dict) -> ResearchPlan:
    system = load_prompt("research_lead")
    payload = json.dumps({
        "problem_statement": case.problem_statement,
        "future_workflow": [s.model_dump() for s in case.future_workflow],
        "data_inventory": [d.model_dump() for d in case.data_inventory],
    }, indent=2)
    data = llm_json(prompt=f"Plan the research for:\n{payload}", role="lead",
                    system=system, ctx=ctx, purpose="research plan")
    return ResearchPlan(
        target_profile=data.get("target_profile", ""),
        capabilities=data.get("capabilities", []),
        questions=data.get("questions", []),
        categories=list((cfg.get("categories") or {}).keys()) or data.get("categories", []),
        source_criteria=data.get("source_criteria", []),
    )


def _print_plan(plan: ResearchPlan) -> None:
    print("\n──── research plan (needs your approval) ────")
    print(f"Target profile: {plan.target_profile}")
    print("Capabilities to evaluate:")
    for c in plan.capabilities:
        print(f"  - {c}")
    print("Research questions:")
    for q in plan.questions:
        print(f"  - {q}")
    print(f"Categories: {', '.join(plan.categories)}")
    print("Source criteria:")
    for s in plan.source_criteria:
        print(f"  - {s}")


# ── 2–3. category worker: queries → search → read full docs → extract ───────

def _generate_queries(category: str, description: str, plan: ResearchPlan,
                      cfg: dict, focus: List[str], ctx: RunContext) -> List[str]:
    q = cfg.get("queries") or {}
    prompt = json.dumps({
        "task": "generate web search queries",
        "category": f"{category}: {description}",
        "target_profile": plan.target_profile,
        "capabilities": plan.capabilities,
        "angles": q.get("angles", []),
        "focus_gaps_from_last_round": focus,
        "max_queries": q.get("max_queries_per_round", 12),
    }, indent=2)
    data = llm_json(
        prompt=prompt, role="worker", ctx=ctx, purpose=f"queries:{category}",
        system=("Cross each capability with each angle and emit concrete web search queries "
                "for THIS category only. Prioritize official documentation and pricing pages. "
                'If focus gaps are given, target those first. Output strict JSON: {"queries": ["..."]}'))
    queries = [str(x) for x in (data.get("queries") or [])]
    return queries[: q.get("max_queries_per_round", 12)]


def _custom_source_ids(ctx: RunContext) -> List[str]:
    """User-registered custom sources (Settings → Research Sources) join every worker."""
    try:
        return [s["id"] for s in list_sources()
                if not s.get("builtin") and s.get("enabled")
                and (ctx.sources is None or s["id"] in ctx.sources)]
    except Exception:
        return []


def _gather_pages(category: str, queries: List[str], cfg: dict, budget_left: List[int],
                  ctx: RunContext) -> List[dict]:
    """Search via the router (curated per-worker chains, quota-guarded, cached),
    then read full pages via the extractor chain (Jina → Firecrawl → builtin)."""
    router: RouterSession = ctx.router
    q = cfg.get("queries") or {}
    sources_cfg = cfg.get("sources") or {}
    custom_ids = _custom_source_ids(ctx)
    hits: Dict[str, dict] = {}
    for query in queries:
        if budget_left[0] <= 0:
            break
        budget_left[0] -= 1  # a search is a tool call
        found = router.search(category, query, n=q.get("results_per_query", 6))
        if custom_ids:  # user-registered APIs ride along for every worker
            found += multi_search(query, n_per_source=3, only=custom_ids, events=ctx.events)
        for h in found:
            url = h["url"]
            if url not in hits and not is_denied(url, sources_cfg):
                # tier floor from the source registry; official-docs URLs still rank high
                by_url = rate_reliability(url, sources_cfg)
                by_tier = TIER_TO_RELIABILITY.get(h.get("tier", ""), "low")
                order = {"high": 0, "medium": 1, "low": 2}
                h["reliability"] = by_url if order[by_url] <= order[by_tier] else by_tier
                hits[url] = h
    ranked = sorted(hits.values(),
                    key=lambda h: {"high": 0, "medium": 1, "low": 2}[h["reliability"]])
    pages = []
    for h in ranked:
        if budget_left[0] <= 0:
            break
        budget_left[0] -= 1  # a read is a tool call
        text = router.read(category, h["url"])
        if text and category == "saas" and any(k in h["url"].lower() for k in ("pricing", "plans")):
            structured = router.extract_structured(h["url"], "extract the pricing table and tiers")
            if structured:
                text = f"{text}\n\nSTRUCTURED PRICING EXTRACTION:\n{structured}"
        if text:
            pages.append({"url": h["url"], "title": h["title"],
                          "reliability": h["reliability"], "tier": h.get("tier", ""),
                          "text": text})
    return pages


def _uploaded_docs(ctx: RunContext) -> List[dict]:
    """User-staged internal documents become high-reliability internal:// pages."""
    updir = ctx.run_dir / "uploads"
    pages = []
    if updir.exists():
        for f in sorted(updir.glob("*.extracted.txt")):
            name = f.name.replace(".extracted.txt", "")
            text = f.read_text(encoding="utf-8", errors="replace")
            ctx.emit("doc_read", agent="research", url=f"internal://{name}", chars=len(text))
            pages.append({"url": f"internal://{name}", "title": name,
                          "reliability": "high", "text": text})
    return pages


def _extract(category: str, plan: ResearchPlan, pages: List[dict],
             ctx: RunContext) -> dict:
    """Run the worker extraction prompt over page batches; merge results."""
    system = load_prompt("research_worker")
    merged = {"options": [], "findings": [], "open_questions": []}
    for i in range(0, len(pages), PAGES_PER_EXTRACTION_CALL):
        batch = pages[i:i + PAGES_PER_EXTRACTION_CALL]
        body = json.dumps({
            "category": category,
            "target_profile": plan.target_profile,
            "capabilities": plan.capabilities,
            "pages": [{"url": p["url"], "title": p["title"],
                       "text": p["text"][:PAGE_CHARS_PER_EXTRACTION]} for p in batch],
        }, indent=2)
        data = llm_json(prompt=body, role="worker", system=system, ctx=ctx,
                        purpose=f"extract:{category}")
        for key in merged:
            merged[key].extend(data.get(key) or [])
    return merged


def _worker_round(category: str, description: str, plan: ResearchPlan, cfg: dict,
                  focus: List[str], ctx: RunContext, round_no: int) -> Tuple[str, dict]:
    ctx.emit("worker_start", agent="research", worker=category, round=round_no)
    budget_left = [int((cfg.get("budget") or {}).get("max_tool_calls_per_worker", 40))]
    queries = _generate_queries(category, description, plan, cfg, focus, ctx)
    pages = _gather_pages(category, queries, cfg, budget_left, ctx)
    if round_no == 1:
        pages = _uploaded_docs(ctx) + pages  # staged internal docs feed round 1
    print(f"  [worker:{category}] {len(queries)} queries → {len(pages)} pages read "
          f"({budget_left[0]} tool calls left)")
    if not pages:
        ctx.emit("worker_end", agent="research", worker=category, round=round_no,
                 status="error", error=f"no reachable sources found for {category}")
        return category, {"options": [], "findings": [],
                          "open_questions": [f"no reachable sources found for {category}"]}
    result = _extract(category, plan, pages, ctx)
    ctx.emit("worker_end", agent="research", worker=category, round=round_no,
             options=len(result.get("options", [])), findings=len(result.get("findings", [])))
    return category, result


# ── merge worker output into the casefile (main thread only) ────────────────

def _merge(case: CaseFile, category: str, result: dict, cfg: dict,
           ctx: RunContext) -> None:
    sources_cfg = cfg.get("sources") or {}
    existing = {o.name.lower(): o for o in case.tool_landscape.get(category, [])}
    for raw in result.get("options", []):
        name = str(raw.get("name", "")).strip()
        if not name:
            continue
        if name.lower() in existing:
            opt = existing[name.lower()]
            extra = raw.get("capability_notes", "")
            if extra and extra not in opt.capability_notes:
                opt.capability_notes = (opt.capability_notes + " | " + extra).strip(" |")
        else:
            opt = ToolOption(name=name, category=category,
                             vendor=raw.get("vendor", ""), url=raw.get("url", ""),
                             summary=raw.get("summary", ""),
                             capability_notes=raw.get("capability_notes", ""))
            case.tool_landscape.setdefault(category, []).append(opt)
            existing[name.lower()] = opt

    seen = {(f.claim.lower(), f.source.url) for f in case.findings}
    for raw in result.get("findings", []):
        src = raw.get("source") or {}
        url = str(src.get("url", "")).strip()
        claim = str(raw.get("claim", "")).strip()
        if not url or not claim or (claim.lower(), url) in seen:
            continue  # no claim without a source — unsourced output is discarded
        try:
            finding = Finding(
                claim=claim,
                kind=raw.get("kind", "fact") if raw.get("kind") in ("fact", "estimate") else "estimate",
                category=category,
                vendor_claim=bool(raw.get("vendor_claim", False)),
                confidence=float(raw.get("confidence", 0.7)),
                option=raw.get("option"),
                source=Source(url=url, title=src.get("title", ""),
                              publisher=src.get("publisher", ""),
                              source_type=src.get("source_type", "community"),
                              reliability=rate_reliability(url, sources_cfg)),
            )
        except ValueError:
            continue
        case.add_finding(finding)
        ctx.emit("finding_created", agent="research", finding_id=finding.id,
                 claim=finding.claim[:300], kind=finding.kind, category=category, url=url)
        seen.add((claim.lower(), url))

    for q in result.get("open_questions", []):
        if q and q not in case.open_questions:
            case.open_questions.append(q)


# ── 4–7. lead synthesis: similarity, costs, scores, coverage gaps ────────────

def _synthesize(case: CaseFile, plan: ResearchPlan, cfg: dict,
                ctx: RunContext) -> List[str]:
    system = load_prompt("research_synthesis")
    payload = json.dumps({
        "target_profile": plan.target_profile,
        "capabilities": plan.capabilities,
        "questions": plan.questions,
        "existing_solution_threshold": (cfg.get("similarity") or {}).get("existing_solution_threshold", 75),
        "hourly_rate_usd": (cfg.get("cost") or {}).get("hourly_rate_usd", 120),
        "options": {cat: [{"name": o.name, "vendor": o.vendor, "url": o.url,
                           "summary": o.summary, "capability_notes": o.capability_notes}
                          for o in opts]
                    for cat, opts in case.tool_landscape.items()},
        "findings": [{"id": f.id, "claim": f.claim, "kind": f.kind, "option": f.option,
                      "category": f.category, "vendor_claim": f.vendor_claim,
                      "url": f.source.url} for f in case.findings],
    }, indent=2)
    data = llm_json(prompt=payload, role="lead", system=system, ctx=ctx,
                    purpose="synthesis: similarity + costs + scores")

    by_key = {(o.category, o.name.lower()): o
              for opts in case.tool_landscape.values() for o in opts}
    valid_ids = {f.id for f in case.findings}
    for raw in data.get("options", []):
        opt = by_key.get((raw.get("category", ""), str(raw.get("name", "")).lower()))
        if opt is None:
            continue
        sim = raw.get("similarity") or {}
        opt.similarity = SimilarityResult(
            index=max(0, min(100, int(sim.get("index", 0)))),
            matched=[str(m) for m in sim.get("matched", [])],
            missing=[str(m) for m in sim.get("missing", [])],
            existing_solution=bool(sim.get("existing_solution", False)),
            existing_solution_url=sim.get("existing_solution_url"),
        )
        costs = raw.get("costs") or {}
        opt.costs = CostEstimate(
            build_cost_usd_low=float(costs.get("build_cost_usd_low", 0) or 0),
            build_cost_usd_high=float(costs.get("build_cost_usd_high", 0) or 0),
            per_run_cost_usd=float(costs.get("per_run_cost_usd", 0) or 0),
            monthly_operation_usd=float(costs.get("monthly_operation_usd", 0) or 0),
            method=costs.get("method", ""),
            assumptions=[str(a) for a in costs.get("assumptions", [])],
        )
        opt.scores = {k: float(v) for k, v in (raw.get("scores") or {}).items()}
        opt.finding_ids = [fid for fid in raw.get("finding_ids", []) if fid in valid_ids]

    for q in data.get("open_questions", []):
        if q and q not in case.open_questions:
            case.open_questions.append(q)
    return [str(g) for g in data.get("coverage_gaps", [])]


# ── 8. coverage check ────────────────────────────────────────────────────────

def _coverage_met(case: CaseFile, cfg: dict) -> bool:
    cov = cfg.get("coverage") or {}
    categories = list((cfg.get("categories") or {}).keys())
    per_option_counts: Dict[str, int] = {}
    for f in case.findings:
        if f.option:
            per_option_counts[f.option.lower()] = per_option_counts.get(f.option.lower(), 0) + 1
    for cat in categories:
        options = case.tool_landscape.get(cat, [])
        if len(options) < int(cov.get("min_options_per_category", 3)):
            return False
        for o in options:
            if per_option_counts.get(o.name.lower(), 0) < int(cov.get("min_findings_per_option", 3)):
                return False
            if cov.get("require_similarity_on_all", True) and not (o.similarity.matched or o.similarity.missing):
                return False
            if cov.get("require_costs_on_all", True) and not o.costs.method:
                return False
    return True


# ── 9. citation verification ────────────────────────────────────────────────

def _verify_citations(case: CaseFile, ctx: RunContext) -> None:
    print(f"[verify] checking {len(case.findings)} citations for reachability…")
    from src.server.router import Cache
    cache = Cache()

    def _check(f: Finding) -> bool:
        if f.source.url.startswith("internal://"):
            return True  # user-staged internal doc — reachable by construction
        if cache.get_page(f.source.url):
            return True  # we fetched and cached it this run — reachable by construction
        return check_url(f.source.url)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = dict(zip([f.id for f in case.findings],
                           pool.map(_check, case.findings)))
    cited_ids = {fid for opts in case.tool_landscape.values()
                 for o in opts for fid in o.finding_ids}
    kept: List[Finding] = []
    dropped = demoted = 0
    for f in case.findings:
        if results.get(f.id, False):
            f.source.verified = True
            ctx.emit("citation_verified", agent="research", finding_id=f.id, url=f.source.url)
            kept.append(f)
        elif f.id in cited_ids or f.option:
            f.source.verified = False
            f.kind = "assumption"          # demote: unreachable evidence is not a fact
            f.confidence = min(f.confidence, 0.3)
            demoted += 1
            ctx.emit("citation_rejected", agent="research", finding_id=f.id, url=f.source.url,
                     action="demoted to assumption", reason="source unreachable")
            kept.append(f)
        else:
            dropped += 1                   # nothing cites it — it does not survive
            ctx.emit("citation_rejected", agent="research", finding_id=f.id, url=f.source.url,
                     action="dropped", reason="source unreachable and nothing cites it")
    case.findings = kept
    valid = {f.id for f in case.findings}
    for opts in case.tool_landscape.values():
        for o in opts:
            o.finding_ids = [fid for fid in o.finding_ids if fid in valid]
    print(f"[verify] verified {len(kept) - demoted}, demoted {demoted}, dropped {dropped}")

    _claim_support_pass(case, ctx, cache)
    _sole_support_flags(case, ctx)


def _claim_support_pass(case: CaseFile, ctx: RunContext, cache) -> None:
    """Re-read each cited page (from cache — zero quota) and confirm it actually
    supports the claim. Unsupported → demoted to assumption + logged as rejected."""
    checkable = [(f, cache.get_page(f.source.url)) for f in case.findings
                 if f.source.verified and not f.source.url.startswith("internal://")]
    checkable = [(f, text) for f, text in checkable if text]
    if not checkable:
        return
    print(f"[verify] claim-support check on {len(checkable)} cached citations…")
    for i in range(0, len(checkable), 8):
        batch = checkable[i:i + 8]
        payload = json.dumps([{"id": f.id, "claim": f.claim,
                               "page_excerpt": text[:2500]} for f, text in batch])
        try:
            data = llm_json(
                prompt=payload, role="classify", ctx=ctx, purpose="citation claim-support",
                system=("For each item decide if the page excerpt actually supports the claim. "
                        "Paraphrase counts; absence or contradiction does not. Output strict "
                        'JSON: {"verdicts": [{"id": "F-1", "supported": true}]}'))
        except Exception as e:  # noqa: BLE001 — verification must not kill the run
            ctx.emit("error", agent="research", error=f"claim-support batch failed: {e}",
                     recovered=True, impact="these findings keep reachability-only verification")
            continue
        verdicts = {v.get("id"): bool(v.get("supported")) for v in data.get("verdicts", [])}
        for f, _text in batch:
            if verdicts.get(f.id) is False:
                f.kind = "assumption"
                f.confidence = min(f.confidence, 0.2)
                f.source.verified = False
                q = f"Rejected by citation check: source did not support “{f.claim[:120]}”"
                if q not in case.open_questions:
                    case.open_questions.append(q)
                ctx.emit("citation_rejected", agent="research", finding_id=f.id,
                         url=f.source.url, action="demoted to assumption",
                         reason="cached page does not support the claim")


def _sole_support_flags(case: CaseFile, ctx: RunContext) -> None:
    """No single vendor page may be the sole support for an option; community
    sources (weight 0.4) can never be the sole basis for a recommendation."""
    by_option: Dict[str, List[Finding]] = {}
    for f in case.findings:
        if f.option:
            by_option.setdefault(f.option.lower(), []).append(f)
    for opts in case.tool_landscape.values():
        for o in opts:
            fs = by_option.get(o.name.lower(), []) + \
                 [f for f in case.findings if f.id in o.finding_ids]
            fs = list({f.id: f for f in fs}.values())
            if not fs:
                continue
            if all(f.vendor_claim for f in fs):
                o.vendor_only = True
                ctx.emit("citation_rejected", agent="research", finding_id="",
                         url=o.url, action=f"option '{o.name}' flagged vendor-only",
                         reason="every supporting source is the vendor — needs one independent source")
            if all(f.source.source_type == "community" for f in fs):
                o.community_only = True
                ctx.emit("citation_rejected", agent="research", finding_id="",
                         url=o.url, action=f"option '{o.name}' flagged community-only",
                         reason="anecdote-only evidence (weight 0.4) cannot be the sole basis")


# ── the loop ─────────────────────────────────────────────────────────────────

def run(case: CaseFile, ctx: RunContext, budget: Optional[str] = None) -> CaseFile:
    if not case.future_workflow:
        raise SystemExit("research needs a validated future workflow — run mapping first")
    if not case.map_validated_by_human:
        if ctx.interactive:
            raise SystemExit("the workflow map was never validated by a human "
                             "(map_validated_by_human=false) — Agent 3 will not start")
        print("[research] warning: unattended run on an UNVALIDATED map")

    cfg = research_config()
    bcfg = cfg.get("budget") or {}
    deadline = Deadline(budget or bcfg.get("max_wall_clock", "4h"))
    categories: Dict[str, str] = cfg.get("categories") or {}

    # plan (reused when resuming a paused run)
    if case.research_plan is None or not case.research_plan.approved_by_human:
        case.research_plan = _make_plan(case, ctx, cfg)
        _print_plan(case.research_plan)
        if gate("Approve this research plan? Workers only run after approval.",
                ctx, "approve_plan"):
            case.research_plan.approved_by_human = True
        else:
            case.status = "awaiting_gate:approve_plan"
            case.next_agent = "research"
            ctx.emit("gate_waiting", agent="research", gate="approve_plan",
                     needs_approval={"target_profile": case.research_plan.target_profile,
                                     "capabilities": case.research_plan.capabilities,
                                     "questions": case.research_plan.questions})
            print("[research] plan NOT approved — no worker will spend a tool call.")
            return case
    plan = case.research_plan

    max_rounds = int(bcfg.get("max_rounds", 6))
    min_rounds = int(bcfg.get("min_rounds", 2))
    max_workers = int(bcfg.get("max_workers", 4))
    gaps: List[str] = []

    # one router per run: quota pre-flight, cache, dedup, curated per-worker chains.
    # Reserves this run's estimated source calls so parallel runs can't jointly
    # blow a free tier; released in the finally below.
    ctx.router = RouterSession(ctx, research_cfg=cfg)

    try:
        _research_loop(case, ctx, cfg, plan, deadline, gaps,
                       max_rounds=max_rounds, min_rounds=min_rounds,
                       max_workers=max_workers, categories=categories)
    finally:
        ctx.router.release()

    _verify_citations(case, ctx)

    out = cfg.get("outputs") or {}
    reports_dir = ctx.run_dir
    html = render_html(case, reports_dir / out.get("html", "reports/detailed_analysis.html"))
    print(f"[reports] {html}")
    ppt = render_ppt(case, reports_dir / out.get("ppt", "reports/overview.pptx"))
    if ppt:
        print(f"[reports] {ppt}")

    case.status = "in_progress"
    case.next_agent = "suitability"
    return case


def _research_loop(case: CaseFile, ctx: RunContext, cfg: dict, plan: ResearchPlan,
                   deadline: Deadline, gaps: List[str], *, max_rounds: int,
                   min_rounds: int, max_workers: int, categories: Dict[str, str]) -> None:
    while case.research_rounds_done < max_rounds:
        deadline.check()
        rnd = case.research_rounds_done + 1
        print(f"\n[research] round {rnd}/{max_rounds} "
              f"(wall clock left: {deadline.remaining():.0f}s, "
              f"spend: ${ctx.tracker.spent_usd:.2f})")

        budget_err: Optional[BudgetExceeded] = None
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_worker_round, cat, desc, plan, cfg, gaps, ctx, rnd): cat
                       for cat, desc in categories.items()}
            for fut in as_completed(futures):
                try:
                    category, result = fut.result()
                    _merge(case, category, result, cfg, ctx)
                except BudgetExceeded as e:
                    budget_err = e
        if budget_err:
            case.save(ctx.run_dir)  # keep what the finished workers found
            raise budget_err

        gaps = _synthesize(case, plan, cfg, ctx)
        case.research_rounds_done = rnd
        case.save(ctx.run_dir)  # checkpoint every round
        ctx.emit("checkpoint_saved", agent="research", round=rnd)

        total_findings = len(case.findings)
        total_options = sum(len(v) for v in case.tool_landscape.values())
        ctx.emit("round_complete", agent="research", round=rnd, of=max_rounds,
                 options=total_options, findings=total_findings, gaps=len(gaps),
                 spent_usd=round(ctx.tracker.spent_usd, 4),
                 wall_clock_left_s=int(deadline.remaining()),
                 wall_clock_budget_s=int(deadline.seconds))
        print(f"[research] round {rnd} done: {total_options} options, "
              f"{total_findings} findings, {len(gaps)} coverage gaps")

        if rnd >= min_rounds and _coverage_met(case, cfg) and not gaps:
            print("[research] coverage met — exiting the loop")
            break
        if deadline.expired():
            print("[research] wall clock exhausted — exiting with what we have")
            break


def main() -> None:
    parser = build_parser("Agent 3 — Research (deep, multi-round, cited tool landscape)")
    parser.add_argument("--budget", help="wall-clock budget, e.g. 4h / 90m (default from research.yaml)")
    args = parser.parse_args()
    if not args.input:
        parser.error("--input runs/<id>/casefile.json is required (research builds on the validated map)")
    case = load_or_new(args)
    ctx = context_from_args(args, case)
    try:
        case = run(case, ctx, budget=args.budget)
    except BudgetExceeded as e:
        checkpoint_and_exit(case, ctx, e, next_agent="research")
    save_and_report(case, ctx)


if __name__ == "__main__":
    main()
