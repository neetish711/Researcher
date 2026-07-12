"""Cost + budget accounting. A run checkpoints and pauses BEFORE exceeding budget:
llm() records usage here, and a breach raises BudgetExceeded, which agents catch to
save the CaseFile with status=paused_budget (resume: runner --resume runs/<id>).
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Optional


class BudgetExceeded(RuntimeError):
    """Raised when the run would exceed its cost or wall-clock budget."""


class StopRequested(RuntimeError):
    """Raised at a checkpoint when the operator asked the run to stop (server writes
    a stop.flag in the run dir). State is checkpointed; Resume continues from there."""


class CostTracker:
    """Thread-safe (research workers run in parallel) accumulator of LLM spend.

    No model is pinned anywhere, so price-per-token is an assumption:
    LLM_COST_PER_MTOK_INPUT / LLM_COST_PER_MTOK_OUTPUT env vars (USD per million
    tokens), defaulting to a deliberately conservative 5.0 / 15.0.
    """

    def __init__(self, max_cost_usd: Optional[float] = None,
                 on_limit: str = "checkpoint_and_pause") -> None:
        self.max_cost_usd = max_cost_usd
        self.on_limit = on_limit
        self.spent_usd = 0.0
        self.calls = 0
        self._lock = threading.Lock()
        self.price_in = float(os.environ.get("LLM_COST_PER_MTOK_INPUT", "5.0"))
        self.price_out = float(os.environ.get("LLM_COST_PER_MTOK_OUTPUT", "15.0"))

    def record(self, input_tokens: int, output_tokens: int) -> float:
        cost = (input_tokens / 1e6) * self.price_in + (output_tokens / 1e6) * self.price_out
        with self._lock:
            self.spent_usd += cost
            self.calls += 1
            if (self.max_cost_usd is not None and self.spent_usd >= self.max_cost_usd
                    and self.on_limit == "checkpoint_and_pause"):
                raise BudgetExceeded(
                    f"cost budget hit: ${self.spent_usd:.2f} >= ${self.max_cost_usd:.2f} "
                    f"after {self.calls} LLM calls"
                )
        return cost


def parse_duration(text: str) -> float:
    """'4h' | '90m' | '30s' | plain seconds -> seconds."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([hms]?)\s*", str(text))
    if not m:
        raise ValueError(f"cannot parse duration {text!r} (use e.g. 4h, 90m, 30s)")
    value, unit = float(m.group(1)), m.group(2)
    return value * {"h": 3600, "m": 60, "s": 1, "": 1}[unit]


class Deadline:
    """Wall-clock budget for the research loop."""

    def __init__(self, budget: str | float) -> None:
        self.seconds = parse_duration(budget) if isinstance(budget, str) else float(budget)
        self.start = time.monotonic()

    def remaining(self) -> float:
        return self.seconds - (time.monotonic() - self.start)

    def expired(self) -> bool:
        return self.remaining() <= 0

    def check(self) -> None:
        if self.expired():
            raise BudgetExceeded(f"wall-clock budget of {self.seconds:.0f}s exhausted")
