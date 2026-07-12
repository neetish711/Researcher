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
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional

# Windows consoles default to cp1252: an error message containing e.g. "→" would
# crash the printing thread BEFORE the run's error state is saved. Replace, never die.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from src.orchestrator.runner import call_agent, gate_satisfied
from src.server import credstore, sources as source_registry
from src.server.events import EventLog, classify_error, read_events, tail_events
from src.state.casefile import CaseFile
from src.tools.costs import BudgetExceeded, CostTracker, StopRequested
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

# ── auth: set CONSOLE_TOKEN to require it on every API call. Static assets and
# /health stay open; file/report GETs also accept ?token= (browser downloads
# can't send headers). Without CONSOLE_TOKEN the console is open (local dev).
CONSOLE_TOKEN = os.environ.get("CONSOLE_TOKEN", "")
_OPEN_PATHS = {"/health", "/", "/index.html", "/favicon.ico"}


@app.middleware("http")
async def _cache_headers(request, call_next):
    """SPA cache discipline: the HTML shell must never be cached (it names the
    current bundle hash — a cached copy 404s after the next deploy purges old
    assets), while the content-hashed /assets/* files are immutable forever."""
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/assets/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif path in ("/", "/index.html"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


@app.middleware("http")
async def _auth(request, call_next):
    if CONSOLE_TOKEN:
        path = request.url.path
        if path not in _OPEN_PATHS and not path.startswith("/assets"):
            supplied = (request.headers.get("x-console-token")
                        or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
                        or request.query_params.get("token", ""))
            if supplied != CONSOLE_TOKEN:
                from fastapi.responses import JSONResponse
                return JSONResponse({"detail": "unauthorized — supply the console token"},
                                    status_code=401)
    return await call_next(request)

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


@app.on_event("startup")
def _recover_interrupted_runs() -> None:
    """A host restart kills in-flight agent threads. Any run still marked
    running:* at boot was interrupted — flip it to a resumable error state so
    the console shows a Retry button instead of a run stuck 'running' forever."""
    if not RUNS_DIR.exists():
        return
    for d in RUNS_DIR.iterdir():
        try:
            if not (d / "casefile.json").exists():
                continue
            case = CaseFile.load(d)
            if case.status.startswith("running:"):
                agent = case.status.split(":", 1)[1]
                case.status = f"error: interrupted by a host restart while running {agent} — press Retry/resume"
                case.next_agent = agent
                case.save(d)
                EventLog(d).emit("error", agent=agent,
                                 error="host restarted mid-run (process exit)",
                                 recovered=False,
                                 impact="checkpointed state kept — resume continues from this agent")
        except Exception:
            continue


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
    for attempt in range(3):  # saves are atomic; retries cover exotic filesystems
        try:
            return CaseFile.load(path)
        except (json.JSONDecodeError, OSError):
            import time
            time.sleep(0.05 * (attempt + 1))
    raise HTTPException(503, f"run {run_id!r} state is briefly unreadable — retry")


def _make_ctx(run_id: str, params: dict) -> RunContext:
    run_dir = RUNS_DIR / run_id
    ctx = RunContext.create(model=params.get("model"), provider=params.get("provider"),
                            run_dir=run_dir, interactive=False,
                            role_models=params.get("models") or {},
                            role_temps=params.get("temperatures") or {})
    ctx.events = EventLog(run_dir)
    ctx.sources = params.get("sources")  # None = all enabled sources
    if params.get("max_rounds"):         # operator shortened/extended the research loop
        ctx.research_overrides = {"max_rounds": int(params["max_rounds"])}
    return ctx


def _gate_payload(case: CaseFile, gate: str) -> dict:
    """What exactly the human is approving — shown in the gate banner."""
    if gate == "confirm_problem":
        return {"problem_statement": case.problem_statement,
                "stated_vs_real": case.stated_vs_real,
                "captured": [c.model_dump() for c in case.captured],
                "data_inventory": [d.model_dump() for d in case.data_inventory],
                "open_interview_questions": case.open_interview_questions,
                "interview_log": case.interview_log}
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
            if (run_dir / "stop.flag").exists():   # operator pressed Stop between agents
                (run_dir / "stop.flag").unlink(missing_ok=True)
                case.next_agent = name
                case.status = f"stopped: by operator before {name} — press Resume to continue"
                case.save(run_dir)
                events.emit("stopped", agent=name,
                            impact="checkpointed state kept — Resume continues from this agent")
                return
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
    except StopRequested as e:
        (run_dir / "stop.flag").unlink(missing_ok=True)
        case.status = f"stopped: {e}"
        events.emit("stopped", agent=name,
                    impact="checkpointed state kept — Resume continues from this agent")
        print(f"[server] run {run_id} stopped by operator at {name}")
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
        "run_id": case.run_id, "title": case.title, "status": case.status, "awaiting_gate": gate,
        "open_questions_count": len(case.open_interview_questions),
        "decision": case.decision.action if case.decision else None,
        "recommended_option": case.decision.recommended_option if case.decision else None,
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
    title: str = ""
    provider: Optional[str] = None
    model: Optional[str] = None                       # run-wide default model
    models: Optional[Dict[str, str]] = None           # per-role: lead/worker/classify/report
    temperatures: Optional[Dict[str, float]] = None   # per-role override
    budget: Optional[str] = None
    sources: Optional[List[str]] = None               # research source ids for this run
    max_rounds: Optional[int] = Field(None, ge=1, le=12)  # shorten/extend Agent 3's loop

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
    max_rounds: Optional[int] = Field(None, ge=1, le=12)


class RerunIn(BaseModel):
    """All optional — anything set overrides the cloned run's params."""
    provider: Optional[str] = None
    model: Optional[str] = None
    models: Optional[Dict[str, str]] = None
    budget: Optional[str] = None
    sources: Optional[List[str]] = None
    max_rounds: Optional[int] = Field(None, ge=1, le=12)


class RejectIn(BaseModel):
    reason: str = ""
    by: str = ""


class ApproveIn(BaseModel):
    by: str = ""


class AnswersIn(BaseModel):
    answers: List[Dict[str, str]]       # [{question, answer}]


class ReviseIn(BaseModel):
    feedback: str
    by: str = ""


class OutcomeIn(BaseModel):
    adoption_pct: Optional[float] = None
    hours_saved_per_month: Optional[float] = None
    notes: str = ""
    recorded_by: str = ""


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
    out = [dict(p, has_key=True) for p in credstore.list_providers()]
    vault_names = {p["name"] for p in out}
    for name, pcfg in (llm_config().get("providers") or {}).items():
        if name not in vault_names:
            env_var = pcfg.get("api_key_env", "")
            has_key = bool(os.environ.get(env_var or "", ""))
            out.append({"name": name, "type": "env (config/llm.yaml)",
                        "base_url": pcfg.get("base_url", ""),
                        "has_key": has_key,
                        "key_fingerprint": f"env:{env_var}" + ("" if has_key else " (NOT SET)")})
    return out


@app.post("/providers", status_code=201)
def providers_save(body: ProviderIn) -> dict:
    base_url = body.base_url.strip() or _TYPE_URLS.get(body.type, "")
    if not base_url:
        raise HTTPException(422, "base_url is required for openai-compatible providers")
    try:
        saved = credstore.save_provider(body.name, body.type, base_url, body.api_key)
    except ValueError as e:
        raise HTTPException(422, str(e))
    saved["persistence"] = credstore.sync_vault_durable()
    return saved


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
    try:
        path.write_text(body.content, encoding="utf-8")
    except OSError as e:
        raise HTTPException(409, "prompts are read-only on this host (serverless bundle) — "
                                 f"edit src/prompts/{name}.md in the repo instead ({e})")
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
    case = CaseFile(title=body.title.strip()[:120])
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
def approve(run_id: str, body: Optional[ApproveIn] = None) -> dict:
    case = _load_case(run_id)
    if not case.status.startswith("awaiting_gate:"):
        raise HTTPException(409, f"run is not waiting at a gate (status: {case.status})")
    gate = case.status.split(":", 1)[1]
    if gate == "confirm_problem":
        case.problem_confirmed_by_human = True
        case.open_interview_questions = []   # approving = accept remaining gaps as assumptions
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
    EventLog(RUNS_DIR / run_id).emit("gate_approved", agent="human", gate=gate,
                                     by=(body.by if body else "") or "unnamed operator")
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
                                     reason=body.reason, by=body.by or "unnamed operator")
    return {"rejected": gate}


