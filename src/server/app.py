"""Operator-console backend. Same pipeline as the CLI — plus:

- credential vault endpoints (providers, key-driven model lists) — keys never leave the server
- research-source registry (builtins + custom HTTP APIs, live-testable)
- structured event log per run (events.jsonl) with SSE tail + replay
- gates as API approvals, failure classification, retry-with-different-model
- file staging (uploads become internal research sources), casefile snapshots + diffs
- flow/prompt config introspection and a dry-run "explain plan"

Serves the React operator console from ui/dist at /.
"""
from __future__ import annotations

import json
import re
import shutil
import threading
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from src.orchestrator.runner import call_agent, gate_satisfied
from src.server import credstore, sources as source_registry
from src.server.events import EventLog, classify_error, read_events, tail_events
from src.state.casefile import CaseFile
from src.tools.costs import BudgetExceeded, CostTracker
from src.tools.models import (CONFIG_DIR, PROMPTS_DIR, REPO_ROOT, RunContext,
                              list_models, llm_config, load_yaml, test_provider,
                              validate_model_id)

app = FastAPI(
    title="Opportunity-to-Solution Copilot",
    description="Operator console API. Human gates are POST /runs/{id}/approve. "
                "Keys live only in the server-side vault. Docs: /docs",
    version="2.0",
)

import os
RUNS_DIR = Path(os.environ.get("RUNS_DIR", str(REPO_ROOT / "runs")))
_threads: Dict[str, threading.Thread] = {}
_lock = threading.Lock()
_model_cache: Dict[str, List[str]] = {}

ROLES = ["lead", "worker", "classify", "report"]

AGENT_META = {
    "discovery": {
        "title": "Agent 1 — Stakeholder Discovery", "role": "lead", "prompt": "discovery",
        "purpose": "Structured interview: separate the stated request from the real problem; capture the data inventory.",
        "inputs": ["Free-text problem statement (run form)"],
        "outputs": ["problem_statement", "stated_vs_real", "captured[]", "data_inventory[]"],
        "files": "No files required — this stage runs on the problem text you provide.",
        "guardrails": ["Never proposes solutions or vendors", "Tags every item confirmed/assumption/missing",
                       "Blocks the pipeline until a human confirms"],
        "gate": "confirm_problem",
    },
    "mapping": {
        "title": "Agent 2 — Workflow Mapping", "role": "lead", "prompt": "mapping",
        "purpose": "Map the current workflow; propose a labeled future state; pause for human validation.",
        "inputs": ["Confirmed CaseFile from discovery"],
        "outputs": ["current_workflow[]", "future_workflow[] (labeled)", "map_validated_by_human"],
        "files": "No files required — this stage runs on the CaseFile.",
        "guardrails": ["Technology-agnostic — no tools or vendors named",
                       "Hard human gate before the expensive research runs"],
        "gate": "validate_map",
    },
    "research": {
        "title": "Agent 3 — Research (deep engine)", "role": "lead + parallel workers", "prompt": "research_lead",
        "purpose": "Orchestrator-worker loop: plan → approval → 4 category workers × N rounds → similarity, costs, scores → citation verification → reports.",
        "inputs": ["Validated future_workflow", "problem_statement", "data_inventory",
                   "Enabled research sources", "Optional: staged internal documents"],
        "outputs": ["research_plan", "findings[] (all cited)", "tool_landscape", "open_questions[]",
                    "reports (HTML + PPT)"],
        "files": "Optional — drag internal docs (.txt/.md/.csv/.json) into staging; they join round 1 as internal:// sources.",
        "guardrails": ["No finding without a source (type-enforced)", "Budget caps: rounds/tool-calls/wall-clock/$",
                       "Vendor claims labeled + down-weighted", "Citation pass demotes/drops unreachable evidence"],
        "gate": "approve_plan",
        "sub_prompts": ["research_worker", "research_synthesis"],
    },
    "suitability": {
        "title": "Agent 5 — AI Suitability", "role": "lead", "prompt": "suitability",
        "purpose": "Evidence-bound verdict: whether AI fits, and which kind — citing only researched findings.",
        "inputs": ["future_workflow", "findings[]", "tool_landscape"],
        "outputs": ["suitability verdict + scores + rationale citing finding IDs"],
        "files": "No files required — this stage runs on the CaseFile.",
        "guardrails": ["May not introduce new claims", "Fixed verdict list", "'Don't use AI' is a valid output"],
        "gate": None,
    },
}

