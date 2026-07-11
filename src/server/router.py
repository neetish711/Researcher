"""Router: the ONLY path from the research workers to any source.

Design constraint from the brief: tool-selection accuracy degrades with tool
count, so each category worker sees just `search()` and `read()`; the router
picks providers from a small curated chain per worker. The LLM never sees the
provider list.

- Keyless primaries first (free AND the best citations), then the keyed chain,
  ending in the free DuckDuckGo/builtin fallbacks — so a dead key mid-run
  degrades transparently instead of crashing (logged as a fallback event).
- QuotaManager pre-flight before every keyed call; QuotaExceeded never reaches
  the network.
- Aggressive persistent cache (SQLite, shared with the ledger DB): every query
  and every page is content-hashed; a repeat costs ZERO quota, in-run or
  cross-run. Duplicate queries are dropped before dispatch.
- Every attempt emits a `source_call` event: provider, endpoint, query/url,
  units consumed + remaining, latency, cache hit/miss, fallback_from, error.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from typing import Dict, List, Optional

from src.server import adapters
from src.server.adapters import (AdapterAuthError, AdapterError, BlockedEndpoint,
                                 READ_ADAPTERS, SEARCH_ADAPTERS)
from src.server.quota import QuotaExceeded, QuotaManager, sources_config

# per-worker curated chains (fallback order). The saas worker prefers the SERP +
# Firecrawl path (JS-heavy vendor sites); full-code leans on the code registries.
SEARCH_ROUTES: Dict[str, List[str]] = {
    "no_code":  ["wikipedia", "tavily", "zenserp", "ddg_web"],
    "low_code":  ["tavily", "zenserp", "ddg_web"],
    "full_code": ["github", "pypi", "npm", "openalex", "semantic_scholar", "arxiv",
                  "tavily", "zenserp", "ddg_web"],
    "saas":      ["zenserp", "tavily", "crossref", "ddg_web"],
}
EVIDENCE_ROUTES: Dict[str, List[str]] = {   # community color, queried alongside
    "no_code": ["algolia_hn"], "low_code": ["algolia_hn"],
    "full_code": ["algolia_hn"], "saas": ["algolia_hn"],
}
READ_ROUTES: Dict[str, List[str]] = {
    "saas":     ["firecrawl", "jina", "builtin"],   # JS-heavy vendor sites
    "default":  ["jina", "firecrawl", "builtin"],   # Jina = default extractor (biggest tier)
}


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:32]


class Cache:
    """Cross-run persistent query + page cache — the single biggest free-tier saver."""

    def __init__(self, db_path=None) -> None:
        from src.server.credstore import DATA_DIR
        path = db_path or (DATA_DIR / "quota.db")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("""CREATE TABLE IF NOT EXISTS query_cache(
            k TEXT PRIMARY KEY, provider TEXT, results TEXT, ts REAL)""")
        self._db.execute("""CREATE TABLE IF NOT EXISTS page_cache(
            k TEXT PRIMARY KEY, url TEXT, text TEXT, provider TEXT, ts REAL)""")
        self._db.commit()
        self._lock = threading.Lock()

    def get_query(self, provider: str, query: str) -> Optional[list]:
        with self._lock:
            row = self._db.execute("SELECT results FROM query_cache WHERE k=?",
                                   (_h(f"{provider}::{query.lower().strip()}"),)).fetchone()
        return json.loads(row[0]) if row else None

    def put_query(self, provider: str, query: str, results: list) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO query_cache VALUES (?,?,?,?)",
                             (_h(f"{provider}::{query.lower().strip()}"), provider,
                              json.dumps(results), time.time()))
            self._db.commit()

    def get_page(self, url: str) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT text FROM page_cache WHERE k=?", (_h(url),)).fetchone()
        return row[0] if row else None

    def put_page(self, url: str, text: str, provider: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO page_cache VALUES (?,?,?,?,?)",
                             (_h(url), url, text, provider, time.time()))
            self._db.commit()

    def page_provider(self, url: str) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT provider FROM page_cache WHERE k=?", (_h(url),)).fetchone()
        return row[0] if row else None


class RouterSession:
    """One per run: holds the quota manager, cache, per-run query dedup, and the
    event emitter. Reserves the run's estimated calls up front; release() when done."""

    def __init__(self, ctx, research_cfg: Optional[dict] = None) -> None:
        self.ctx = ctx
        self.run_id = getattr(ctx, "run_dir", None) and ctx.run_dir.name or None
        self.quota = QuotaManager()
        self.cache = Cache()
        self.seen_queries: set = set()
        self._lock = threading.Lock()
        if research_cfg:
            try:
                self.quota.reserve(self.run_id or "adhoc", self.quota.estimate_run(research_cfg))
            except Exception:
                pass

    def release(self) -> None:
        try:
            self.quota.release(self.run_id or "adhoc")
        except Exception:
            pass

    def _emit(self, **fields) -> None:
        self.ctx.emit("source_call", agent="research", **fields)

    # ── search ────────────────────────────────────────────────────────────
    def search(self, worker: str, query: str, n: int = 6,
               force_provider: Optional[str] = None) -> List[dict]:
        norm = " ".join(query.lower().split())
        with self._lock:
            if norm in self.seen_queries and not force_provider:
                self._emit(provider="router", endpoint="search", query=query,
                           cache="dedup", units=0, status="ok",
                           note="duplicate query dropped before dispatch")
                return []
            self.seen_queries.add(norm)

        chain = ([force_provider] if force_provider else
                 SEARCH_ROUTES.get(worker, SEARCH_ROUTES["low_code"]))
        merged: Dict[str, dict] = {}
        fallback_from = ""
        for provider in chain:
            got = self._search_one(provider, query, n, fallback_from)
            if got is None:            # provider failed → try the next
                fallback_from = provider
                continue
            for r in got:
                merged.setdefault(r["url"], r)
            if len(merged) >= n:
                break
        # community evidence rides along (never the sole basis — weighted 0.4)
        if not force_provider:
            for provider in EVIDENCE_ROUTES.get(worker, []):
                got = self._search_one(provider, query, max(2, n // 3), "")
                for r in got or []:
                    merged.setdefault(r["url"], r)
        return list(merged.values())[: n * 2]

    def _search_one(self, provider: str, query: str, n: int,
                    fallback_from: str) -> Optional[List[dict]]:
        fn = SEARCH_ADAPTERS.get(provider)
        if fn is None:
            return None
        cached = self.cache.get_query(provider, query)
        if cached is not None:
            self._emit(provider=provider, endpoint="search", query=query, cache="hit",
                       units=0, remaining=self.quota.status(provider).get("remaining"),
                       results=len(cached), status="ok",
                       fallback_from=fallback_from or None)
            return cached
        started = time.monotonic()
        try:
            self.quota.preflight(provider, 1, self.run_id)
            self.quota.throttle(provider)
            out = fn(query, n)
            results = out["results"]
            st = self.quota.consume(provider, out.get("units", 1), self.run_id)
            self.cache.put_query(provider, query, results)
            self._emit(provider=provider, endpoint="search", query=query, cache="miss",
                       units=out.get("units", 1), remaining=st.get("remaining"),
                       latency_ms=int((time.monotonic() - started) * 1000),
                       results=len(results), status="ok",
                       fallback_from=fallback_from or None)
            return results
        except (QuotaExceeded, AdapterAuthError, AdapterError, BlockedEndpoint, Exception) as e:
            self._emit(provider=provider, endpoint="search", query=query, cache="miss",
                       units=0, latency_ms=int((time.monotonic() - started) * 1000),
                       status="error", error=str(e),
                       fallback_from=fallback_from or None,
                       fallback_to="next in chain")
            return None

    # ── read / extract ────────────────────────────────────────────────────
    def read(self, worker: str, url: str, force_provider: Optional[str] = None) -> str:
        cached = self.cache.get_page(url)
        if cached is not None and not force_provider:
            self._emit(provider=self.cache.page_provider(url) or "cache", endpoint="read",
                       url=url, cache="hit", units=0, chars=len(cached), status="ok")
            return cached
        chain = ([force_provider] if force_provider else
                 READ_ROUTES.get(worker, READ_ROUTES["default"]))
        fallback_from = ""
        for provider in chain:
            fn = READ_ADAPTERS.get(provider)
            if fn is None:
                continue
            started = time.monotonic()
            try:
                if provider != "builtin":
                    self.quota.preflight(provider, 3000 if provider == "jina" else 1, self.run_id)
                    self.quota.throttle(provider)
                out = fn(url)
                text = out["text"]
                st = (self.quota.consume(provider, out.get("units", 1), self.run_id)
                      if provider != "builtin" else {"remaining": None})
                self.cache.put_page(url, text, provider)
                self._emit(provider=provider, endpoint="read", url=url, cache="miss",
                           units=out.get("units", 0), remaining=st.get("remaining"),
                           latency_ms=int((time.monotonic() - started) * 1000),
                           chars=len(text), status="ok",
                           fallback_from=fallback_from or None)
                return text
            except (QuotaExceeded, AdapterAuthError, AdapterError, Exception) as e:
                self._emit(provider=provider, endpoint="read", url=url, cache="miss",
                           units=0, latency_ms=int((time.monotonic() - started) * 1000),
                           status="error", error=str(e),
                           fallback_from=fallback_from or None, fallback_to="next in chain")
                fallback_from = provider
        return ""

    def extract_structured(self, url: str, instruction: str) -> str:
        """TinyFish, only when structured extraction is actually needed (saas pricing)."""
        started = time.monotonic()
        try:
            self.quota.preflight("tinyfish", 1, self.run_id)
            self.quota.throttle("tinyfish")
            out = adapters.tinyfish_extract(url, instruction)
            st = self.quota.consume("tinyfish", out.get("units", 1), self.run_id)
            self._emit(provider="tinyfish", endpoint="extract", url=url, cache="miss",
                       units=out.get("units", 1), remaining=st.get("remaining"),
                       latency_ms=int((time.monotonic() - started) * 1000), status="ok")
            return out["text"]
        except Exception as e:  # noqa: BLE001 — optional enrichment, never fatal
            self._emit(provider="tinyfish", endpoint="extract", url=url, units=0,
                       status="error", error=str(e))
            return ""


def usage_summary(events: List[dict], findings: List[dict]) -> dict:
    """End-of-run source-usage summary from the event stream."""
    calls: Dict[str, dict] = {}
    url_provider: Dict[str, str] = {}
    for e in events:
        if e.get("type") != "source_call":
            continue
        p = e.get("provider", "?")
        c = calls.setdefault(p, {"calls": 0, "errors": 0, "cache_hits": 0,
                                 "units": 0.0, "findings": 0})
        c["calls"] += 1
        if e.get("status") == "error":
            c["errors"] += 1
        if e.get("cache") == "hit":
            c["cache_hits"] += 1
        c["units"] += float(e.get("units") or 0)
        if e.get("url") and e.get("status") == "ok":
            url_provider[e["url"]] = p
    for f in findings:
        p = url_provider.get(f.get("url", ""))
        if p and p in calls:
            calls[p]["findings"] += 1
    total = sum(c["calls"] for c in calls.values())
    hits = sum(c["cache_hits"] for c in calls.values())
    return {"providers": calls, "total_calls": total,
            "cache_hit_rate": round(hits / total, 3) if total else 0.0}
