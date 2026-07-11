"""Source adapters: six keyed providers + the keyless primaries.

Rules enforced here:
- NEVER called directly by agents — only the router calls these, and the router
  consults the QuotaManager first.
- Keys come from the vault (credstore, saved via the UI) or the provider's
  key_env environment variable — never from arguments, config files, or code.
- Tavily: only the `search` endpoint exists here; anything else raises
  BlockedEndpoint (its /research endpoint can burn ~250 credits in one call).
- Every adapter returns a normalized shape and reports the units it consumed
  in the provider's own unit (credits / queries / tokens / requests).

search adapters -> {"results": [{title,url,snippet,source,tier}], "units": n}
read adapters   -> {"text": str, "units": n}
"""
from __future__ import annotations

import math
import os
import re
from typing import Dict, List, Optional

import requests

from src.server import credstore
from src.server.quota import sources_config
from src.tools.search import USER_AGENT, fetch_page, web_search


class BlockedEndpoint(RuntimeError):
    """A billable/expensive endpoint that is disabled by policy."""


class AdapterAuthError(RuntimeError):
    """Invalid/missing key — the router treats this as 'fall back to the next provider'."""


class AdapterError(RuntimeError):
    pass


def get_key(provider: str) -> Optional[str]:
    """Vault first (Settings → Research Sources), then the configured env var."""
    key = credstore.get_source_secret(provider)
    if key:
        return key
    pc = (sources_config().get("providers") or {}).get(provider) or {}
    return os.environ.get(pc.get("key_env", "") or "") or None


def _mailto() -> str:
    env = sources_config().get("mailto_env", "CONTACT_EMAIL")
    return os.environ.get(env, "") or "o2s-copilot@example.com"


def _get(url: str, *, headers=None, params=None, timeout=25) -> requests.Response:
    h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    h.update(headers or {})
    resp = requests.get(url, headers=h, params=params, timeout=timeout)
    if resp.status_code in (401, 403):
        raise AdapterAuthError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    if resp.status_code == 429:
        raise AdapterError(f"rate limited (HTTP 429): {resp.text[:150]}")
    if resp.status_code >= 400:
        raise AdapterError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    return resp


def _results(items: List[Dict], source: str, tier: str) -> Dict:
    return {"results": [r for r in items if r.get("url", "").startswith("http")]
            and [dict(r, source=source, tier=tier) for r in items if r.get("url", "").startswith("http")]
            or [], "units": 1}


# ── keyed: search ────────────────────────────────────────────────────────────

TAVILY_ALLOWED = {"search"}


def tavily_search(query: str, n: int = 6, endpoint: str = "search") -> Dict:
    if endpoint not in TAVILY_ALLOWED:
        raise BlockedEndpoint(
            f"tavily endpoint {endpoint!r} is blocked by policy — /research can burn "
            "~250 credits in a single call; only basic /search is permitted")
    key = get_key("tavily")
    if not key:
        raise AdapterAuthError("no Tavily key configured")
    resp = requests.post("https://api.tavily.com/search",
                         headers={"Authorization": f"Bearer {key}",
                                  "content-type": "application/json"},
                         json={"query": query, "search_depth": "basic",  # basic ONLY (1 credit)
                               "max_results": n},
                         timeout=25)
    if resp.status_code in (401, 403):
        raise AdapterAuthError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    if resp.status_code >= 400:
        raise AdapterError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    items = [{"title": r.get("title", ""), "url": r.get("url", ""),
              "snippet": (r.get("content") or "")[:400]} for r in data.get("results", [])]
    return _results(items, "tavily", "secondary")


def zenserp_search(query: str, n: int = 6) -> Dict:
    key = get_key("zenserp")
    if not key:
        raise AdapterAuthError("no Zenserp key configured")
    resp = _get("https://app.zenserp.com/api/v2/search",
                headers={"apikey": key}, params={"q": query, "num": n})
    organic = (resp.json() or {}).get("organic") or []
    items = [{"title": r.get("title", ""), "url": r.get("url", ""),
              "snippet": (r.get("description") or "")[:400]} for r in organic[:n]]
    return _results(items, "zenserp", "secondary")


