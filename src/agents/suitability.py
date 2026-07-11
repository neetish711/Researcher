"""Agent 5 — AI Suitability.

Decides whether AI is the right answer — and which kind — using ONLY Agent 3's cited
evidence. May not introduce new claims; every rationale sentence cites finding IDs;
an honest "don't use AI" is a valid output. Re-renders the reports with the verdict.

Standalone: python -m src.agents.suitability --input runs/<id>/casefile.json
"""
from __future__ import annotations

import json

from src.agents._common import (build_parser, checkpoint_and_exit, context_from_args,
                                load_or_new, save_and_report)
from src.state.casefile import (CaseFile, Decision, DECISION_ACTIONS, PilotPlan,
                                ROIEstimate, Suitability, VERDICTS)
from src.tools.costs import BudgetExceeded
from src.tools.models import RunContext, llm_json, load_prompt, load_yaml, CONFIG_DIR
from src.tools.reports import render_html, render_one_pager, render_ppt


def _evidence_block(case: CaseFile) -> str:
    return json.dumps({
        "problem_statement": case.problem_statement,
        "future_workflow": [s.model_dump() for s in case.future_workflow],
        "findings": [{"id": f.id, "claim": f.claim, "kind": f.kind,
                      "category": f.category, "vendor_claim": f.vendor_claim,
                      "verified": f.source.verified, "url": f.source.url}
                     for f in case.findings],
        "tool_landscape": {
            cat: [{"name": o.name, "similarity": o.similarity.model_dump(),
                   "scores": o.scores,
                   "monthly_operation_usd_estimate": o.costs.monthly_operation_usd}
                  for o in options]
            for cat, options in case.tool_landscape.items()},
        "open_questions": case.open_questions,
    }, indent=2)


def run(case: CaseFile, ctx: RunContext) -> CaseFile:
    if not case.findings:
        raise SystemExit("suitability is evidence-bound: the casefile has no findings — "
                         "run research first")

    system = load_prompt("suitability")
    transcript = [{"role": "user", "content": f"Cited evidence:\n{_evidence_block(case)}"}]

    verdict = None
    for attempt in range(2):
        data = llm_json(messages=transcript, role="lead", system=system, ctx=ctx)
        cited = [fid for fid in data.get("cited_finding_ids", []) if case.get_finding(fid)]
        try:
            verdict = Suitability(
                verdict=str(data.get("verdict", "")).strip(),
                scores={k: float(v) for k, v in (data.get("scores") or {}).items()},
                rationale=data.get("rationale", ""),
                cited_finding_ids=cited,
                better_path=data.get("better_path"),
            )
        except ValueError as e:
            transcript.append({"role": "assistant", "content": json.dumps(data)})
            transcript.append({"role": "user",
                               "content": f"Invalid output ({e}). The verdict must be EXACTLY "
                                          f"one of: {VERDICTS}. Re-emit the strict JSON."})
            continue
        if not cited:
            transcript.append({"role": "assistant", "content": json.dumps(data)})
            transcript.append({"role": "user",
                               "content": "cited_finding_ids referenced no real finding IDs. "
                                          "Cite only IDs from the supplied evidence and re-emit."})
            verdict = None
            continue
        break
    if verdict is None:
        raise SystemExit("suitability: could not obtain a valid, evidence-cited verdict")

    case.suitability = verdict
    _decide(case, ctx)
    case.status = "complete"
    case.next_agent = None

    print("\n──── AI suitability brief ────")
    print(f"VERDICT: {verdict.verdict}")
    for k, v in verdict.scores.items():
        print(f"  {k:<16} {v:g}/10")
    print(f"\n{verdict.rationale}")
    if verdict.better_path:
        print(f"\nBetter path: {verdict.better_path}")
    print(f"\nEvidence: {', '.join(verdict.cited_finding_ids)}")

    if case.decision:
        d = case.decision
        print(f"\nDECISION: {d.action.upper()}"
              + (f" — {d.recommended_option}" if d.recommended_option else ""))
        if d.roi.monthly_value_usd:
            print(f"  ROI (estimate): ~{d.roi.hours_saved_per_month:g} h/month ≈ "
                  f"${d.roi.monthly_value_usd:,.0f}/month · payback "
                  f"{d.roi.payback_months_low:g}–{d.roi.payback_months_high:g} months")

    # refresh the deliverables with the verdict + decision on them
    reports_dir = ctx.run_dir / "reports"
    html = render_html(case, reports_dir / "detailed_analysis.html")
    print(f"[reports] {html}")
    one_pager = render_one_pager(case, reports_dir / "one_pager.html")
    print(f"[reports] {one_pager}")
    ppt = render_ppt(case, reports_dir / "overview.pptx")
    if ppt:
        print(f"[reports] {ppt}")
    return case


