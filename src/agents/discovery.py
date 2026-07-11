"""Agent 1 — Stakeholder Discovery.

Structured interview that separates the stated request from the real problem and
captures what data is available. Asks follow-ups only on gaps; tags every captured
item confirmed | assumption | missing; never proposes solutions.
Gate after: human confirms problem + data (blocks the pipeline until then).

Standalone: python -m src.agents.discovery [--problem "..."] [--model <id>]
"""
from __future__ import annotations

from typing import List

from src.agents._common import (build_parser, checkpoint_and_exit, context_from_args,
                                gate, load_or_new, save_and_report)
from src.state.casefile import CapturedItem, CaseFile, DataInventoryItem
from src.tools.costs import BudgetExceeded
from src.tools.models import RunContext, llm_json, load_prompt

MAX_INTERVIEW_TURNS = 10


def _apply(case: CaseFile, data: dict) -> None:
    if data.get("problem_statement"):
        case.problem_statement = data["problem_statement"]
    if data.get("stated_vs_real"):
        case.stated_vs_real = {k: str(v) for k, v in data["stated_vs_real"].items()}
    if data.get("captured"):
        case.captured = [CapturedItem(**c) for c in data["captured"]]
    if data.get("data_inventory"):
        case.data_inventory = [DataInventoryItem(**d) for d in data["data_inventory"]]


def _print_summary(case: CaseFile) -> None:
    print("\n──── discovery summary ────")
    print(f"REAL problem: {case.problem_statement}")
    print(f"Stated:       {case.stated_vs_real.get('stated', '')}")
    print("\nCaptured fields:")
    for c in case.captured:
        print(f"  [{c.status:<10}] {c.field}: {c.value}")
    print("\nData inventory:")
    for d in case.data_inventory:
        print(f"  [{d.status:<10}] {d.name} ({d.format}, {d.sensitivity}) — {d.location}")


def _transcript(case: CaseFile, problem: str) -> List[dict]:
    """Rebuild the interview from persisted state — this is what lets the web UI
    answer questions across requests and lets revisions regenerate with context."""
    parts = [f"Stated business problem:\n{problem}"]
    if case.interview_log:
        parts.append("Interview so far:\n" + "\n\n".join(
            f"Q: {e.get('question', '')}\nA: {e.get('answer', '') or '(no answer — treat as missing)'}"
            for e in case.interview_log))
    for note in case.gate_feedback.get("confirm_problem", []):
        parts.append(f"Human feedback on the previous summary — revise accordingly:\n{note}")
    return [{"role": "user", "content": "\n\n".join(parts)}]


def run(case: CaseFile, ctx: RunContext, problem: str = "") -> CaseFile:
    system = load_prompt("discovery")
    if not problem:
        problem = case.problem_statement or case.stated_vs_real.get("stated", "")
    if not problem and ctx.interactive:
        problem = input("Describe the business problem (as stated by the stakeholder):\n> ").strip()
    if not problem:
        raise SystemExit("discovery needs a problem statement (--problem or interactive input)")

    data: dict = {}
    for turn in range(MAX_INTERVIEW_TURNS):
        data = llm_json(messages=_transcript(case, problem), role="lead", system=system,
                        ctx=ctx, purpose="discovery interview")
        _apply(case, data)
        questions = [str(q) for q in (data.get("follow_up_questions") or [])]
        case.open_interview_questions = [] if data.get("coverage_complete") else questions
        if not case.open_interview_questions:
            break
        if not ctx.interactive:
            # server mode: persist the questions — the UI answers them via
            # POST /runs/{id}/answers, which re-runs this agent with a fuller log
            print(f"[discovery] {len(questions)} open questions await answers in the console")
            break
        print(f"\n[discovery] round {turn + 1} — {len(questions)} gaps to close:")
        for q in questions:
            a = input(f"  Q: {q}\n  A: ").strip()
            case.interview_log.append({"question": q, "answer": a})
        case.open_interview_questions = []

    _print_summary(case)
    if case.open_interview_questions:
        print("\nOpen questions (answer via the console, or approve to accept as assumptions):")
        for q in case.open_interview_questions:
            print(f"  ? {q}")

    # ── human gate: confirm problem + data ──
    if gate("Confirm this problem statement and data inventory?", ctx, "confirm_problem"):
        case.problem_confirmed_by_human = True
        case.status = "in_progress"
    else:
        case.status = "awaiting_gate:confirm_problem"
        print("[discovery] NOT confirmed — pipeline stays blocked here.")
    case.next_agent = "mapping"
    return case


def main() -> None:
    parser = build_parser("Agent 1 — Stakeholder Discovery (structured interview)")
    parser.add_argument("--problem", help="stated business problem (skips the initial prompt)")
    args = parser.parse_args()
    case = load_or_new(args)
    ctx = context_from_args(args, case)
    try:
        case = run(case, ctx, problem=args.problem or "")
    except BudgetExceeded as e:
        checkpoint_and_exit(case, ctx, e, next_agent="discovery")
    save_and_report(case, ctx)


if __name__ == "__main__":
    main()
