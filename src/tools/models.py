"""LLM access. Nothing about a specific model is hardcoded anywhere in this repo:

- a ROLE (config/llm.yaml `roles`) supplies only temperature + max_tokens;
- a PROVIDER supplies the key + base_url — either a vault connection saved via the
  UI (src/server/credstore.py, takes precedence) or an env-keyed entry in llm.yaml;
- the MODEL comes from the call, resolution order:
      call arg -> ctx per-role override -> ctx run override
      -> config/run.yaml models[role]/models.default -> llm.yaml defaults.model
  If nothing resolves, fail fast and ask for one (ModelNotSpecified).

Wire format (Anthropic messages vs OpenAI chat completions) is chosen from the
provider's type/base_url. Every call is instrumented: if ctx.events is set, an
llm_call event (redacted prompt/response, tokens, cost, duration) is emitted, plus
retry/error events — that event stream is the Run Console's source of truth.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import yaml

from src.tools.costs import BudgetExceeded, CostTracker

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
PROMPTS_DIR = REPO_ROOT / "src" / "prompts"


class ModelNotSpecified(RuntimeError):
    pass


class LLMError(RuntimeError):
    pass


@lru_cache(maxsize=None)
def _load_yaml_cached(path: str, mtime: float) -> Dict[str, Any]:
    p = Path(path)
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def load_yaml(path: str) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    return _load_yaml_cached(str(p), p.stat().st_mtime)  # reload when the file changes


def llm_config() -> Dict[str, Any]:
    return load_yaml(str(CONFIG_DIR / "llm.yaml"))


def run_config() -> Dict[str, Any]:
    return load_yaml(str(CONFIG_DIR / "run.yaml"))


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def validate_model_id(model: str) -> None:
    """Reject anything that looks like a pasted API key (the UI's security hole)."""
    from src.server.credstore import looks_like_api_key
    if model and looks_like_api_key(model):
        raise ValueError("That looks like an API key, not a model id — "
                         "add keys under Settings → Providers.")


@dataclass
class RunContext:
    """Per-run knobs threaded through every agent (standalone, orchestrated, server)."""
    model: Optional[str] = None            # run-wide override (CLI --model)
    provider: Optional[str] = None
    role_models: Dict[str, str] = field(default_factory=dict)   # per-role overrides
    role_temps: Dict[str, float] = field(default_factory=dict)  # per-role temperature
    run_dir: Path = field(default_factory=lambda: REPO_ROOT / "runs" / "adhoc")
    tracker: CostTracker = field(default_factory=CostTracker)
    interactive: bool = True               # False = human gates are skipped (unattended)
    prior_cost_usd: float = 0.0            # spend from earlier sessions of a resumed run
    prior_llm_calls: int = 0
    events: Optional[object] = None        # EventLog (src/server/events.py) or None
    sources: Optional[List[str]] = None    # research source ids enabled for this run

    @classmethod
    def create(cls, model: Optional[str] = None, provider: Optional[str] = None,
               run_dir: Optional[Path | str] = None, interactive: bool = True,
               role_models: Optional[Dict[str, str]] = None,
               role_temps: Optional[Dict[str, float]] = None) -> "RunContext":
        limits = llm_config().get("limits", {}) or {}
        tracker = CostTracker(
            max_cost_usd=limits.get("max_cost_usd_per_run"),
            on_limit=limits.get("on_limit", "checkpoint_and_pause"),
        )
        ctx = cls(model=model, provider=provider, tracker=tracker, interactive=interactive,
                  role_models=role_models or {}, role_temps=role_temps or {})
        if run_dir:
            ctx.run_dir = Path(run_dir)
        return ctx

    def emit(self, type: str, **fields) -> None:
        if self.events is not None:
            try:
                self.events.emit(type, **fields)
            except Exception:
                pass  # observability must never break the run


def resolve_model(role: str, call_model: Optional[str] = None,
                  ctx: Optional[RunContext] = None) -> str:
    """call arg -> ctx role override -> ctx run override -> run.yaml -> defaults.model."""
    for candidate in (call_model,
                      ctx.role_models.get(role) if ctx else None,
                      ctx.model if ctx else None):
        if candidate:
            validate_model_id(candidate)
            return candidate
    models = run_config().get("models") or {}
    if models.get(role):
        return models[role]
    if models.get("default"):
        return models["default"]
    fallback = (llm_config().get("defaults") or {}).get("model")
    if fallback:
        return fallback
    raise ModelNotSpecified(
        f"no model resolved for role {role!r}: pick one on the run form / pass --model, "
        f"set models.{role} in config/run.yaml, or set defaults.model in config/llm.yaml"
    )


def _provider_config(provider: Optional[str]) -> tuple[str, Dict[str, Any]]:
    """Vault connections (saved via the UI) take precedence over llm.yaml entries."""
    cfg = llm_config()
    name = provider or run_config().get("provider") or (cfg.get("defaults") or {}).get("provider", "default")
    try:
        from src.server.credstore import get_provider_secret
        secret = get_provider_secret(name)
    except Exception:
        secret = None
    if secret:
        return name, {"base_url": secret["base_url"], "api_key": secret["api_key"],
                      "api_style": "anthropic" if secret["type"] == "anthropic" else
                                   ("anthropic" if "anthropic" in secret["base_url"] else "openai")}
    providers = cfg.get("providers") or {}
    if name not in providers:
        raise LLMError(f"provider {name!r} not found — add it under Settings → Providers "
                       f"or in config/llm.yaml (have: {list(providers)})")
    return name, providers[name]


def _api_style(pcfg: Dict[str, Any]) -> str:
    style = pcfg.get("api_style")
    if style:
        return style
    return "anthropic" if "anthropic" in str(pcfg.get("base_url", "")) else "openai"


def _resolve_key(pname: str, pcfg: Dict[str, Any]) -> str:
    api_key = pcfg.get("api_key") or os.environ.get(pcfg.get("api_key_env", "") or "", "")
    if not api_key:
        raise LLMError(
            f"no API key for provider {pname!r}: add it under Settings → Providers, "
            f"or set the {pcfg.get('api_key_env')!r} environment variable"
        )
    return api_key


def list_models(provider: Optional[str] = None) -> List[str]:
    """Models actually available to the provider's key (its list-models endpoint)."""
    pname, pcfg = _provider_config(provider)
    api_key = _resolve_key(pname, pcfg)
    base_url = str(pcfg.get("base_url", "")).rstrip("/")
    style = _api_style(pcfg)
    if style == "anthropic":
        url, headers = f"{base_url}/v1/models", {"x-api-key": api_key,
                                                 "anthropic-version": "2023-06-01"}
    else:
        url, headers = f"{base_url}/models", {"Authorization": f"Bearer {api_key}"}
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code >= 400:
        raise LLMError(f"list-models failed (HTTP {resp.status_code}): {resp.text[:300]}")
    data = resp.json().get("data", [])
    ids = [m.get("id") for m in data if m.get("id")]
    return sorted(ids)


def test_provider(provider: str) -> Dict[str, Any]:
    """Cheap live validation: try list-models; fall back to a 1-token completion
    only when the endpoint simply doesn't exist (auth failures report directly)."""
    try:
        models = list_models(provider)
        return {"ok": True, "detail": f"key valid — {len(models)} models visible", "models": models}
    except LLMError as e:
        err = str(e)
        no_endpoint = ("list-models failed" in err
                       and any(f"HTTP {c}" in err for c in (404, 405, 501)))
        if not no_endpoint:
            return {"ok": False, "detail": err, "models": []}
    # endpoint without /models (some local servers): try a minimal completion
    try:
        llm("ping", role="classify", provider=provider,
            model=(run_config().get("models") or {}).get("classify") or "test")
        return {"ok": True, "detail": "key valid (no list-models endpoint — "
                                      "enter model ids manually)", "models": []}
    except Exception as e:  # noqa: BLE001 — reporting, not handling
        return {"ok": False, "detail": str(e), "models": []}


def llm(prompt: str = "", role: str = "worker", model: Optional[str] = None,
        provider: Optional[str] = None, system: Optional[str] = None,
        messages: Optional[List[Dict[str, str]]] = None,
        ctx: Optional[RunContext] = None, purpose: str = "") -> str:
    """One LLM call. Model is a per-call parameter, changeable every call."""
    if ctx is not None:
        provider = provider or ctx.provider
    cfg = llm_config()
    defaults = cfg.get("defaults") or {}
    role_cfg = (cfg.get("roles") or {}).get(role) or {}
    model_id = resolve_model(role, model, ctx)
    pname, pcfg = _provider_config(provider)
    api_key = _resolve_key(pname, pcfg)

    base_url = str(pcfg.get("base_url", "")).rstrip("/")
    style = _api_style(pcfg)
    temperature = (ctx.role_temps.get(role) if ctx and role in ctx.role_temps
                   else role_cfg.get("temperature", 0.3))
    max_tokens = role_cfg.get("max_tokens", 4000)
    top_p = defaults.get("top_p", 0.95)
    timeout = defaults.get("timeout_seconds", 120)
    retries = int(defaults.get("max_retries", 4))

    msgs = messages or [{"role": "user", "content": prompt}]

    if style == "anthropic":
        url = f"{base_url}/v1/messages"
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        payload: Dict[str, Any] = {"model": model_id, "max_tokens": max_tokens,
                                   "temperature": temperature, "top_p": top_p,
                                   "messages": msgs}
        if system:
            payload["system"] = system
    else:
        url = f"{base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}
        oai_msgs = ([{"role": "system", "content": system}] if system else []) + msgs
        payload = {"model": model_id, "max_tokens": max_tokens,
                   "temperature": temperature, "top_p": top_p, "messages": oai_msgs}

    def _emit(status: str, started: float, tin: int = 0, tout: int = 0,
              response: str = "", error: str = "") -> None:
        if ctx is None:
            return
        cost = (tin / 1e6) * ctx.tracker.price_in + (tout / 1e6) * ctx.tracker.price_out
        ctx.emit("llm_call", role=role, model=model_id, provider=pname, status=status,
                 purpose=purpose, temperature=temperature,
                 duration_ms=int((time.monotonic() - started) * 1000),
                 tokens_in=tin, tokens_out=tout, cost_usd=round(cost, 6),
                 system=system or "", messages=msgs, response=response, error=error)

    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        started = time.monotonic()
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
            if resp.status_code in (429, 500, 502, 503, 529):
                raise LLMError(f"retryable HTTP {resp.status_code}: {resp.text[:300]}")
            if resp.status_code >= 400:
                raise LLMError(f"HTTP {resp.status_code} from {pname}: {resp.text[:500]}")
            data = resp.json()
            if style == "anthropic":
                text = "".join(b.get("text", "") for b in data.get("content", [])
                               if b.get("type") == "text")
                usage = data.get("usage", {})
                tin, tout = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
            else:
                text = data["choices"][0]["message"].get("content") or ""
                usage = data.get("usage", {})
                tin, tout = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
            _emit("ok", started, tin, tout, response=text)
            if ctx is not None:
                ctx.tracker.record(tin, tout)  # may raise BudgetExceeded -> checkpoint_and_pause
            return text
        except BudgetExceeded:
            raise
        except (requests.RequestException, LLMError, KeyError, ValueError) as e:
            last_err = e
            retryable = "retryable" in str(e) or isinstance(e, requests.RequestException)
            if attempt < retries and retryable:
                _emit("retrying", started, error=str(e))
                if ctx is not None:
                    ctx.emit("retry", agent=role, attempt=attempt + 1, of=retries,
                             model=model_id, error=str(e))
                time.sleep(min(2 ** attempt, 30))
                continue
            _emit("error", started, error=str(e))
            raise LLMError(f"LLM call failed (model={model_id}, provider={pname}): {e}") from e
    raise LLMError(f"LLM call failed after {retries + 1} attempts: {last_err}")


def extract_json(text: str) -> Any:
    """Pull the first JSON object/array out of an LLM reply (fences tolerated)."""
    text = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise LLMError(f"no JSON found in LLM reply: {text[:200]!r}")
    decoder = json.JSONDecoder()
    for i in range(start, len(text)):
        if text[i] in "{[":
            try:
                obj, _ = decoder.raw_decode(text, i)
                return obj
            except json.JSONDecodeError:
                continue
    raise LLMError(f"no parseable JSON in LLM reply: {text[:200]!r}")


def llm_json(prompt: str = "", role: str = "worker", model: Optional[str] = None,
             provider: Optional[str] = None, system: Optional[str] = None,
             messages: Optional[List[Dict[str, str]]] = None,
             ctx: Optional[RunContext] = None, purpose: str = "") -> Any:
    """llm() + strict-JSON extraction, with one corrective retry on a malformed reply."""
    text = llm(prompt, role=role, model=model, provider=provider, system=system,
               messages=messages, ctx=ctx, purpose=purpose)
    try:
        return extract_json(text)
    except LLMError:
        if ctx is not None:
            ctx.emit("retry", agent=role, attempt=1, of=1, model="",
                     error="reply was not valid JSON — corrective retry")
        retry_msgs = (messages or [{"role": "user", "content": prompt}]) + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": "That was not valid JSON. Reply with ONLY the strict JSON object, no prose, no fences."},
        ]
        text = llm(role=role, model=model, provider=provider, system=system,
                   messages=retry_msgs, ctx=ctx, purpose=purpose)
        return extract_json(text)