# ── helpers ──────────────────────────────────────────────────────────────────


def _flow_steps() -> list:
    return load_yaml(str(CONFIG_DIR / "flow.yaml")).get("flow") or []


def _params_path(run_id: str) -> Path:
    return RUNS_DIR / run_id / "server_params.json"


def _load_params(run_id: str) -> dict:
    p = _params_path(run_id)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _save_params(run_id: str, params: dict) -> None:
    _params_path(run_id).write_text(json.dumps(params), encoding="utf-8")


def _load_case(run_id: str) -> CaseFile:
    path = RUNS_DIR / run_id / "casefile.json"
    if not path.exists():
        raise HTTPException(404, f"run {run_id!r} not found")
    return CaseFile.load(path)


def _make_ctx(run_id: str, params: dict) -> RunContext:
    run_dir = RUNS_DIR / run_id
    ctx = RunContext.create(model=params.get("model"), provider=params.get("provider"),
                            run_dir=run_dir, interactive=False,
                            role_models=params.get("models") or {},
                            role_temps=params.get("temperatures") or {})
    ctx.events = EventLog(run_dir)
    ctx.sources = params.get("sources")  # None = all enabled sources
    return ctx


def _gate_payload(case: CaseFile, gate: str) -> dict:
    """What exactly the human is approving — shown in the gate banner."""
    if gate == "confirm_problem":
        return {"problem_statement": case.problem_statement,
                "stated_vs_real": case.stated_vs_real,
                "captured": [c.model_dump() for c in case.captured],
                "data_inventory": [d.model_dump() for d in case.data_inventory]}
    if gate == "validate_map":
        return {"current_workflow": [s.model_dump() for s in case.current_workflow],
                "future_workflow": [s.model_dump() for s in case.future_workflow]}
    if gate == "approve_plan" and case.research_plan:
        return case.research_plan.model_dump()
    return {}


def _advance(run_id: str) -> None:
    """Run agents from case.next_agent until a gate, a pause, an error, or the end."""
    run_dir = RUNS_DIR / run_id
    case = CaseFile.load(run_dir)
    params = _load_params(run_id)
    ctx = _make_ctx(run_id, params)
    ctx.prior_cost_usd = case.cost_spent_usd
    ctx.prior_llm_calls = case.llm_calls
    events = ctx.events

    steps = _flow_steps()
    names = [s["agent"] for s in steps]
    start_idx = names.index(case.next_agent) if case.next_agent in names else 0

    name = ""
    try:
        for step in steps[start_idx:]:
            name = step["agent"]
            case.next_agent = name
            case.status = f"running:{name}"
            case.save(run_dir)
            events.emit("agent_start", agent=name, gate_after=step.get("gate", "none"))
            import time as _t
            t0 = _t.monotonic()
            case = call_agent(name, case, ctx, params.get("problem", ""),
                              params.get("budget"))
            case.cost_spent_usd = ctx.prior_cost_usd + ctx.tracker.spent_usd
            case.llm_calls = ctx.prior_llm_calls + ctx.tracker.calls
            case.save(run_dir)
            shutil.copyfile(run_dir / "casefile.json",
                            run_dir / f"snapshot_after_{name}.json")
            events.emit("agent_end", agent=name,
                        duration_ms=int((_t.monotonic() - t0) * 1000),
                        cost_usd=round(ctx.tracker.spent_usd, 4))
            events.emit("checkpoint_saved", agent=name)
            gate_name = step.get("gate", "none")
            if not gate_satisfied(case, gate_name):
                case.status = f"awaiting_gate:{gate_name}"
                case.save(run_dir)
                events.emit("gate_waiting", agent=name, gate=gate_name,
                            needs_approval=_gate_payload(case, gate_name))
                return
        if case.suitability is not None:
            case.status = "complete"
            case.next_agent = None
    except BudgetExceeded as e:
        case.status = "paused_budget"
        events.emit("error", agent=name, error=str(e), recovered=False,
                    impact="run paused — resume to continue with a fresh budget window")
        print(f"[server] run {run_id} paused on budget: {e}")
    except (Exception, SystemExit) as e:  # SystemExit: agents fail fast this way
        case.status = f"error: {e}"
        events.emit("error", agent=name, error=str(e), recovered=False,
                    impact="run stopped at this agent — fix the cause and retry")
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
    d = {
        "run_id": case.run_id, "status": case.status, "awaiting_gate": gate,
        "next_agent": case.next_agent, "findings": len(case.findings),
        "options": sum(len(v) for v in case.tool_landscape.values()),
        "rounds_done": case.research_rounds_done,
        "verdict": case.suitability.verdict if case.suitability else None,
        "cost_spent_usd": round(case.cost_spent_usd, 4), "llm_calls": case.llm_calls,
        "created_at": case.created_at, "updated_at": case.updated_at,
        "problem": (case.problem_statement or case.stated_vs_real.get("stated", ""))[:140],
    }
    if case.status.startswith("error"):
        d.update(classify_error(case.status))
    return d


