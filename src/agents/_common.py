"""Shared CLI plumbing + human-gate helper. Every agent exposes run(case, ctx) and a
standalone `python -m src.agents.<name>` entry that wraps the same run() — which is
what makes standalone and orchestrated behavior identical.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional

from src.state.casefile import CaseFile
from src.tools.costs import BudgetExceeded
from src.tools.models import REPO_ROOT, RunContext


def build_parser(description: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--input", help="path to runs/<id>/casefile.json (or the run dir)")
    p.add_argument("--model", help="model id for every LLM call in this invocation")
    p.add_argument("--provider", help="provider name from config/llm.yaml")
    p.add_argument("--run-dir", help="where to save the casefile (default runs/<run_id>)")
    p.add_argument("--no-gates", action="store_true",
                   help="skip human gates (unattended; not recommended)")
    return p


def context_from_args(args: argparse.Namespace, case: CaseFile) -> RunContext:
    run_dir = Path(args.run_dir) if args.run_dir else REPO_ROOT / "runs" / case.run_id
    ctx = RunContext.create(model=args.model, provider=args.provider,
                            run_dir=run_dir, interactive=not args.no_gates)
    ctx.prior_cost_usd = case.cost_spent_usd
    ctx.prior_llm_calls = case.llm_calls
    return ctx


def load_or_new(args: argparse.Namespace) -> CaseFile:
    if args.input:
        return CaseFile.load(args.input)
    return CaseFile()


def gate(question: str, ctx: RunContext, gate_name: str = "") -> bool:
    """Human gate. Returns True only on a real human yes; unattended mode returns
    False (the flag stays honest) but callers proceed with a warning."""
    if not ctx.interactive:
        print(f"[gate:{gate_name}] human gates disabled — proceeding UNVALIDATED")
        return False
    while True:
        answer = input(f"\n{question} [y/n] > ").strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("please answer y or n")


def checkpoint_and_exit(case: CaseFile, ctx: RunContext, err: BudgetExceeded,
                        next_agent: str) -> None:
    """checkpoint_and_pause: save state and tell the human how to resume."""
    case.status = "paused_budget"
    case.next_agent = next_agent
    case.cost_spent_usd = ctx.prior_cost_usd + ctx.tracker.spent_usd
    case.llm_calls = ctx.prior_llm_calls + ctx.tracker.calls
    path = case.save(ctx.run_dir)
    print(f"\n[budget] {err}")
    print(f"[budget] checkpointed to {path}")
    print(f"[budget] resume with: python -m src.orchestrator.runner --resume {ctx.run_dir}")
    raise SystemExit(3)


def save_and_report(case: CaseFile, ctx: RunContext) -> Path:
    case.cost_spent_usd = ctx.prior_cost_usd + ctx.tracker.spent_usd
    case.llm_calls = ctx.prior_llm_calls + ctx.tracker.calls
    path = case.save(ctx.run_dir)
    print(f"\n[saved] {path}  (spend: ${case.cost_spent_usd:.2f} total, "
          f"{ctx.tracker.calls} LLM calls this session)")
    return path
