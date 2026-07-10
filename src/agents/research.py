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
from src.tools.search import (check_url, fetch_page, is_denied, rate_reliability,
                              web_search)

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
                    system=system, ctx=ctx)
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
        prompt=prompt, role="worker", ctx=ctx,
        system=("Cross each capability with each angle and emit concrete web search queries "
                "for THIS category only. Prioritize official documentation and pricing pages. "
                'If focus gaps are given, target those first. Output strict JSON: {"queries": ["..."]}'))
    queries = [str(x) for x in (data.get("queries") or [])]
    return queries[: q.get("max_queries_per_round", 12)]


def _gather_pages(queries: List[str], cfg: dict, budget_left: List[int]) -> List[dict]:
    """Search then fetch full pages, best-first by reliability tier."""
    q = cfg.get("queries") or {}
    sources_cfg = cfg.get("sources") or {}
    hits: Dict[str, dict] = {}
    for query in queries:
        if budget_left[0] <= 0:
            break
        budget_left[0] -= 1  # a search is a tool call
        for h in web_search(query, max_results=q.get("results_per_query", 6)):
            url = h["url"]
            if url not in hits and not is_denied(url, sources_cfg):
                h["reliability"] = rate_reliability(url, sources_cfg)
                hits[url] = h
    ranked = sorted(hits.values(),
                    key=lambda h: {"high": 0, "medium": 1, "low": 2}[h["reliability"]])
    pages = []
    for h in ranked:
        if budget_left[0] <= 0:
            break
        budget_left[0] -= 1  # a fetch is a tool call
        text = fetch_page(h["url"])
        if text:
            pages.append({"url": h["url"], "title": h["title"],
                          "reliability": h["reliability"], "text": text})
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
        data = llm_json(prompt=body, role="worker", system=system, ctx=ctx)
        for key in merged:
            merged[key].extend(data.get(key) or [])
    return merged


def _worker_round(category: str, description: str, plan: ResearchPlan, cfg: dict,
                  focus: List[str], ctx: RunContext) -> Tuple[str, dict]:
    budget_left = [int((cfg.get("budget") or {}).get("max_tool_calls_per_worker", 40))]
    queries = _generate_queries(category, description, plan, cfg, focus, ctx)
    pages = _gather_pages(queries, cfg, budget_left)
    print(f"  [worker:{category}] {len(queries)} queries → {len(pages)} pages read "
          f"({budget_left[0]} tool calls left)")
    if not pages:
        return category, {"options": [], "findings": [],
                          "open_questions": [f"no reachable sources found for {category}"]}
    return category, _extract(category, plan, pages, ctx)


# ── merge worker output into the casefile (main thread only) ────────────────

def _merge(case: CaseFile, category: str, result: dict, cfg: dict) -> None:
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
    data = llm_json(prompt=payload, role="lead", system=system, ctx=ctx)

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

def _verify_citations(case: CaseFile) -> None:
    print(f"[verify] checking {len(case.findings)} citations for reachability…")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = dict(zip([f.id for f in case.findings],
                           pool.map(lambda f: check_url(f.source.url), case.findings)))
    cited_ids = {fid for opts in case.tool_landscape.values()
                 for o in opts for fid in o.finding_ids}
    kept: List[Finding] = []
    dropped = demoted = 0
    for f in case.findings:
        if results.get(f.id, False):
            f.source.verified = True
            kept.append(f)
        elif f.id in cited_ids or f.option:
            f.source.verified = False
            f.kind = "assumption"          # demote: unreachable evidence is not a fact
            f.confidence = min(f.confidence, 0.3)
            demoted += 1
            kept.append(f)
        else:
            dropped += 1                   # nothing cites it — it does not survive
    case.findings = kept
    valid = {f.id for f in case.findings}
    for opts in case.tool_landscape.values():
        for o in opts:
            o.finding_ids = [fid for fid in o.finding_ids if fid in valid]
    print(f"[verify] verified {len(kept) - demoted}, demoted {demoted}, dropped {dropped}")


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
            print("[research] plan NOT approved — no worker will spend a tool call.")
            return case
    plan = case.research_plan

    max_rounds = int(bcfg.get("max_rounds", 6))
    min_rounds = int(bcfg.get("min_rounds", 2))
    max_workers = int(bcfg.get("max_workers", 4))
    gaps: List[str] = []

    while case.research_rounds_done < max_rounds:
        deadline.check()
        rnd = case.research_rounds_done + 1
        print(f"\n[research] round {rnd}/{max_rounds} "
              f"(wall clock left: {deadline.remaining():.0f}s, "
              f"spend: ${ctx.tracker.spent_usd:.2f})")

        budget_err: Optional[BudgetExceeded] = None
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_worker_round, cat, desc, plan, cfg, gaps, ctx): cat
                       for cat, desc in categories.items()}
            for fut in as_completed(futures):
                try:
                    category, result = fut.result()
                    _merge(case, category, result, cfg)
                except BudgetExceeded as e:
                    budget_err = e
        if budget_err:
            case.save(ctx.run_dir)  # keep what the finished workers found
            raise budget_err

        gaps = _synthesize(case, plan, cfg, ctx)
        case.research_rounds_done = rnd
        case.save(ctx.run_dir)  # checkpoint every round

        total_findings = len(case.findings)
        total_options = sum(len(v) for v in case.tool_landscape.values())
        print(f"[research] round {rnd} done: {total_options} options, "
              f"{total_findings} findings, {len(gaps)} coverage gaps")

        if rnd >= min_rounds and _coverage_met(case, cfg) and not gaps:
            print("[research] coverage met — exiting the loop")
            break
        if deadline.expired():
            print("[research] wall clock exhausted — exiting with what we have")
            break

    _verify_citations(case)

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