# ── models / request bodies ──────────────────────────────────────────────────

_KEY_MSG = "That looks like an API key, not a model id — add keys under Settings → Providers."


class StartRun(BaseModel):
    problem: str
    provider: Optional[str] = None
    model: Optional[str] = None                       # run-wide default model
    models: Optional[Dict[str, str]] = None           # per-role: lead/worker/classify/report
    temperatures: Optional[Dict[str, float]] = None   # per-role override
    budget: Optional[str] = None
    sources: Optional[List[str]] = None               # research source ids for this run

    @field_validator("model")
    @classmethod
    def _model_not_key(cls, v):
        if v and credstore.looks_like_api_key(v):
            raise ValueError(_KEY_MSG)
        return v

    @field_validator("models")
    @classmethod
    def _models_not_keys(cls, v):
        for role, m in (v or {}).items():
            if m and credstore.looks_like_api_key(m):
                raise ValueError(f"{role}: {_KEY_MSG}")
        return v


class ProviderIn(BaseModel):
    name: str
    type: str = "openai-compatible"     # anthropic | openai | openai-compatible
    base_url: str = ""
    api_key: Optional[str] = None       # omit on update to keep the existing key


class PromptIn(BaseModel):
    content: str


class RetryIn(BaseModel):
    model: Optional[str] = None
    models: Optional[Dict[str, str]] = None
    provider: Optional[str] = None


class RejectIn(BaseModel):
    reason: str = ""


class SourceTest(BaseModel):
    query: str = "workflow automation tools"


class DryRun(BaseModel):
    problem: str
    provider: Optional[str] = None
    model: Optional[str] = None
    models: Optional[Dict[str, str]] = None


# ── providers (Settings → Providers) ─────────────────────────────────────────

_TYPE_URLS = {"anthropic": "https://api.anthropic.com", "openai": "https://api.openai.com/v1"}


@app.get("/providers")
def providers_list() -> list:
    out = credstore.list_providers()
    vault_names = {p["name"] for p in out}
    for name, pcfg in (llm_config().get("providers") or {}).items():
        if name not in vault_names:
            out.append({"name": name, "type": "env (config/llm.yaml)",
                        "base_url": pcfg.get("base_url", ""),
                        "key_fingerprint": f"env:{pcfg.get('api_key_env', '')}"})
    return out


@app.post("/providers", status_code=201)
def providers_save(body: ProviderIn) -> dict:
    base_url = body.base_url.strip() or _TYPE_URLS.get(body.type, "")
    if not base_url:
        raise HTTPException(422, "base_url is required for openai-compatible providers")
    try:
        return credstore.save_provider(body.name, body.type, base_url, body.api_key)
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.delete("/providers/{name}")
def providers_delete(name: str) -> dict:
    if not credstore.delete_provider(name):
        raise HTTPException(404, f"no vault provider {name!r} (llm.yaml providers are read-only here)")
    _model_cache.pop(name, None)
    return {"deleted": name}


