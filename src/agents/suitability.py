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
from src.state.casefile import CaseFile, Suitability, VERDICTS
from src.tools.costs import BudgetExceeded
from src.tools.models import RunContext, llm_json, load_prompt
from src.tools.reports import render_html, render_ppt


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

    # refresh the deliverables with the verdict on them
    reports_dir = ctx.run_dir / "reports"
    html = render_html(case, reports_dir / "detailed_analysis.html")
    print(f"[reports] {html}")
    ppt = render_ppt(case, reports_dir / "overview.pptx")
    if ppt:
        print(f"[reports] {ppt}")
    return case


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
