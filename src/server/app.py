"""HTTP server mode — the deployable face of the pipeline (Railway etc.).

The CLI's input() gates become API approvals: a run executes agent by agent in a
background thread, stops whenever a flow gate is unsatisfied (status
awaiting_gate:<name>), and POST /runs/{id}/approve sets the human flag and
continues. Same CaseFile, same agent run() functions, same flow.yaml as the CLI —
only the gate transport differs.

    uvicorn src.server.app:app --host 0.0.0.0 --port $PORT

Note: agents run with ctx.interactive=False here, so discovery's follow-up
interview is skipped — put everything you know in the `problem` field. State
lives on disk under runs/<id>/ (attach a volume in production to survive deploys).
"""
from __future__ import annotations

import json
import threading
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from src.orchestrator.runner import call_agent, gate_satisfied
from src.state.casefile import CaseFile
from src.tools.costs import BudgetExceeded
from src.tools.models import CONFIG_DIR, REPO_ROOT, RunContext, load_yaml

app = FastAPI(
    title="Opportunity-to-Solution Copilot",
    description="4-agent pipeline: Discovery → Mapping → Research → Suitability. "
                "Human gates are POST /runs/{id}/approve. Interactive docs at /docs.",
    version="1.0",
)

RUNS_DIR = REPO_ROOT / "runs"
_threads: Dict[str, threading.Thread] = {}
_lock = threading.Lock()


class StartRun(BaseModel):
    problem: str
    model: Optional[str] = None      # per AGENTS.md §2: model is supplied at call time
    provider: Optional[str] = None
    budget: Optional[str] = None     # research wall clock, e.g. "4h"


def _flow_steps() -> list:
    return load_yaml(str(CONFIG_DIR / "flow.yaml")).get("flow") or []


def _params_path(run_id: str):
    return RUNS_DIR / run_id / "server_params.json"


def _load_case(run_id: str) -> CaseFile:
    path = RUNS_DIR / run_id / "casefile.json"
    if not path.exists():
        raise HTTPException(404, f"run {run_id!r} not found")
    return CaseFile.load(path)


def _advance(run_id: str) -> None:
    """Run agents from case.next_agent until a gate, a pause, or the end."""
    run_dir = RUNS_DIR / run_id
    case = CaseFile.load(run_dir)
    params = json.loads(_params_path(run_id).read_text(encoding="utf-8"))
    ctx = RunContext.create(model=params.get("model"), provider=params.get("provider"),
                            run_dir=run_dir, interactive=False)
    ctx.prior_cost_usd = case.cost_spent_usd
    ctx.prior_llm_calls = case.llm_calls

    steps = _flow_steps()
    names = [s["agent"] for s in steps]
    start_idx = names.index(case.next_agent) if case.next_agent in names else 0

    try:
        for step in steps[start_idx:]:
            name = step["agent"]
            case.next_agent = name
            case.status = f"running:{name}"
            case.save(run_dir)
            case = call_agent(name, case, ctx, params.get("problem", ""),
                              params.get("budget"))
            case.cost_spent_usd = ctx.prior_cost_usd + ctx.tracker.spent_usd
            case.llm_calls = ctx.prior_llm_calls + ctx.tracker.calls
            case.save(run_dir)
            gate_name = step.get("gate", "none")
            if not gate_satisfied(case, gate_name):
                case.status = f"awaiting_gate:{gate_name}"
                case.save(run_dir)
                return
        if case.suitability is not None:
            case.status = "complete"
            case.next_agent = None
    except BudgetExceeded as e:
        case.status = "paused_budget"
        print(f"[server] run {run_id} paused on budget: {e}")
    except (Exception, SystemExit) as e:  # SystemExit: agents fail fast this way
        case.status = f"error: {e}"
        print(f"[server] run {run_id} errored: {e}")
    case.cost_spent_usd = ctx.prior_cost_usd + ctx.tracker.spent_usd
    case.llm_calls = ctx.prior_llm_calls + ctx.tracker.calls
    case.save(run_dir)