@app.post("/providers/{name}/test")
def providers_test(name: str) -> dict:
    result = test_provider(name)
    if result["ok"] and result["models"]:
        _model_cache[name] = result["models"]
    return {"ok": result["ok"], "detail": credstore.redact(result["detail"]),
            "model_count": len(result["models"])}


@app.get("/providers/{name}/models")
def providers_models(name: str, refresh: bool = False) -> dict:
    if refresh or name not in _model_cache:
        try:
            _model_cache[name] = list_models(name)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, credstore.redact(str(e)))
    return {"provider": name, "models": _model_cache[name], "cached": not refresh}


# ── research sources (Settings → Research Sources) ───────────────────────────

@app.get("/sources")
def sources_list() -> list:
    return source_registry.list_sources()


@app.post("/sources", status_code=201)
def sources_add(defn: dict) -> dict:
    try:
        return source_registry.add_custom_source(defn)
    except (ValueError, KeyError) as e:
        raise HTTPException(422, str(e))


@app.patch("/sources/{source_id}")
def sources_update(source_id: str, patch: dict) -> dict:
    api_key = patch.pop("api_key", None)
    if api_key:
        credstore.save_source_secret(source_id, api_key)
    try:
        return source_registry.update_source(source_id, patch)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.delete("/sources/{source_id}")
def sources_delete(source_id: str) -> dict:
    if not source_registry.delete_source(source_id):
        raise HTTPException(404, f"{source_id!r} is not a custom source (builtins can only be disabled)")
    return {"deleted": source_id}


@app.post("/sources/{source_id}/test")
def sources_test(source_id: str, body: SourceTest) -> dict:
    src = source_registry.get_source(source_id)
    if not src:
        raise HTTPException(404, f"unknown source {source_id!r}")
    result = source_registry.search_source(src, body.query, n=5, raw=True)
    raw = result.get("raw")
    raw_str = json.dumps(raw, indent=2)[:8000] if raw is not None else "(non-JSON or ddg builtin)"
    return {"query": body.query, "parsed": result.get("results", []),
            "error": result.get("error"), "raw": credstore.redact(raw_str)}


# ── config introspection (Workflow view) ─────────────────────────────────────

@app.get("/config/flow")
def config_flow() -> dict:
    cfg = load_yaml(str(CONFIG_DIR / "flow.yaml"))
    steps = []
    for step in cfg.get("flow") or []:
        meta = dict(AGENT_META.get(step["agent"], {}))
        meta.update({"agent": step["agent"], "gate_after": step.get("gate", "none")})
        steps.append(meta)
    research_cfg = load_yaml(str(CONFIG_DIR / "research.yaml"))
    return {"flow": steps, "human_gates": cfg.get("human_gates", True),
            "roles": llm_config().get("roles") or {},
            "research": {"budget": research_cfg.get("budget"),
                         "categories": research_cfg.get("categories"),
                         "coverage": research_cfg.get("coverage")}}


@app.get("/config/prompts/{name}")
def prompt_get(name: str) -> dict:
    path = PROMPTS_DIR / f"{name}.md"
    if not re.fullmatch(r"[a-z_]+", name) or not path.exists():
        raise HTTPException(404, f"no prompt {name!r}")
    return {"name": name, "content": path.read_text(encoding="utf-8")}


@app.put("/config/prompts/{name}")
def prompt_put(name: str, body: PromptIn) -> dict:
    path = PROMPTS_DIR / f"{name}.md"
    if not re.fullmatch(r"[a-z_]+", name) or not path.exists():
        raise HTTPException(404, f"no prompt {name!r}")
    path.write_text(body.content, encoding="utf-8")
    return {"name": name, "saved": True,
            "note": "prompt edits apply to the next agent invocation"}


# ── dry run / explain plan ───────────────────────────────────────────────────

