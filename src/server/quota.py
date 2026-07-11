"""QuotaManager — sits in front of EVERY source adapter; no adapter may be called
except through the router, and the router consults this first.

- Per-provider budget ledger persisted across runs (SQLite at DATA_DIR/quota.db):
  monthly quota, units consumed (in the provider's own unit), reset date.
- Pre-flight check: a call that would exceed the remaining free quota is REFUSED
  (QuotaExceeded), never attempted. Unknown quotas get a conservative assumed cap
  until verified.
- free_tier_only (config/sources.yaml, default true): with quota exhausted there is
  no "try and see" path — the refusal happens before any HTTP request.
- Token-bucket rate limiting per provider (rps + rpm) and 429 backoff hinting.
- Reservations: a deep run reserves its estimated calls up front so parallel runs
  can't jointly blow a free tier; leftovers are released when the run ends.
"""
from __future__ import annotations

import math
import sqlite3
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, Optional

import yaml

from src.tools.models import CONFIG_DIR


class QuotaExceeded(RuntimeError):
    """Raised BEFORE any HTTP call would exceed a free tier."""


def _override_path() -> Path:
    from src.server.credstore import DATA_DIR
    return DATA_DIR / "sources_override.json"


def sources_config() -> dict:
    p = CONFIG_DIR / "sources.yaml"
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    ov = _override_path()
    if ov.exists():
        try:
            import json
            cfg.update({k: v for k, v in json.loads(ov.read_text(encoding="utf-8")).items()
                        if k == "free_tier_only"})
        except Exception:
            pass
    return cfg


def set_free_tier_only(value: bool) -> None:
    import json
    p = _override_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"free_tier_only": bool(value)}), encoding="utf-8")


def _next_month_first(today: Optional[date] = None) -> str:
    d = today or date.today()
    return (date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)).isoformat()


class _Bucket:
    """Token bucket enforcing rps and rpm."""

    def __init__(self, rps: float, rpm: float) -> None:
        self.min_gap = 1.0 / max(rps, 0.01)
        self.rpm = max(1, int(rpm))
        self.stamps: list[float] = []
        self.last = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                self.stamps = [t for t in self.stamps if now - t < 60]
                gap_wait = self.min_gap - (now - self.last)
                rpm_wait = (60 - (now - self.stamps[0])) if len(self.stamps) >= self.rpm else 0
                wait = max(gap_wait, rpm_wait, 0)
                if wait <= 0:
                    self.last = now
                    self.stamps.append(now)
                    return
            time.sleep(min(wait, 5.0))


