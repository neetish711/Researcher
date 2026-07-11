"""Research source registry: built-in free APIs + user-registered custom HTTP APIs.

Every source — built-in or custom — is described by the same schema:
    request template (URL with {query}) + auth placement + response mapping
    (dot-paths to the results array and to title/url/snippet/date within an item)
so "Test source" exercises exactly the machinery a real run uses. Overrides
(enabled/tier/weight) and custom sources persist in data/sources.json; source API
keys live in the credential vault (credstore), never in this file.

The research agent calls multi_search() instead of hitting one engine directly.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Dict, List, Optional

import requests

from src.server import credstore
from src.server.credstore import DATA_DIR
from src.tools.search import USER_AGENT, web_search

_STORE = DATA_DIR / "sources.json"
_lock = threading.Lock()

TIER_TO_RELIABILITY = {"primary": "high", "secondary": "medium",
                       "vendor": "low", "community": "low"}

BUILTINS: List[Dict] = [
    {"id": "ddg_web", "name": "DuckDuckGo Web", "builtin": True, "tier": "secondary",
     "weight": 1.0, "enabled": True, "free_tier": "unlimited, unauthenticated",
     "auth": {"type": "none"}, "special": "ddg",
     "description": "General web search (ddgs package with HTML fallback)"},
    {"id": "github_repos", "name": "GitHub Repositories", "builtin": True, "tier": "primary",
     "weight": 1.0, "enabled": True, "free_tier": "10 req/min unauthenticated",
     "auth": {"type": "none"},
     "request": {"url": "https://api.github.com/search/repositories?q={query}&per_page={n}"},
     "mapping": {"items": "items", "title": "full_name", "url": "html_url",
                 "snippet": "description", "date": "updated_at"},
     "description": "Open-source tools and frameworks"},
    {"id": "stackoverflow", "name": "Stack Overflow", "builtin": True, "tier": "community",
     "weight": 0.7, "enabled": True, "free_tier": "300 req/day unauthenticated",
     "auth": {"type": "none"},
     "request": {"url": "https://api.stackexchange.com/2.3/search/advanced"
                        "?q={query}&site=stackoverflow&pagesize={n}&order=desc&sort=relevance"},
     "mapping": {"items": "items", "title": "title", "url": "link", "snippet": "",
                 "date": "creation_date"},
     "description": "Practitioner problems and integration pain points"},
    {"id": "hackernews", "name": "Hacker News (Algolia)", "builtin": True, "tier": "community",
     "weight": 0.6, "enabled": True, "free_tier": "unlimited, unauthenticated",
     "auth": {"type": "none"},
     "request": {"url": "https://hn.algolia.com/api/v1/search?query={query}&hitsPerPage={n}"},
     "mapping": {"items": "hits", "title": "title", "url": "url",
                 "snippet": "story_text", "date": "created_at"},
     "description": "Launches, reviews, war stories"},
    {"id": "npm", "name": "npm registry", "builtin": True, "tier": "primary",
     "weight": 0.8, "enabled": False, "free_tier": "unlimited, unauthenticated",
     "auth": {"type": "none"},
     "request": {"url": "https://registry.npmjs.org/-/v1/search?text={query}&size={n}"},
     "mapping": {"items": "objects", "title": "package.name", "url": "package.links.npm",
                 "snippet": "package.description", "date": "package.date"},
     "description": "JS ecosystem packages (enable for full-code research)"},
    {"id": "reddit", "name": "Reddit search", "builtin": True, "tier": "community",
     "weight": 0.5, "enabled": False, "free_tier": "unauthenticated, strict rate limits",
     "auth": {"type": "none"},
     "request": {"url": "https://www.reddit.com/search.json?q={query}&limit={n}"},
     "mapping": {"items": "data.children", "title": "data.title",
                 "url": "data.url", "snippet": "data.selftext", "date": "data.created_utc"},
     "description": "User sentiment; disabled by default (noisy)"},
]

DEFAULTS = {"rate_limit_per_min": 30, "timeout_s": 20}


def _load_store() -> Dict:
    if _STORE.exists():
        return json.loads(_STORE.read_text(encoding="utf-8"))
    return {"overrides": {}, "custom": {}}


def _save_store(data: Dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _STORE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def list_sources() -> List[Dict]:
    store = _load_store()
    out = []
    for b in BUILTINS:
        merged = {**DEFAULTS, **b, **store["overrides"].get(b["id"], {})}
        merged["has_key"] = credstore.source_secret_fingerprint(b["id"]) is not None
        merged["key_fingerprint"] = credstore.source_secret_fingerprint(b["id"]) or ""
        out.append(merged)
    for sid, c in sorted(store["custom"].items()):
        merged = {**DEFAULTS, **c, "id": sid, "builtin": False}
        merged["has_key"] = credstore.source_secret_fingerprint(sid) is not None
        merged["key_fingerprint"] = credstore.source_secret_fingerprint(sid) or ""
        out.append(merged)
    return out


def get_source(source_id: str) -> Optional[Dict]:
    return next((s for s in list_sources() if s["id"] == source_id), None)


def update_source(source_id: str, patch: Dict) -> Dict:
    """Toggle/tier/weight/rate/timeout for builtins; full definition for customs."""
    allowed = {"enabled", "tier", "weight", "rate_limit_per_min", "timeout_s", "free_tier"}
    with _lock:
        store = _load_store()
        if any(b["id"] == source_id for b in BUILTINS):
            ov = store["overrides"].setdefault(source_id, {})
            ov.update({k: v for k, v in patch.items() if k in allowed})
        elif source_id in store["custom"]:
            store["custom"][source_id].update(
                {k: v for k, v in patch.items() if k != "id"})
        else:
            raise KeyError(f"unknown source {source_id!r}")
        _save_store(store)
    src = get_source(source_id)
    assert src is not None
    return src


def add_custom_source(defn: Dict) -> Dict:
    """defn: {id, name, request:{url with {query}}, auth:{type, header?/param?},
    mapping:{items,title,url,snippet,date}, tier, weight, api_key?}"""
    sid = defn.get("id") or defn["name"].lower().replace(" ", "_")
    if any(b["id"] == sid for b in BUILTINS):
        raise ValueError(f"{sid!r} clashes with a built-in source id")
    if "{query}" not in (defn.get("request") or {}).get("url", ""):
        raise ValueError("request.url must contain a {query} placeholder")
    api_key = defn.pop("api_key", None)
    if api_key:
        credstore.save_source_secret(sid, api_key)
    with _lock:
        store = _load_store()
        store["custom"][sid] = {
            "name": defn.get("name", sid), "tier": defn.get("tier", "secondary"),
            "weight": float(defn.get("weight", 1.0)), "enabled": defn.get("enabled", True),
            "auth": defn.get("auth", {"type": "none"}), "request": defn["request"],
            "mapping": defn.get("mapping", {}), "free_tier": defn.get("free_tier", ""),
            "description": defn.get("description", ""),
            "rate_limit_per_min": defn.get("rate_limit_per_min", DEFAULTS["rate_limit_per_min"]),
            "timeout_s": defn.get("timeout_s", DEFAULTS["timeout_s"]),
        }
        _save_store(store)
    src = get_source(sid)
    assert src is not None
    return src


def delete_source(source_id: str) -> bool:
    with _lock:
        store = _load_store()
        existed = store["custom"].pop(source_id, None) is not None
        store["overrides"].pop(source_id, None)
        _save_store(store)
    credstore.delete_source_secret(source_id)
    return existed


def _dig(obj, path: str):
    """Tiny dot-path resolver: 'data.children' / 'package.links.npm'."""
    cur = obj
    for part in [p for p in (path or "").split(".") if p]:
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            cur = cur[int(part)] if int(part) < len(cur) else None
        else:
            return None
    return cur


# per-source naive rate limiting (process-local)
_last_call: Dict[str, float] = {}
_rate_lock = threading.Lock()


def _throttle(source: Dict) -> None:
    import time
    per_min = max(1, int(source.get("rate_limit_per_min", 30)))
    min_gap = 60.0 / per_min
    with _rate_lock:
        last = _last_call.get(source["id"], 0.0)
        wait = min_gap - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        _last_call[source["id"]] = time.monotonic()


def search_source(source: Dict, query: str, n: int = 6,
                  raw: bool = False) -> Dict:
    """Run one query through one source. -> {results: [...], raw?: ..., error?: str}"""
    if source.get("special") == "ddg":
        try:
            return {"results": [dict(r, source=source["id"], tier=source["tier"])
                                for r in web_search(query, max_results=n)]}
        except Exception as e:  # noqa: BLE001
            return {"results": [], "error": str(e)}
    _throttle(source)
    req = source.get("request") or {}
    url = req.get("url", "").replace("{query}", requests.utils.quote(query)).replace("{n}", str(n))
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    params = {}
    auth = source.get("auth") or {"type": "none"}
    key = credstore.get_source_secret(source["id"]) if auth.get("type") != "none" else None
    if key:
        if auth["type"] == "api-key-header":
            headers[auth.get("header", "X-Api-Key")] = key
        elif auth["type"] == "bearer":
            headers["Authorization"] = f"Bearer {key}"
        elif auth["type"] == "query-param":
            params[auth.get("param", "api_key")] = key
    try:
        resp = requests.get(url, headers=headers, params=params,
                            timeout=source.get("timeout_s", 20))
        body = resp.json()
    except Exception as e:  # noqa: BLE001
        return {"results": [], "error": f"{type(e).__name__}: {e}"}
    if resp.status_code >= 400:
        return {"results": [], "error": f"HTTP {resp.status_code}: {str(body)[:300]}",
                **({"raw": body} if raw else {})}
    mapping = source.get("mapping") or {}
    items = _dig(body, mapping.get("items", "")) or []
    results = []
    for item in items[:n]:
        r = {"title": str(_dig(item, mapping.get("title", "")) or ""),
             "url": str(_dig(item, mapping.get("url", "")) or ""),
             "snippet": str(_dig(item, mapping.get("snippet", "")) or "")[:400],
             "date": str(_dig(item, mapping.get("date", "")) or ""),
             "source": source["id"], "tier": source.get("tier", "secondary")}
        if r["url"].startswith("http"):
            results.append(r)
    out = {"results": results}
    if raw:
        out["raw"] = body
    return out


def multi_search(query: str, n_per_source: int = 6,
                 only: Optional[List[str]] = None,
                 events=None) -> List[Dict]:
    """Fan a query across every enabled (and per-run selected) source; merge + dedupe."""
    hits: Dict[str, Dict] = {}
    for source in list_sources():
        if not source.get("enabled"):
            continue
        if only is not None and source["id"] not in only:
            continue
        result = search_source(source, query, n=n_per_source)
        if events is not None:
            try:
                events.emit("search_query", agent="research", source=source["id"],
                            query=query, results=len(result.get("results", [])),
                            status="error" if result.get("error") else "ok",
                            error=result.get("error", ""))
            except Exception:
                pass
        for r in result.get("results", []):
            if r["url"] not in hits:
                hits[r["url"]] = r
    return list(hits.values())