@app.post("/dryrun")
def dryrun(body: DryRun) -> dict:
    """What WOULD happen: agents in order, the research plan + example queries,
    and a cost estimate — no workers execute, nothing is persisted."""
    from src.agents.research import _generate_queries, _make_plan, research_config
    ctx = RunContext.create(model=body.model, provider=body.provider)
    ctx.role_models = body.models or {}
    case = CaseFile(problem_statement=body.problem)
    cfg = research_config()
    try:
        plan = _make_plan(case, ctx, cfg)
        first_cat = next(iter((cfg.get("categories") or {}).items()), ("saas", ""))
        queries = _generate_queries(first_cat[0], first_cat[1], plan, cfg, [], ctx)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, credstore.redact(str(e)))
    b = cfg.get("budget") or {}
    rounds, workers = int(b.get("max_rounds", 6)), int(b.get("max_workers", 4))
    calls_per_round = workers * (1 + 10)  # query-gen + ~pages/batch extraction calls
    est_calls = 1 + rounds * (calls_per_round + 1) + 2
    tracker = CostTracker()
    est_cost = est_calls * (6000 / 1e6 * tracker.price_in + 1500 / 1e6 * tracker.price_out)
    return {
        "flow": [{"agent": s["agent"], "gate_after": s.get("gate", "none")} for s in _flow_steps()],
        "research_plan": plan.model_dump(),
        "example_queries": {first_cat[0]: queries},
        "estimate": {"max_rounds": rounds, "workers": workers,
                     "llm_calls_upper_bound": est_calls,
                     "cost_usd_upper_bound": round(min(est_cost,
                         (llm_config().get("limits") or {}).get("max_cost_usd_per_run", 25)), 2),
                     "assumptions": ["~6k tokens in / 1.5k out per call",
                                     f"price ${tracker.price_in}/{tracker.price_out} per MTok in/out "
                                     "(LLM_COST_PER_MTOK_* env)",
                                     "capped by limits.max_cost_usd_per_run"]},
        "dry_run_cost_usd": round(ctx.tracker.spent_usd, 4),
    }


# ── runs ─────────────────────────────────────────────────────────────────────

@app.post("/runs", status_code=201)
def start_run(body: StartRun) -> dict:
    if not body.problem.strip():
        raise HTTPException(422, "problem must be non-empty")
    case = CaseFile()
    run_dir = RUNS_DIR / case.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    _save_params(case.run_id, body.model_dump())
    case.save(run_dir)
    EventLog(run_dir).emit("run_created", agent="server",
                           params={k: v for k, v in body.model_dump().items() if k != "problem"})
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
    return sorted(out, key=lambda r: r.get("created_at", ""), reverse=True)


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    case = _load_case(run_id)
    summary = _summary(case)
    gate = summary["awaiting_gate"]
    params = _load_params(run_id)
    params.pop("problem", None)
    return {"summary": summary,
            "gate_payload": _gate_payload(case, gate) if gate else None,
            "params": params,
            "casefile": json.loads(case.model_dump_json())}


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
    EventLog(RUNS_DIR / run_id).emit("gate_approved", agent="human", gate=gate)
    _kick(run_id)
    return {"approved": gate, "status_url": f"/runs/{run_id}"}


@app.post("/runs/{run_id}/reject")
def reject(run_id: str, body: RejectIn) -> dict:
    case = _load_case(run_id)
    if not case.status.startswith("awaiting_gate:"):
        raise HTTPException(409, f"run is not waiting at a gate (status: {case.status})")
    gate = case.status.split(":", 1)[1]
    case.status = f"rejected:{gate}"
    case.save(RUNS_DIR / run_id)
    EventLog(RUNS_DIR / run_id).emit("gate_rejected", agent="human", gate=gate,
                                     reason=body.reason)
    return {"rejected": gate}


@app.post("/runs/{run_id}/resume")
def resume(run_id: str) -> dict:
    case = _load_case(run_id)
    if not (case.status == "paused_budget" or case.status.startswith(("error", "rejected"))):
        raise HTTPException(409, f"nothing to resume (status: {case.status})")
    _kick(run_id)
    return {"resumed_at_agent": case.next_agent, "status_url": f"/runs/{run_id}"}