def algolia_hn_search(query: str, n: int = 6) -> Dict:
    # public HN index — keyless; a key (if set) is accepted but not required
    resp = _get("https://hn.algolia.com/api/v1/search",
                params={"query": query, "hitsPerPage": n})
    hits = resp.json().get("hits", [])
    items = [{"title": h.get("title") or h.get("story_title") or "",
              "url": h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}",
              "snippet": (h.get("story_text") or h.get("comment_text") or "")[:400]}
             for h in hits]
    return _results(items, "algolia_hn", "community")


# ── keyed: read / extract ────────────────────────────────────────────────────

def jina_read(url: str) -> Dict:
    """PRIMARY extractor: URL → clean markdown. Units = tokens (~chars/4)."""
    key = get_key("jina")
    if not key:
        raise AdapterAuthError("no Jina key configured")
    resp = requests.get(f"https://r.jina.ai/{url}",
                        headers={"Authorization": f"Bearer {key}",
                                 "User-Agent": USER_AGENT, "X-Return-Format": "markdown"},
                        timeout=45)
    if resp.status_code in (401, 403):
        raise AdapterAuthError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    if resp.status_code >= 400:
        raise AdapterError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    text = resp.text[:40000]
    if len(text.strip()) < 80:
        raise AdapterError("empty extraction (JS-blocked or unreadable page)")
    return {"text": text, "units": math.ceil(len(text) / 4)}   # token estimate


def firecrawl_read(url: str) -> Dict:
    """Fallback scraper for JS-heavy pages Jina fails on. 1 credit/page."""
    key = get_key("firecrawl")
    if not key:
        raise AdapterAuthError("no Firecrawl key configured")
    resp = requests.post("https://api.firecrawl.dev/v1/scrape",
                         headers={"Authorization": f"Bearer {key}",
                                  "content-type": "application/json"},
                         json={"url": url, "formats": ["markdown"]}, timeout=60)
    if resp.status_code in (401, 403):
        raise AdapterAuthError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    if resp.status_code >= 400:
        raise AdapterError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    md = ((resp.json() or {}).get("data") or {}).get("markdown") or ""
    if len(md.strip()) < 80:
        raise AdapterError("empty extraction")
    return {"text": md[:40000], "units": 1}


def tinyfish_extract(url: str, instruction: str = "extract the pricing table") -> Dict:
    """Agentic structured extraction via TinyFish's AgentQL REST API
    (api.agentql.com/v1/query-data, natural-language prompt → structured JSON).
    Override the base with TINYFISH_BASE_URL for self-hosted/enterprise endpoints."""
    key = get_key("tinyfish")
    if not key:
        raise AdapterAuthError("no TinyFish/AgentQL key configured")
    base = os.environ.get("TINYFISH_BASE_URL", "https://api.agentql.com").rstrip("/")
    resp = requests.post(f"{base}/v1/query-data",
                         headers={"X-API-Key": key, "content-type": "application/json"},
                         json={"url": url, "prompt": instruction}, timeout=90)
    if resp.status_code in (401, 403):
        raise AdapterAuthError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    if resp.status_code >= 400:
        raise AdapterError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    import json as _json
    body = resp.json()
    data = body.get("data", body)
    if not data:
        raise AdapterError("extraction returned no data")
    return {"text": _json.dumps(data, indent=1)[:20000], "units": 1}


def builtin_read(url: str) -> Dict:
    """Keyless final fallback: plain requests + BeautifulSoup (static pages only)."""
    text = fetch_page(url)
    if not text:
        raise AdapterError("empty or unreachable page")
    return {"text": text, "units": 0}


# ── keyless primaries (free, and the best citations) ─────────────────────────

def openalex_search(query: str, n: int = 6) -> Dict:
    resp = _get("https://api.openalex.org/works",
                params={"search": query, "per-page": n, "mailto": _mailto()})
    items = []
    for w in resp.json().get("results", []):
        url = (w.get("primary_location") or {}).get("landing_page_url") or w.get("doi") or w.get("id", "")
        items.append({"title": w.get("display_name", ""), "url": url,
                      "snippet": f"cited {w.get('cited_by_count', 0)}× · {w.get('publication_year', '')}"})
    return dict(_results(items, "openalex", "primary"), units=0)


def crossref_search(query: str, n: int = 6) -> Dict:
    resp = _get("https://api.crossref.org/works",
                params={"query": query, "rows": n, "mailto": _mailto()})
    items = [{"title": " ".join(w.get("title") or [""]), "url": w.get("URL", ""),
              "snippet": w.get("publisher", "")}
             for w in resp.json().get("message", {}).get("items", [])]
    return dict(_results(items, "crossref", "primary"), units=0)


