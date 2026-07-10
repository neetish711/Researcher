"""Agent 2 — Workflow Mapping.

Maps the current-state workflow, proposes a labeled future-state, then PAUSES and
routes to a human for validation. map_validated_by_human must be true before the
expensive research runs. Technology-agnostic: never names tools or vendors.

Standalone: python -m src.agents.mapping --input runs/<id>/casefile.json
"""
from __future__ import annotations

import json

from src.agents._common import (build_parser, checkpoint_and_exit, context_from_args,
                                gate, load_or_new, save_and_report)
from src.state.casefile import CaseFile, WorkflowStep
from src.tools.costs import BudgetExceeded
from src.tools.models import RunContext, llm_json, load_prompt

MAX_REVISIONS = 3


def _context_block(case: CaseFile) -> str:
    return json.dumps({
        "problem_statement": case.problem_statement,
        "stated_vs_real": case.stated_vs_real,
        "captured": [c.model_dump() for c in case.captured],
        "data_inventory": [d.model_dump() for d in case.data_inventory],
    }, indent=2)


def _apply(case: CaseFile, data: dict) -> str:
    case.current_workflow = [WorkflowStep(**s) for s in data.get("current_workflow", [])]
    case.future_workflow = [WorkflowStep(**s) for s in data.get("future_workflow", [])]
    return data.get("notes_for_validation", "")


def _print_map(case: CaseFile, notes: str) -> None:
    print("\n──── CURRENT workflow ────")
    for s in case.current_workflow:
        pains = f"  pain: {'; '.join(s.pain_points)}" if s.pain_points else ""
        print(f"  {s.id} {s.name} — {s.actor} via {s.system} ({s.time_estimate}){pains}")
    print("\n──── FUTURE workflow (proposed) ────")
    for s in case.future_workflow:
        print(f"  {s.id} [{s.label}] {s.name} — {s.actor} ({s.time_estimate})")
        if s.rationale:
            print(f"       why: {s.rationale}")
    if notes:
        print(f"\nValidate before sign-off: {notes}")


def run(case: CaseFile, ctx: RunContext) -> CaseFile:
    if not case.problem_statement:
        raise SystemExit("mapping needs a casefile with a problem statement — run discovery "
                         "first or pass --input with a CaseFile stub")
    if not case.problem_confirmed_by_human:
        print("[mapping] warning: problem was never confirmed by a human")

    system = load_prompt("mapping")
    transcript = [{"role": "user", "content": f"Discovery output:\n{_context_block(case)}"}]

    for revision in range(MAX_REVISIONS + 1):
        data = llm_json(messages=transcript, role="lead", system=system, ctx=ctx)
        notes = _apply(case, data)
        _print_map(case, notes)

        # ── hard human gate before anything downstream runs ──
        if gate("Validate this workflow map?", ctx, "validate_map"):
            case.map_validated_by_human = True
            case.status = "in_progress"
            break
        if not ctx.interactive:
            case.status = "awaiting_gate:validate_map"
            break
        feedback = input("What is wrong / missing? (empty = reject and stop)\n> ").strip()
        if not feedback or revision == MAX_REVISIONS:
            case.status = "awaiting_gate:validate_map"
            print("[mapping] map NOT validated — Agent 3 will not start.")
            break
        transcript.append({"role": "assistant", "content": json.dumps(data)})
        transcript.append({"role": "user",
                           "content": f"Human feedback on the map — revise accordingly:\n{feedback}"})

    case.next_agent = "research"
    return case


def main() -> None:
    parser = build_parser("Agent 2 — Workflow Mapping (current + future state, human-validated)")
    args = parser.parse_args()
    if not args.input:
        parser.error("--input runs/<id>/casefile.json is required (mapping builds on discovery)")
    case = load_or_new(args)
    ctx = context_from_args(args, case)
    try:
        case = run(case, ctx)
    except BudgetExceeded as e:
        checkpoint_and_exit(case, ctx, e, next_agent="mapping")
    save_and_report(case, ctx)


if __name__ == "__main__":
    main()