@app.post("/runs/{run_id}/retry")
def retry(run_id: str, body: RetryIn) -> dict:
    """Retry the failed/paused step — optionally with a different model (per role or
    run-wide), since the model is a per-call parameter."""
    case = _load_case(run_id)
    if not (case.status == "paused_budget" or case.status.startswith(("error", "rejected", "awaiting"))):
        raise HTTPException(409, f"nothing to retry (status: {case.status})")
    for m in [body.model] + list((body.models or {}).values()):
        if m and credstore.looks_like_api_key(m):
            raise HTTPException(422, _KEY_MSG)
    params = _load_params(run_id)
    if body.model:
        params["model"] = body.model
    if body.models:
        params["models"] = {**(params.get("models") or {}), **body.models}
    if body.provider:
        params["provider"] = body.provider
    _save_params(run_id, params)
    EventLog(RUNS_DIR / run_id).emit("retry", agent="human", step=case.next_agent,
                                     model=body.model or "", models=body.models or {},
                                     provider=body.provider or "")
    if case.status.startswith("awaiting_gate:"):
        return {"retrying": None, "note": "model overrides saved — approve the gate to continue",
                "status_url": f"/runs/{run_id}"}
    _kick(run_id)
    return {"retrying": case.next_agent, "status_url": f"/runs/{run_id}"}


# ── events / metrics ─────────────────────────────────────────────────────────

@app.get("/runs/{run_id}/events")
def events_replay(run_id: str, since: int = 0, limit: int = 0) -> dict:
    _load_case(run_id)
    events = read_events(RUNS_DIR / run_id, since_seq=since, limit=limit)
    return {"events": events, "last_seq": events[-1]["seq"] if events else since}


@app.get("/runs/{run_id}/events/stream")
def events_stream(run_id: str, since: int = 0) -> StreamingResponse:
    _load_case(run_id)

    def gen():
        for e in tail_events(RUNS_DIR / run_id, since_seq=since):
            yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/runs/{run_id}/metrics")
def metrics(run_id: str) -> dict:
    case = _load_case(run_id)
    events = read_events(RUNS_DIR / run_id)
    llm_calls = [e for e in events if e["type"] == "llm_call"]
    research_cfg = load_yaml(str(CONFIG_DIR / "research.yaml"))
    b = research_cfg.get("budget") or {}
    cov = research_cfg.get("coverage") or {}
    cats = list((research_cfg.get("categories") or {}).keys())
    per_cat = {c: len(case.tool_landscape.get(c, [])) for c in cats}
    need = max(1, int(cov.get("min_options_per_category", 3))) * max(1, len(cats))
    coverage_pct = min(100, round(100 * sum(min(v, int(cov.get("min_options_per_category", 3)))
                                            for v in per_cat.values()) / need))
    last_round = next((e for e in reversed(events) if e["type"] == "round_complete"), None)
    return {
        "status": case.status,
        "rounds": {"done": case.research_rounds_done, "max": int(b.get("max_rounds", 6))},
        "coverage_pct": coverage_pct,
        "options_per_category": per_cat,
        "findings": {"created": len([e for e in events if e["type"] == "finding_created"]) or len(case.findings),
                     "verified": len([e for e in events if e["type"] == "citation_verified"]),
                     "rejected": len([e for e in events if e["type"] == "citation_rejected"])},
        "sources_hit": len({e.get("source") for e in events if e["type"] == "search_query"}),
        "tokens": {"in": sum(e.get("tokens_in", 0) for e in llm_calls),
                   "out": sum(e.get("tokens_out", 0) for e in llm_calls)},
        "cost_spent_usd": round(case.cost_spent_usd, 4),
        "cost_cap_usd": (llm_config().get("limits") or {}).get("max_cost_usd_per_run"),
        "wall_clock": {"left_s": last_round.get("wall_clock_left_s") if last_round else None,
                       "budget_s": last_round.get("wall_clock_budget_s") if last_round else None},
        "problems": len([e for e in events if e.get("status") == "error" or e["type"] == "error"]),
        "elapsed_s": int(events[-1]["ts"] - events[0]["ts"]) if events else 0,
    }


