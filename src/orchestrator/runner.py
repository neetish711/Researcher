"""Orchestrator: executes config/flow.yaml over one CaseFile.

Same graph + CaseFile as standalone mode — the runner calls the exact run()
functions the per-agent CLIs wrap, enforces the human gates declared in the flow,
checkpoints after every agent, and resumes paused runs.

    python -m src.orchestrator.runner --flow config/flow.yaml --problem "…"
    python -m src.orchestrator.runner --flow config/custom.yaml --input runs/<id>/casefile.json
    python -m src.orchestrator.runner --resume runs/<id>
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional

from src.agents import discovery, mapping, research, suitability
from src.agents._common import checkpoint_and_exit, save_and_report
from src.state.casefile import CaseFile
from src.tools.costs import BudgetExceeded
from src.tools.models import CONFIG_DIR, REPO_ROOT, RunContext, load_yaml


def _gate_satisfied(case: CaseFile, gate_name: str) -> bool:
    if gate_name in (None, "", "none"):
        return True
    if gate_name == "confirm_problem":
        return case.problem_confirmed_by_human
    if gate_name == "validate_map":
        return case.map_validated_by_human
    if gate_name == "approve_plan":
        return case.research_plan is not None and case.research_plan.approved_by_human
    raise SystemExit(f"unknown gate {gate_name!r} in flow config")


def _call_agent(name: str, case: CaseFile, ctx: RunContext,
                problem: str, budget: Optional[str]) -> CaseFile:
    if name == "discovery":
        return discovery.run(case, ctx, problem=problem)
    if name == "mapping":
        return mapping.run(case, ctx)
    if name == "research":
        return research.run(case, ctx, budget=budget)
    if name == "suitability":
        return suitability.run(case, ctx)
    raise SystemExit(f"unknown agent {name!r} in flow config")


def run_flow(flow_path: Path, case: CaseFile, ctx: RunContext,
             problem: str = "", budget: Optional[str] = None,
             start_at: Optional[str] = None) -> CaseFile:
    flow_cfg = load_yaml(str(flow_path))
    steps = flow_cfg.get("flow") or []
    if not steps:
        raise SystemExit(f"{flow_path} has no `flow:` list")
    if not flow_cfg.get("human_gates", True):
        ctx.interactive = False
        print("[runner] human_gates: false — running UNATTENDED (not recommended)")

    names = [s["agent"] for s in steps]
    start_idx = names.index(start_at) if start_at in names else 0

    for step in steps[start_idx:]:
        name = step["agent"]
        gate_name = step.get("gate", "none")
        print(f"\n════ agent: {name} (gate after: {gate_name}) ════")
        case.next_agent = name
        try:
            case = _call_agent(name, case, ctx, problem, budget)
        except BudgetExceeded as e:
            checkpoint_and_exit(case, ctx, e, next_agent=name)
        case.save(ctx.run_dir)  # checkpoint after every agent

        if ctx.interactive and not _gate_satisfied(case, gate_name):
            case.status = f"awaiting_gate:{gate_name}"
            save_and_report(case, ctx)
            print(f"[runner] stopped at gate {gate_name!r} — rerun or "
                  f"--resume {ctx.run_dir} once resolved.")
            return case

    if case.suitability is not None:
        case.status = "complete"
        case.next_agent = None
    save_and_report(case, ctx)
    print(f"\n[runner] flow finished with status: {case.status}")
    return case


def main() -> None:
    p = argparse.ArgumentParser(description="Opportunity-to-Solution pipeline runner")
    p.add_argument("--flow", default=str(CONFIG_DIR / "flow.yaml"),
                   help="flow config (default config/flow.yaml)")
    p.add_argument("--problem", default="", help="stated business problem (feeds discovery)")
    p.add_argument("--input", help="start from an existing runs/<id>/casefile.json")
    p.add_argument("--resume", help="resume a paused run dir, e.g. runs/<id>")
    p.add_argument("--model", help="model id for every LLM call (any call can differ in code)")
    p.add_argument("--provider", help="provider name from config/llm.yaml")
    p.add_argument("--budget", help="research wall-clock budget, e.g. 4h")
    p.add_argument("--no-gates", action="store_true",
                   help="skip human gates regardless of flow.yaml (not recommended)")
    args = p.parse_args()

    start_at: Optional[str] = None
    if args.resume:
        case = CaseFile.load(args.resume)
        run_dir = Path(args.resume)
        start_at = case.next_agent
        print(f"[runner] resuming run {case.run_id} at agent {start_at!r} "
              f"(was: {case.status}, prior spend ${case.cost_spent_usd:.2f})")
        case.status = "in_progress"
    elif args.input:
        case = CaseFile.load(args.input)
        run_dir = REPO_ROOT / "runs" / case.run_id
    else:
        case = CaseFile()
        run_dir = REPO_ROOT / "runs" / case.run_id

    ctx = RunContext.create(model=args.model, provider=args.provider, run_dir=run_dir,
                            interactive=not args.no_gates)
    ctx.prior_cost_usd = case.cost_spent_usd
    ctx.prior_llm_calls = case.llm_calls
    run_flow(Path(args.flow), case, ctx, problem=args.problem, budget=args.budget,
             start_at=start_at)


if __name__ == "__main__":
    main()