_GATE_OWNER = {"confirm_problem": "discovery", "validate_map": "mapping",
               "approve_plan": "research"}


@app.post("/runs/{run_id}/answers")
def submit_answers(run_id: str, body: AnswersIn) -> dict:
    """Answer the discovery interview from the UI — the consultative loop, over HTTP.
    Discovery re-runs with the fuller log and either asks sharper follow-ups or
    completes coverage and returns to the confirm gate."""
    case = _load_case(run_id)
    if case.status.startswith("running"):
        raise HTTPException(409, "run is executing — wait for it to pause")
    if case.problem_confirmed_by_human:
        raise HTTPException(409, "problem already confirmed — use /revise to reopen it")
    answered = [{"question": a.get("question", "").strip(), "answer": a.get("answer", "").strip()}
                for a in body.answers if a.get("question", "").strip()]
    if not answered:
        raise HTTPException(422, "no answers supplied")
    case.interview_log.extend(answered)
    case.open_interview_questions = []
    case.status = "in_progress"
    case.next_agent = "discovery"        # re-run the interview with the fuller log
    case.save(RUNS_DIR / run_id)
    EventLog(RUNS_DIR / run_id).emit("interview_answers", agent="human",
                                     answered=len(answered))
    _kick(run_id)
    return {"recorded": len(answered), "status_url": f"/runs/{run_id}"}