# ── file staging ─────────────────────────────────────────────────────────────

_UPLOAD_TYPES = {".txt", ".md", ".csv", ".json"}
_UPLOAD_MAX = 5 * 1024 * 1024
_SENSITIVITIES = {"public", "internal", "confidential", "restricted"}


def _uploads_meta(run_id: str) -> list:
    p = RUNS_DIR / run_id / "uploads" / "meta.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def _save_uploads_meta(run_id: str, meta: list) -> None:
    p = RUNS_DIR / run_id / "uploads" / "meta.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(meta, indent=2), encoding="utf-8")


@app.get("/runs/{run_id}/inputs")
def stage_inputs(run_id: str) -> dict:
    """Exactly what each stage needs, and whether it's satisfied right now."""
    case = _load_case(run_id)
    uploads = _uploads_meta(run_id)
    return {"stages": [
        {"agent": "discovery", "files": AGENT_META["discovery"]["files"],
         "needs": ["problem text"], "satisfied": bool(_load_params(run_id).get("problem"))},
        {"agent": "mapping", "files": AGENT_META["mapping"]["files"],
         "needs": ["human-confirmed problem"], "satisfied": case.problem_confirmed_by_human},
        {"agent": "research", "files": AGENT_META["research"]["files"],
         "needs": ["human-validated map", "≥1 enabled research source"],
         "satisfied": case.map_validated_by_human,
         "staged_files": len(uploads)},
        {"agent": "suitability", "files": AGENT_META["suitability"]["files"],
         "needs": ["cited findings from research"], "satisfied": len(case.findings) > 0},
    ]}


@app.post("/runs/{run_id}/uploads", status_code=201)
async def upload(run_id: str, file: UploadFile = File(...),
                 sensitivity: str = Form("internal")) -> dict:
    _load_case(run_id)
    if sensitivity not in _SENSITIVITIES:
        raise HTTPException(422, f"sensitivity must be one of {sorted(_SENSITIVITIES)}")
    name = Path(file.filename or "upload").name
    ext = Path(name).suffix.lower()
    if ext not in _UPLOAD_TYPES:
        raise HTTPException(422, f"unsupported type {ext!r} — accepted: {sorted(_UPLOAD_TYPES)}. "
                                 "(PDF/DOCX extraction is not wired up yet — export to text first.)")
    content = await file.read()
    if len(content) > _UPLOAD_MAX:
        raise HTTPException(422, f"file exceeds {_UPLOAD_MAX // (1024*1024)} MB limit")
    updir = RUNS_DIR / run_id / "uploads"
    updir.mkdir(parents=True, exist_ok=True)
    (updir / name).write_bytes(content)
    entry = {"name": name, "size": len(content), "sensitivity": sensitivity,
             "status": "stored", "error": ""}
    try:
        text = content.decode("utf-8", errors="replace")
        (updir / f"{name}.extracted.txt").write_text(text, encoding="utf-8")
        entry["status"] = "parsed"  # parsed → fed to research workers as internal:// source
        entry["chars"] = len(text)
    except Exception as e:  # noqa: BLE001
        entry.update(status="error", error=str(e))
    meta = [m for m in _uploads_meta(run_id) if m["name"] != name] + [entry]
    _save_uploads_meta(run_id, meta)
    EventLog(RUNS_DIR / run_id).emit("doc_read", agent="staging", url=f"internal://{name}",
                                     status="ok" if entry["status"] == "parsed" else "error",
                                     sensitivity=sensitivity, chars=entry.get("chars", 0),
                                     error=entry["error"])
    return entry


@app.get("/runs/{run_id}/uploads")
def uploads_list(run_id: str) -> dict:
    _load_case(run_id)
    return {"files": _uploads_meta(run_id)}