def _kick(run_id: str) -> None:
    with _lock:
        t = _threads.get(run_id)
        if t and t.is_alive():
            raise HTTPException(409, f"run {run_id} is already executing")
        t = threading.Thread(target=_advance, args=(run_id,), daemon=True)
        _threads[run_id] = t
        t.start()


def _summary(case: CaseFile) -> dict:
    gate = case.status.split(":", 1)[1] if case.status.startswith("awaiting_gate:") else None
    return {
        "run_id": case.run_id,
        "status": case.status,
        "awaiting_gate": gate,
        "approve_with": f"/runs/{case.run_id}/approve" if gate else None,
        "next_agent": case.next_agent,
        "findings": len(case.findings),
        "options": sum(len(v) for v in case.tool_landscape.values()),
        "verdict": case.suitability.verdict if case.suitability else None,
        "cost_spent_usd": round(case.cost_spent_usd, 4),
        "updated_at": case.updated_at,
    }


@app.get("/")
def root() -> dict:
    return {
        "service": "Opportunity-to-Solution Copilot",
        "docs": "/docs",
        "endpoints": {
            "POST /runs": "start a run {problem, model?, provider?, budget?}",
            "GET /runs": "list runs",
            "GET /runs/{id}": "full casefile + status",
            "POST /runs/{id}/approve": "approve the gate the run is waiting at",
            "POST /runs/{id}/resume": "re-kick after a budget pause or error",
            "GET /runs/{id}/report": "interactive HTML report",
        },
        "gates": ["confirm_problem", "validate_map", "approve_plan"],
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/runs", status_code=201)
def start_run(body: StartRun) -> dict:
    if not body.problem.strip():
        raise HTTPException(422, "problem must be non-empty")
    case = CaseFile()
    run_dir = RUNS_DIR / case.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    _params_path(case.run_id).write_text(json.dumps(body.model_dump()), encoding="utf-8")
    case.save(run_dir)
    _kick(case.run_id)
    return {"run_id": case.run_id, "status_url": f"/runs/{case.run_id}"}


@app.get("/runs")
def list_runs() -> list:
    out = []
    if RUNS_DIR.exists():
        for d in sorted(RUNS_DIR.iterdir()):
            if (d / "casefile.json").exists():
                try:
                    out.append(_summary(CaseFile.load(d)))
                except Exception:
                    out.append({"run_id": d.name, "status": "unreadable"})
    return out


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    case = _load_case(run_id)
    return {"summary": _summary(case), "casefile": json.loads(case.model_dump_json())}


@app.post("/runs/{run_id}/approve")
def approve(run_id: str) -> dict:
    case = _load_case(run_id)
    if not case.status.startswith("awaiting_gate:"):
        raise HTTPException(409, f"run is not waiting at a gate (status: {case.status})")
    gate = case.status.split(":", 1)[1]
    if gate == "confirm_problem":
        case.problem_confirmed_by_human = True
    elif gate == "validate_map":
        case.map_validated_by_human = True
    elif gate == "approve_plan":
        if case.research_plan is None:
            raise HTTPException(409, "no research plan on the casefile yet")
        case.research_plan.approved_by_human = True
    else:
        raise HTTPException(409, f"unknown gate {gate!r}")
    case.status = "in_progress"
    case.save(RUNS_DIR / run_id)
    _kick(run_id)
    return {"approved": gate, "status_url": f"/runs/{run_id}"}


@app.post("/runs/{run_id}/resume")
def resume(run_id: str) -> dict:
    case = _load_case(run_id)
    if not (case.status == "paused_budget" or case.status.startswith("error")):
        raise HTTPException(409, f"nothing to resume (status: {case.status})")
    _kick(run_id)
    return {"resumed_at_agent": case.next_agent, "status_url": f"/runs/{run_id}"}


@app.get("/runs/{run_id}/report")
def report(run_id: str) -> FileResponse:
    _load_case(run_id)  # 404 if the run doesn't exist
    path = RUNS_DIR / run_id / "reports" / "detailed_analysis.html"
    if not path.exists():
        raise HTTPException(404, "no report yet — research has not completed")
    return FileResponse(str(path), media_type="text/html")