@app.post("/runs/{run_id}/revise")
def revise(run_id: str, body: ReviseIn) -> dict:
    """Reject-with-guidance: feed feedback to the gate's owning agent and regenerate,
    instead of killing the run."""
    case = _load_case(run_id)
    if not case.status.startswith("awaiting_gate:"):
        raise HTTPException(409, f"run is not waiting at a gate (status: {case.status})")
    if not body.feedback.strip():
        raise HTTPException(422, "feedback must be non-empty")
    gate = case.status.split(":", 1)[1]
    owner = _GATE_OWNER.get(gate)
    if owner is None:
        raise HTTPException(409, f"unknown gate {gate!r}")
    case.gate_feedback.setdefault(gate, []).append(body.feedback.strip())
    if gate == "approve_plan":
        case.research_plan = None        # force a fresh plan incorporating the feedback
    if gate == "confirm_problem":
        case.open_interview_questions = []
    case.status = "in_progress"
    case.next_agent = owner
    case.save(RUNS_DIR / run_id)
    EventLog(RUNS_DIR / run_id).emit("gate_revision", agent="human", gate=gate,
                                     feedback=body.feedback, by=body.by or "unnamed operator")
    _kick(run_id)
    return {"revising": owner, "status_url": f"/runs/{run_id}"}


@app.post("/runs/{run_id}/outcomes", status_code=201)
def record_outcome(run_id: str, body: OutcomeIn) -> dict:
    """Post-decision tracking: log actuals against the ROI estimate."""
    from src.state.casefile import OutcomeEntry
    case = _load_case(run_id)
    entry = OutcomeEntry(adoption_pct=body.adoption_pct,
                         hours_saved_per_month=body.hours_saved_per_month,
                         notes=body.notes, recorded_by=body.recorded_by or "unnamed operator")
    case.outcomes.append(entry)
    case.save(RUNS_DIR / run_id)
    EventLog(RUNS_DIR / run_id).emit("outcome_recorded", agent="human",
                                     **entry.model_dump())
    estimate = case.decision.roi.hours_saved_per_month if case.decision else None
    return {"recorded": entry.model_dump(),
            "estimate_hours_saved_per_month": estimate,
            "total_entries": len(case.outcomes)}