class QuotaManager:
    def __init__(self, db_path: Optional[Path] = None) -> None:
        from src.server.credstore import DATA_DIR
        self.cfg = sources_config()
        self.free_tier_only = bool(self.cfg.get("free_tier_only", True))
        path = db_path or (DATA_DIR / "quota.db")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("""CREATE TABLE IF NOT EXISTS ledger(
            provider TEXT PRIMARY KEY, used REAL NOT NULL DEFAULT 0,
            reset_date TEXT NOT NULL, verified INTEGER NOT NULL DEFAULT 0,
            verified_note TEXT DEFAULT '')""")
        self._db.execute("""CREATE TABLE IF NOT EXISTS reservations(
            run_id TEXT NOT NULL, provider TEXT NOT NULL, units REAL NOT NULL,
            PRIMARY KEY (run_id, provider))""")
        self._db.commit()
        self._lock = threading.Lock()
        self._buckets: Dict[str, _Bucket] = {}

    # ── config helpers ────────────────────────────────────────────────────
    def provider_cfg(self, provider: str) -> dict:
        return (self.cfg.get("providers") or {}).get(provider) or {}

    def quota_of(self, provider: str) -> Optional[float]:
        """Enforceable monthly cap: declared quota, else the conservative assumed
        cap for unverified providers, else None (truly uncapped = keyless)."""
        pc = self.provider_cfg(provider)
        if not pc:
            return None  # keyless/custom source — no billing risk
        if pc.get("monthly_quota") is not None:
            return float(pc["monthly_quota"])
        return float(pc.get("assumed_quota", 100))

    # ── ledger ────────────────────────────────────────────────────────────
    def _row(self, provider: str) -> tuple[float, str, int, str]:
        with self._lock:
            cur = self._db.execute(
                "SELECT used, reset_date, verified, verified_note FROM ledger WHERE provider=?",
                (provider,))
            row = cur.fetchone()
            if row is None:
                row = (0.0, _next_month_first(), 0, "")
                self._db.execute(
                    "INSERT INTO ledger(provider, used, reset_date) VALUES (?,?,?)",
                    (provider, 0.0, row[1]))
                self._db.commit()
            used, reset_date, verified, note = row
            if date.today().isoformat() >= reset_date:  # monthly rollover
                used = 0.0
                reset_date = _next_month_first()
                self._db.execute("UPDATE ledger SET used=0, reset_date=? WHERE provider=?",
                                 (reset_date, provider))
                self._db.commit()
            return float(used), reset_date, int(verified), note or ""

    def _reserved_elsewhere(self, provider: str, run_id: Optional[str]) -> float:
        cur = self._db.execute(
            "SELECT COALESCE(SUM(units),0) FROM reservations WHERE provider=? AND run_id<>?",
            (provider, run_id or ""))
        return float(cur.fetchone()[0])

    def status(self, provider: str) -> dict:
        used, reset_date, verified, note = self._row(provider)
        quota = self.quota_of(provider)
        pc = self.provider_cfg(provider)
        remaining = None if quota is None else max(0.0, quota - used)
        return {"provider": provider, "unit": pc.get("unit", "call"), "used": round(used, 1),
                "monthly_quota": quota, "remaining": None if remaining is None else round(remaining, 1),
                "resets_on": reset_date, "quota_verified": bool(verified),
                "verified_note": note,
                "assumed": pc.get("monthly_quota") is None and bool(pc)}

    def mark_verified(self, provider: str, note: str = "") -> None:
        self._row(provider)
        with self._lock:
            self._db.execute("UPDATE ledger SET verified=1, verified_note=? WHERE provider=?",
                             (note[:200], provider))
            self._db.commit()

    # ── the pre-flight gate ───────────────────────────────────────────────
    def preflight(self, provider: str, est_units: float = 1.0,
                  run_id: Optional[str] = None) -> None:
        """Refuse (raise) BEFORE the call if it could exceed the free tier."""
        quota = self.quota_of(provider)
        if quota is None:
            return  # keyless — no billing possible
        used, _, _, _ = self._row(provider)
        with self._lock:
            reserved = self._reserved_elsewhere(provider, run_id)
        remaining = quota - used - reserved
        if est_units > remaining:
            pc = self.provider_cfg(provider)
            raise QuotaExceeded(
                f"{provider}: call refused pre-flight — needs ~{est_units:g} {pc.get('unit','unit')}(s), "
                f"{max(remaining,0):g} left of {quota:g}/month"
                + (" (assumed cap — verify the real limit to raise it)" if pc.get("monthly_quota") is None else "")
                + ". free_tier_only forbids overage." if self.free_tier_only else ".")

    def throttle(self, provider: str) -> None:
        pc = self.provider_cfg(provider)
        rl = pc.get("rate_limit") or {}
        with self._lock:
            if provider not in self._buckets:
                self._buckets[provider] = _Bucket(rl.get("rps", 1), rl.get("rpm", 30))
        self._buckets[provider].wait()

    def consume(self, provider: str, units: float, run_id: Optional[str] = None) -> dict:
        """Record actual usage after a successful call; draws down this run's reservation."""
        self._row(provider)
        with self._lock:
            self._db.execute("UPDATE ledger SET used = used + ? WHERE provider=?",
                             (units, provider))
            if run_id:
                self._db.execute(
                    "UPDATE reservations SET units = MAX(units - ?, 0) WHERE run_id=? AND provider=?",
                    (units, run_id, provider))
            self._db.commit()
        return self.status(provider)

    # ── reservations for deep runs ────────────────────────────────────────
    def reserve(self, run_id: str, estimates: Dict[str, float]) -> None:
        with self._lock:
            for provider, units in estimates.items():
                if self.quota_of(provider) is None:
                    continue
                self._db.execute(
                    "INSERT OR REPLACE INTO reservations(run_id, provider, units) VALUES (?,?,?)",
                    (run_id, provider, units))
            self._db.commit()

    def release(self, run_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM reservations WHERE run_id=?", (run_id,))
            self._db.commit()

    # ── forecast (pre-run) ────────────────────────────────────────────────
    def estimate_run(self, research_cfg: dict) -> Dict[str, float]:
        """Upper-bound source calls per provider for one deep run, from research.yaml."""
        b = research_cfg.get("budget") or {}
        q = research_cfg.get("queries") or {}
        rounds = int(b.get("max_rounds", 6))
        workers = int(b.get("max_workers", 4))
        queries = int(q.get("max_queries_per_round", 12))
        pages = int(b.get("max_tool_calls_per_worker", 40)) // 2
        searches = rounds * workers * queries          # upper bound before dedup/cache
        reads = rounds * workers * pages
        return {
            "tavily": math.ceil(searches * 0.5),       # primary search for most workers
            "zenserp": math.ceil(searches * 0.3),
            "jina": reads * 3000,                      # tokens (~3k/page) — default extractor
            "firecrawl": math.ceil(reads * 0.15),      # only when Jina fails / JS-heavy
            "tinyfish": math.ceil(rounds * 3),         # structured pricing extractions (saas)
            "algolia_hn": rounds * workers * 2,
        }

    def forecast(self, research_cfg: dict) -> list[dict]:
        out = []
        for provider, est in self.estimate_run(research_cfg).items():
            st = self.status(provider)
            st["estimated_units"] = est
            st["would_exceed"] = (st["remaining"] is not None and est > st["remaining"])
            out.append(st)
        return out
