"""Structured, append-only event log per run: runs/<id>/events.jsonl.

Every consequential action emits one JSON line: agent_start/agent_end, llm_call,
tool_call, search_query, page_fetch, doc_read, finding_created, citation_verified,
citation_rejected, worker_start/worker_end, round_complete, gate_waiting, error,
retry, checkpoint_saved. The UI replays the file (survives refresh) and tails it
live over SSE.

Everything passing through here is scrubbed via credstore.redact — an API key can
never appear in an event even if a prompt or error message contains one.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Dict, Iterator, Optional

_TRUNC = 20000  # chars kept for prompts/responses/page bodies in event payloads


def _scrub(value):
    from src.server.credstore import redact
    if isinstance(value, str):
        s = redact(value)
        return s[:_TRUNC] + f"… [truncated {len(s) - _TRUNC} chars]" if len(s) > _TRUNC else s
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


ERROR_CLASSES = [
    ("auth", ["api key", "unauthorized", "401", "invalid x-api-key", "authentication"],
     "Check the provider's API key under Settings → Providers and press Test."),
    ("rate_limit", ["429", "rate limit", "overloaded", "quota"],
     "Slow down or lower max_workers/max_queries in config/research.yaml; retrying usually recovers."),
    ("timeout", ["timeout", "timed out", "read timed out"],
     "Increase defaults.timeout_seconds in config/llm.yaml or retry."),
    ("budget", ["budget", "wall-clock", "cost budget hit"],
     "Raise limits.max_cost_usd_per_run (llm.yaml) or the run budget, then Resume."),
    ("parse", ["not valid json", "no json found", "no parseable json", "validation error"],
     "The model returned malformed output — retry, or retry with a stronger model."),
    ("no_model", ["no model resolved", "modelnotspecified"],
     "Pick a model on the run form (per role) or set one in config/run.yaml."),
    ("no_source", ["no reachable sources", "no results"],
     "Enable more research sources under Settings → Research Sources."),
    ("tool", ["connection", "dns", "ssl", "certificate"],
     "Network/tool failure — usually transient; retry the step."),
]


def classify_error(message: str) -> Dict[str, str]:
    low = (message or "").lower()
    for cls, needles, fix in ERROR_CLASSES:
        if any(n in low for n in needles):
            return {"error_class": cls, "suggested_fix": fix}
    return {"error_class": "unknown",
            "suggested_fix": "Open the event detail for the full request/response, then retry."}


class EventLog:
    """Thread-safe (research workers emit from a pool) append-only jsonl writer."""

    def __init__(self, run_dir: Path | str) -> None:
        self.path = Path(run_dir) / "events.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq = self._count_existing()

    def _count_existing(self) -> int:
        if not self.path.exists():
            return 0
        with self.path.open("rb") as f:
            return sum(1 for _ in f)

    def emit(self, type: str, agent: str = "", status: str = "ok", **fields) -> Dict:
        if "error" in fields and fields["error"]:
            fields.update(classify_error(str(fields["error"])))
        event = {"type": type, "agent": agent, "status": status, "ts": time.time(), **fields}
        event = _scrub(event)
        with self._lock:
            self._seq += 1
            event["seq"] = self._seq
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event


def read_events(run_dir: Path | str, since_seq: int = 0, limit: int = 0) -> list:
    path = Path(run_dir) / "events.jsonl"
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("seq", 0) > since_seq:
                out.append(e)
    return out[-limit:] if limit else out


def tail_events(run_dir: Path | str, since_seq: int = 0,
                poll_s: float = 0.7, stop_after_idle_s: float = 900) -> Iterator[Dict]:
    """Blocking generator for SSE: yields existing events after since_seq, then new
    ones as they are appended. Ends after a long idle period (client reconnects)."""
    seen = since_seq
    idle = 0.0
    while idle < stop_after_idle_s:
        batch = read_events(run_dir, since_seq=seen)
        if batch:
            for e in batch:
                seen = max(seen, e.get("seq", seen))
                yield e
            idle = 0.0
        else:
            time.sleep(poll_s)
            idle += poll_s