@app.post("/runs/{run_id}/resume")
def resume(run_id: str) -> dict:
    case = _load_case(run_id)
    if not (case.status == "paused_budget"
            or case.status.startswith(("error", "rejected", "stopped"))):
        raise HTTPException(409, f"nothing to resume (status: {case.status})")
    (RUNS_DIR / run_id / "stop.flag").unlink(missing_ok=True)
    _kick(run_id)
    return {"resumed_at_agent": case.next_agent, "status_url": f"/runs/{run_id}"}


@app.post("/runs/{run_id}/retry")
def retry(run_id: str, body: RetryIn) -> dict:
    """Retry the failed/paused step — optionally with a different model (per role or
    run-wide), since the model is a per-call parameter."""
    case = _load_case(run_id)
    if not (case.status == "paused_budget"
            or case.status.startswith(("error", "rejected", "awaiting", "stopped"))):
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
    if body.max_rounds:
        params["max_rounds"] = body.max_rounds
    _save_params(run_id, params)
    (RUNS_DIR / run_id / "stop.flag").unlink(missing_ok=True)
    EventLog(RUNS_DIR / run_id).emit("retry", agent="human", step=case.next_agent,
                                     model=body.model or "", models=body.models or {},
                                     provider=body.provider or "")
    if case.status.startswith("awaiting_gate:"):
        return {"retrying": None, "note": "model overrides saved — approve the gate to continue",
                "status_url": f"/runs/{run_id}"}
    _kick(run_id)
    return {"retrying": case.next_agent, "status_url": f"/runs/{run_id}"}


@app.post("/runs/{run_id}/stop")
def stop_run(run_id: str) -> dict:
    """Cooperative stop: the run halts at the next checkpoint (agent boundary or
    research round boundary) keeping everything found so far. Resume continues;
    threads can't be killed mid-LLM-call safely, so this is the honest contract."""
    _load_case(run_id)
    t = _threads.get(run_id)
    if not (t and t.is_alive()):
        raise HTTPException(409, "run is not executing — nothing to stop "
                                 "(use Delete to remove it, or Resume to continue it)")
    (RUNS_DIR / run_id / "stop.flag").touch()
    EventLog(RUNS_DIR / run_id).emit("stop_requested", agent="human",
                                     impact="run pauses at the next checkpoint; findings so far are kept")
    return {"stopping": True,
            "note": "takes effect at the next agent/round checkpoint — a call already in flight finishes first"}


@app.post("/runs/{run_id}/rerun", status_code=201)
def rerun_run(run_id: str, body: Optional[RerunIn] = None) -> dict:
    """Clone a run's intake into a brand-new run (fresh id, fresh state) —
    optionally overriding model/provider/budget/rounds/sources for the new run."""
    old = _load_case(run_id)
    params = _load_params(run_id)
    if body:
        params.update(body.model_dump(exclude_none=True))
    if not (params.get("problem") or "").strip():
        raise HTTPException(409, "original run has no stored problem statement to rerun")
    case = CaseFile(title=old.title)
    run_dir = RUNS_DIR / case.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    _save_params(case.run_id, params)
    case.save(run_dir)
    EventLog(run_dir).emit("run_created", agent="server", rerun_of=run_id,
                           params={k: v for k, v in params.items() if k != "problem"})
    _kick(case.run_id)
    return {"run_id": case.run_id, "rerun_of": run_id, "status_url": f"/runs/{case.run_id}"}


@app.delete("/runs/{run_id}")
def delete_run(run_id: str) -> dict:
    """Remove a run and all its artifacts (casefile, events, reports, uploads).
    An executing run must be stopped first — its thread can't be killed safely."""
    _load_case(run_id)
    t = _threads.get(run_id)
    if t and t.is_alive():
        raise HTTPException(409, "run is executing — press Stop first, then delete "
                                 "once it pauses at the next checkpoint")
    import time
    for attempt in range(4):  # Windows: a polling reader may briefly hold a file handle
        try:
            shutil.rmtree(RUNS_DIR / run_id)
            break
        except (PermissionError, OSError):
            if attempt == 3:
                raise HTTPException(503, "run files are briefly locked — retry the delete")
            time.sleep(0.2 * (attempt + 1))
    with _lock:
        _threads.pop(run_id, None)
    return {"deleted": run_id}


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
    "one_pager.html": ("reports/one_pager.html", "text/html", "decision_brief.html"),
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