def arxiv_search(query: str, n: int = 6) -> Dict:
    resp = _get("http://export.arxiv.org/api/query",
                params={"search_query": f"all:{query}", "max_results": n},
                headers={"Accept": "application/atom+xml"})
    items = []
    for m in re.finditer(r"<entry>(.*?)</entry>", resp.text, re.S):
        block = m.group(1)
        title = re.search(r"<title>(.*?)</title>", block, re.S)
        link = re.search(r'<id>(.*?)</id>', block)
        summary = re.search(r"<summary>(.*?)</summary>", block, re.S)
        items.append({"title": (title.group(1) if title else "").strip()[:200],
                      "url": (link.group(1) if link else "").strip(),
                      "snippet": (summary.group(1) if summary else "").strip()[:300]})
    return dict(_results(items, "arxiv", "primary"), units=0)


def semantic_scholar_search(query: str, n: int = 6) -> Dict:
    resp = _get("https://api.semanticscholar.org/graph/v1/paper/search",
                params={"query": query, "limit": n, "fields": "title,url,abstract"})
    items = [{"title": p.get("title", ""), "url": p.get("url") or "",
              "snippet": (p.get("abstract") or "")[:300]}
             for p in resp.json().get("data", [])]
    return dict(_results(items, "semantic_scholar", "primary"), units=0)


def github_search(query: str, n: int = 6) -> Dict:
    resp = _get("https://api.github.com/search/repositories",
                params={"q": query, "per_page": n})
    items = [{"title": r.get("full_name", ""), "url": r.get("html_url", ""),
              "snippet": (r.get("description") or "")[:300]}
             for r in resp.json().get("items", [])]
    return dict(_results(items, "github", "primary"), units=0)


def pypi_search(query: str, n: int = 6) -> Dict:
    from bs4 import BeautifulSoup
    resp = _get("https://pypi.org/search/", params={"q": query},
                headers={"Accept": "text/html"})
    soup = BeautifulSoup(resp.text, "html.parser")
    items = []
    for a in soup.select("a.package-snippet")[:n]:
        name = a.select_one(".package-snippet__name")
        desc = a.select_one(".package-snippet__description")
        items.append({"title": name.get_text(strip=True) if name else "",
                      "url": f"https://pypi.org{a.get('href', '')}",
                      "snippet": desc.get_text(strip=True)[:300] if desc else ""})
    return dict(_results(items, "pypi", "primary"), units=0)


def npm_search(query: str, n: int = 6) -> Dict:
    resp = _get("https://registry.npmjs.org/-/v1/search", params={"text": query, "size": n})
    items = [{"title": o["package"].get("name", ""),
              "url": (o["package"].get("links") or {}).get("npm", ""),
              "snippet": (o["package"].get("description") or "")[:300]}
             for o in resp.json().get("objects", [])]
    return dict(_results(items, "npm", "primary"), units=0)


def wikipedia_search(query: str, n: int = 6) -> Dict:
    resp = _get("https://en.wikipedia.org/w/rest.php/v1/search/page",
                params={"q": query, "limit": n})
    items = [{"title": p.get("title", ""),
              "url": f"https://en.wikipedia.org/wiki/{p.get('key', '')}",
              "snippet": re.sub(r"<[^>]+>", "", p.get("excerpt") or "")[:300]}
             for p in resp.json().get("pages", [])]
    return dict(_results(items, "wikipedia", "secondary"), units=0)


def ddg_search(query: str, n: int = 6) -> Dict:
    items = web_search(query, max_results=n)
    return dict(_results(items, "ddg_web", "secondary"), units=0)


SEARCH_ADAPTERS = {
    "tavily": tavily_search, "zenserp": zenserp_search, "algolia_hn": algolia_hn_search,
    "openalex": openalex_search, "crossref": crossref_search, "arxiv": arxiv_search,
    "semantic_scholar": semantic_scholar_search, "github": github_search,
    "pypi": pypi_search, "npm": npm_search, "wikipedia": wikipedia_search,
    "ddg_web": ddg_search,
}
READ_ADAPTERS = {"jina": jina_read, "firecrawl": firecrawl_read, "builtin": builtin_read}
KEYED = {"tavily", "zenserp", "firecrawl", "jina", "tinyfish"}   # algolia_hn works keyless