def _decide(case: CaseFile, ctx: RunContext) -> None:
    """Turn the verdict into build/buy/pilot/modify/reject + ROI. Evidence-bound;
    a failure here never voids the suitability verdict itself."""
    hourly = (load_yaml(str(CONFIG_DIR / "research.yaml")).get("cost") or {}).get("hourly_rate_usd", 120)
    payload = json.dumps({
        "verdict": case.suitability.model_dump() if case.suitability else None,
        "problem_statement": case.problem_statement,
        "captured": [c.model_dump() for c in case.captured],
        "hourly_rate_usd": hourly,
        "tool_landscape": {
            cat: [{"name": o.name, "similarity": o.similarity.model_dump(),
                   "costs": o.costs.model_dump(), "scores": o.scores,
                   "vendor_only": o.vendor_only, "community_only": o.community_only}
                  for o in options]
            for cat, options in case.tool_landscape.items()},
        "future_workflow": [s.model_dump() for s in case.future_workflow],
    }, indent=2)
    transcript = [{"role": "user", "content": f"Evidence:\n{payload}"}]
    system = load_prompt("decision")
    option_names = {o.name.lower() for opts in case.tool_landscape.values() for o in opts}
    for attempt in range(2):
        try:
            data = llm_json(messages=transcript, role="lead", system=system, ctx=ctx,
                            purpose="decision + ROI")
            rec = data.get("recommended_option")
            if data.get("action") in ("buy", "pilot") and (not rec or rec.lower() not in option_names):
                raise ValueError(f"action {data.get('action')!r} must name a landscape option")
            pilot = data.get("pilot_plan")
            case.decision = Decision(
                action=str(data.get("action", "")).strip(),
                recommended_option=rec,
                rationale=data.get("rationale", ""),
                pilot_plan=PilotPlan(**{k: v for k, v in (pilot or {}).items()
                                        if k in PilotPlan.model_fields}) if pilot else None,
                roi=ROIEstimate(**{k: v for k, v in (data.get("roi") or {}).items()
                                   if k in ROIEstimate.model_fields}),
                cited_finding_ids=[fid for fid in data.get("cited_finding_ids", [])
                                   if case.get_finding(fid)],
            )
            return
        except (ValueError, TypeError) as e:
            transcript.append({"role": "assistant", "content": json.dumps(data) if 'data' in locals() else ""})
            transcript.append({"role": "user",
                               "content": f"Invalid output ({e}). action must be one of "
                                          f"{DECISION_ACTIONS}; buy/pilot must name a real option. Re-emit."})
        except Exception as e:  # noqa: BLE001 — decision is additive, never fatal
            ctx.emit("error", agent="suitability", error=f"decision layer failed: {e}",
                     recovered=True, impact="verdict stands; no build/buy/pilot decision recorded")
            return
    ctx.emit("error", agent="suitability", error="decision layer: no valid output after retry",
             recovered=True, impact="verdict stands; no decision recorded")


def main() -> None:
    parser = build_parser("Agent 5 — AI Suitability (evidence-bound decision brief)")
    args = parser.parse_args()
    if not args.input:
        parser.error("--input runs/<id>/casefile.json is required (suitability needs cited findings)")
    case = load_or_new(args)
    ctx = context_from_args(args, case)
    try:
        case = run(case, ctx)
    except BudgetExceeded as e:
        checkpoint_and_exit(case, ctx, e, next_agent="suitability")
    save_and_report(case, ctx)


if __name__ == "__main__":
    main()