# ── research providers: quota-guarded source integrations ───────────────────

from src.server import adapters as adapters_mod
from src.server.quota import QuotaManager, set_free_tier_only, sources_config
from src.server.router import Cache, RouterSession, usage_summary


class KeyIn(BaseModel):
    api_key: str


class SourceRetryIn(BaseModel):
    worker: str = "low_code"
    query: Optional[str] = None
    url: Optional[str] = None
    force_provider: Optional[str] = None


@app.get("/research-sources")
def research_sources() -> dict:
    cfg = sources_config()
    qm = QuotaManager()
    cards = []
    for pid, pc in (cfg.get("providers") or {}).items():
        st = qm.status(pid)
        has_key = bool(adapters_mod.get_key(pid))
        fp = credstore.source_secret_fingerprint(pid) or \
            (f"env:{pc.get('key_env')}" if os.environ.get(pc.get("key_env", "") or "") else "")
        if not has_key and not pc.get("keyless_ok"):
            status = "no_key"
        elif st.get("last_test_ok") is False:
            status = "invalid"          # last Test connection failed — key present but bad
        elif st["remaining"] is not None and st["remaining"] <= 0:
            status = "exhausted"
        elif (st["remaining"] is not None and st["monthly_quota"]
              and st["remaining"] < 0.15 * st["monthly_quota"]):
            status = "quota_low"
        elif st.get("last_test_ok") is True:
            status = "connected"        # verified by a real call
        else:
            status = "untested"         # key present, never tested
        cards.append({"id": pid, "name": pc.get("name", pid), "role": pc.get("role", ""),
                      "key_env": pc.get("key_env", ""), "pricing_url": pc.get("pricing_url", ""),
                      "reliability": pc.get("reliability", "secondary"),
                      "rate_limit": pc.get("rate_limit", {}), "use_when": pc.get("use_when", ""),
                      "allowed_endpoints": pc.get("allowed_endpoints"),
                      "keyless_ok": bool(pc.get("keyless_ok")),
                      "has_key": has_key, "key_fingerprint": fp,
                      "status": status, "quota": st})
    return {"free_tier_only": qm.free_tier_only,
            "providers": cards,
            "keyless": cfg.get("keyless") or [],
            "custom": [s for s in source_registry.list_sources() if not s.get("builtin")],
            "weights": cfg.get("weights") or {}}


@app.post("/research-sources/{pid}/key")
def research_source_key(pid: str, body: KeyIn) -> dict:
    if pid not in (sources_config().get("providers") or {}):
        raise HTTPException(404, f"unknown provider {pid!r}")
    if not body.api_key.strip():
        raise HTTPException(422, "api_key must be non-empty")
    fp = credstore.save_source_secret(pid, body.api_key.strip())
    return {"provider": pid, "key_fingerprint": fp,   # fingerprint only — never the key
            "persistence": credstore.sync_vault_durable()}


@app.delete("/research-sources/{pid}/key")
def research_source_key_delete(pid: str) -> dict:
    credstore.delete_source_secret(pid)
    return {"provider": pid, "deleted": True}