@app.delete("/runs/{run_id}/uploads/{name}")
def upload_delete(run_id: str, name: str) -> dict:
    _load_case(run_id)
    safe = Path(name).name
    updir = RUNS_DIR / run_id / "uploads"
    removed = False
    for p in (updir / safe, updir / f"{safe}.extracted.txt"):
        if p.exists():
            p.unlink()
            removed = True
    _save_uploads_meta(run_id, [m for m in _uploads_meta(run_id) if m["name"] != safe])
    if not removed:
        raise HTTPException(404, f"no staged file {safe!r}")
    return {"deleted": safe}


# ── snapshots (CaseFile diff view) ───────────────────────────────────────────

@app.get("/runs/{run_id}/snapshots")
def snapshots(run_id: str) -> dict:
    _load_case(run_id)
    out = []
    for step in _flow_steps():
        p = RUNS_DIR / run_id / f"snapshot_after_{step['agent']}.json"
        if p.exists():
            out.append({"agent": step["agent"], "url": f"/runs/{run_id}/snapshots/{step['agent']}"})
    return {"snapshots": out}


@app.get("/runs/{run_id}/snapshots/{agent}")
def snapshot_get(run_id: str, agent: str) -> dict:
    p = RUNS_DIR / run_id / f"snapshot_after_{Path(agent).name}.json"
    if not p.exists():
        raise HTTPException(404, f"no snapshot after {agent!r}")
    return json.loads(p.read_text(encoding="utf-8"))


# ── reports / downloads ──────────────────────────────────────────────────────

_RUN_FILES = {
    "casefile.json": ("casefile.json", "application/json", "casefile.json"),
    "report.html": ("reports/detailed_analysis.html", "text/html", "detailed_analysis.html"),
    "overview.pptx": ("reports/overview.pptx",
                      "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                      "overview.pptx"),
    "events.jsonl": ("events.jsonl", "application/x-ndjson", "events.jsonl"),
}


@app.get("/runs/{run_id}/report")
def report(run_id: str) -> FileResponse:
    _load_case(run_id)
    path = RUNS_DIR / run_id / "reports" / "detailed_analysis.html"
    if not path.exists():
        raise HTTPException(404, "no report yet — research has not completed")
    return FileResponse(str(path), media_type="text/html")


@app.get("/runs/{run_id}/files")
def list_files(run_id: str) -> dict:
    _load_case(run_id)
    files = []
    for name, (rel, _media, _dl) in _RUN_FILES.items():
        path = RUNS_DIR / run_id / rel
        files.append({"name": name, "available": path.exists(),
                      "size": path.stat().st_size if path.exists() else 0,
                      "url": f"/runs/{run_id}/files/{name}"})
    return {"files": files}


@app.get("/runs/{run_id}/files/{name}")
def download_file(run_id: str, name: str) -> FileResponse:
    _load_case(run_id)
    if name not in _RUN_FILES:  # whitelist — never serve arbitrary paths
        raise HTTPException(404, f"unknown file {name!r} (have: {list(_RUN_FILES)})")
    rel, media, download_name = _RUN_FILES[name]
    path = RUNS_DIR / run_id / rel
    if not path.exists():
        raise HTTPException(404, f"{name} not generated yet for this run")
    return FileResponse(str(path), media_type=media, filename=download_name)


# ── misc ─────────────────────────────────────────────────────────────────────

@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api")
def api_index() -> dict:
    return {"service": "Opportunity-to-Solution Copilot", "version": "2.0", "docs": "/docs",
            "areas": {"providers": "/providers (vault: keys never leave the server)",
                      "sources": "/sources (research source registry)",
                      "runs": "/runs (+ /events, /metrics, /approve, /retry, /uploads, /files)",
                      "config": "/config/flow, /config/prompts/{name}", "dryrun": "POST /dryrun"}}


# React operator console (built assets). Registered last: API routes above win.
_UI_DIST = REPO_ROOT / "ui" / "dist"
if _UI_DIST.exists():
    app.mount("/", StaticFiles(directory=str(_UI_DIST), html=True), name="ui")
else:  # dev fallback before the first `npm run build`
    @app.get("/", response_class=HTMLResponse)
    def _no_ui() -> str:
        return ("<h3>UI not built</h3><p>Run <code>cd ui && npm install && npm run build</code>, "
                "then restart. API docs: <a href='/docs'>/docs</a></p>")
