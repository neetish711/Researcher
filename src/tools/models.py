"""LLM access. Nothing about a specific model is hardcoded anywhere in this repo:

- a ROLE (config/llm.yaml `roles`) supplies only temperature + max_tokens;
- a PROVIDER supplies the key (from its api_key_env) + base_url;
- the MODEL comes from the call, resolution order:
      call arg  ->  config/run.yaml models[role] / models.default  ->  llm.yaml defaults.model
  If nothing resolves, fail fast and ask for one (ModelNotSpecified).

Works against the Anthropic messages API or any OpenAI-compatible endpoint; the wire
format is chosen from the provider's base_url (override with `api_style` on the provider).
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
def load_yaml(path: str) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def llm_config() -> Dict[str, Any]:
    return load_yaml(str(CONFIG_DIR / "llm.yaml"))


def run_config() -> Dict[str, Any]:
    return load_yaml(str(CONFIG_DIR / "run.yaml"))


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


@dataclass
class RunContext:
    """Per-run knobs threaded through every agent (standalone and orchestrated)."""
    model: Optional[str] = None          # --model flag: overrides everything for every call
    provider: Optional[str] = None       # --provider flag
    run_dir: Path = field(default_factory=lambda: REPO_ROOT / "runs" / "adhoc")
    tracker: CostTracker = field(default_factory=CostTracker)
    interactive: bool = True             # False = human gates are skipped (unattended)
    prior_cost_usd: float = 0.0          # spend from earlier sessions of a resumed run
    prior_llm_calls: int = 0

    @classmethod
    def create(cls, model: Optional[str] = None, provider: Optional[str] = None,
               run_dir: Optional[Path | str] = None, interactive: bool = True) -> "RunContext":
        limits = llm_config().get("limits", {}) or {}
        tracker = CostTracker(
            max_cost_usd=limits.get("max_cost_usd_per_run"),
            on_limit=limits.get("on_limit", "checkpoint_and_pause"),
        )
        ctx = cls(model=model, provider=provider, tracker=tracker, interactive=interactive)
        if run_dir:
            ctx.run_dir = Path(run_dir)
        return ctx


def resolve_model(role: str, call_model: Optional[str] = None) -> str:
    """call arg -> run config -> defaults.model, else fail fast."""
    if call_model:
        return call_model
    models = run_config().get("models") or {}
    if models.get(role):
        return models[role]
    if models.get("default"):
        return models["default"]
    fallback = (llm_config().get("defaults") or {}).get("model")
    if fallback:
        return fallback
    raise ModelNotSpecified(
        f"no model resolved for role {role!r}: pass --model on the CLI, set models.{role} "
        "in config/run.yaml, or set defaults.model in config/llm.yaml"
    )


def _provider_config(provider: Optional[str]) -> tuple[str, Dict[str, Any]]:
    cfg = llm_config()
    name = provider or run_config().get("provider") or (cfg.get("defaults") or {}).get("provider", "default")
    providers = cfg.get("providers") or {}
    if name not in providers:
        raise LLMError(f"provider {name!r} not found in config/llm.yaml (have: {list(providers)})")
    return name, providers[name]


def _api_style(pcfg: Dict[str, Any]) -> str:
    style = pcfg.get("api_style")
    if style:
        return style
    return "anthropic" if "anthropic" in str(pcfg.get("base_url", "")) else "openai"


def llm(prompt: str = "", role: str = "worker", model: Optional[str] = None,
        provider: Optional[str] = None, system: Optional[str] = None,
        messages: Optional[List[Dict[str, str]]] = None,
        ctx: Optional[RunContext] = None) -> str:
    """One LLM call. Model is a per-call parameter, changeable every call."""
    if ctx is not None:
        model = model or ctx.model
        provider = provider or ctx.provider
    cfg = llm_config()
    defaults = cfg.get("defaults") or {}
    role_cfg = (cfg.get("roles") or {}).get(role) or {}
    model_id = resolve_model(role, model)
    pname, pcfg = _provider_config(provider)

    api_key = os.environ.get(pcfg.get("api_key_env", ""), "")
    if not api_key:
        raise LLMError(
            f"no API key: set the {pcfg.get('api_key_env')!r} environment variable "
            f"(provider {pname!r} in config/llm.yaml)"
        )

    base_url = str(pcfg.get("base_url", "")).rstrip("/")
    style = _api_style(pcfg)
    temperature = role_cfg.get("temperature", 0.3)
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

    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
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
            if ctx is not None:
                ctx.tracker.record(tin, tout)  # may raise BudgetExceeded -> checkpoint_and_pause
            return text
        except BudgetExceeded:
            raise
        except (requests.RequestException, LLMError, KeyError, ValueError) as e:
            last_err = e
            if attempt < retries and ("retryable" in str(e) or isinstance(e, requests.RequestException)):
                time.sleep(min(2 ** attempt, 30))
                continue
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
             ctx: Optional[RunContext] = None) -> Any:
    """llm() + strict-JSON extraction, with one corrective retry on a malformed reply."""
    text = llm(prompt, role=role, model=model, provider=provider, system=system,
               messages=messages, ctx=ctx)
    try:
        return extract_json(text)
    except LLMError:
        retry_msgs = (messages or [{"role": "user", "content": prompt}]) + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": "That was not valid JSON. Reply with ONLY the strict JSON object, no prose, no fences."},
        ]
        text = llm(role=role, model=model, provider=provider, system=system,
                   messages=retry_msgs, ctx=ctx)
        return extract_json(text)