@app.post("/research-sources/{pid}/test")
def research_source_test(pid: str) -> dict:
    """One cheap live call through the quota gate; success marks the quota row verified."""
    cfg = (sources_config().get("providers") or {}).get(pid)
    if cfg is None:
        raise HTTPException(404, f"unknown provider {pid!r}")
    qm = QuotaManager()
    try:
        qm.preflight(pid, 1)
        qm.throttle(pid)
        if pid == "jina":
            out = adapters_mod.jina_read("https://example.com/")
            detail = f"read ok — {out['units']} tokens for example.com"
        elif pid == "firecrawl":
            out = adapters_mod.firecrawl_read("https://example.com/")
            detail = "scrape ok (1 credit consumed)"
        elif pid == "tinyfish":
            out = adapters_mod.tinyfish_extract("https://example.com/", "extract the page title")
            detail = "extraction ok (1 request consumed)"
        elif pid == "tavily":
            out = adapters_mod.tavily_search("workflow automation", 2)
            detail = f"search ok — {len(out['results'])} results (1 credit, depth=basic)"
        elif pid == "zenserp":
            out = adapters_mod.zenserp_search("workflow automation", 2)
            detail = f"search ok — {len(out['results'])} results (1 query)"
        elif pid == "algolia_hn":
            out = adapters_mod.algolia_hn_search("automation", 2)
            detail = f"search ok — {len(out['results'])} results (keyless public index)"
        else:
            raise HTTPException(404, f"no test for {pid!r}")
        qm.consume(pid, out.get("units", 1))
        qm.mark_verified(pid, detail)
        return {"ok": True, "detail": credstore.redact(detail), "quota": qm.status(pid),
                "pricing_url": cfg.get("pricing_url", ""),
                "note": "quota ceilings come from config/sources.yaml — confirm them against "
                        "the live pricing page (free tiers change; Brave killed theirs)"}
    except Exception as e:  # noqa: BLE001 — this endpoint reports, never raises raw
        qm.mark_test_failed(pid, str(e)[:200])
        return {"ok": False, "detail": credstore.redact(str(e)), "quota": qm.status(pid),
                "pricing_url": cfg.get("pricing_url", "")}


class FreeTierIn(BaseModel):
    free_tier_only: bool


@app.patch("/research-sources/config")
def research_sources_config(body: FreeTierIn) -> dict:
    set_free_tier_only(body.free_tier_only)
    return {"free_tier_only": body.free_tier_only,
            "warning": None if body.free_tier_only else
            "Disabling this permits billable calls. Brave-style overage billing has no spend cap."}


@app.post("/forecast")
def forecast() -> dict:
    """Pre-run quota forecast: estimated calls per provider vs remaining free quota."""
    research_cfg = load_yaml(str(CONFIG_DIR / "research.yaml"))
    qm = QuotaManager()
    rows = qm.forecast(research_cfg)
    flags = [r["provider"] for r in rows if r["would_exceed"]]
    b = research_cfg.get("budget") or {}
    return {"providers": rows, "would_exceed": flags,
            "free_tier_only": qm.free_tier_only,
            "suggestion": (f"reduce scope: max_rounds ({b.get('max_rounds')}) or "
                           f"max_queries_per_round in config/research.yaml, or rely on the "
                           "keyless primaries" if flags else None),
            "note": "upper bounds before dedup + cache; cached repeats consume zero quota"}


@app.get("/runs/{run_id}/source-usage")
def source_usage(run_id: str) -> dict:
    case = _load_case(run_id)
    events = read_events(RUNS_DIR / run_id)
    findings = [{"url": f.source.url} for f in case.findings]
    return usage_summary(events, findings)


@app.post("/runs/{run_id}/source-retry")
def source_retry(run_id: str, body: SourceRetryIn) -> dict:
    """One-click retry of a failed source call, optionally forcing a provider.
    The result lands in the cross-run cache, so the agent's next attempt is free."""
    _load_case(run_id)
    if not body.query and not body.url:
        raise HTTPException(422, "give a query (search) or a url (read)")
    ctx = RunContext.create(run_dir=RUNS_DIR / run_id, interactive=False)
    ctx.events = EventLog(RUNS_DIR / run_id)
    router = RouterSession(ctx)
    try:
        if body.url:
            text = router.read(body.worker, body.url, force_provider=body.force_provider)
            return {"ok": bool(text), "chars": len(text),
                    "cached": bool(text), "note": "cached for the agent's next attempt"}
        results = router.search(body.worker, body.query, force_provider=body.force_provider)
        return {"ok": bool(results), "results": results[:10]}
    finally:
        router.release()


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
