"""Free-source web retrieval for Agent 3: search, read full pages, verify citations.

Search prefers the `ddgs` package (DuckDuckGo, no key); without it, falls back to
scraping DuckDuckGo's HTML endpoint. Both are best-effort — workers treat an empty
result as "no evidence", never as an excuse to invent one.
"""
from __future__ import annotations

import time
from typing import Dict, List
from urllib.parse import unquote, urlparse, parse_qs

import requests

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MAX_PAGE_CHARS = 20000
_SEARCH_PAUSE_S = 1.0  # be polite to the free endpoint
_last_search = [0.0]


def _throttle() -> None:
    delta = time.monotonic() - _last_search[0]
    if delta < _SEARCH_PAUSE_S:
        time.sleep(_SEARCH_PAUSE_S - delta)
    _last_search[0] = time.monotonic()


def web_search(query: str, max_results: int = 6) -> List[Dict[str, str]]:
    """-> [{title, url, snippet}], deduped, best-effort."""
    _throttle()
    try:
        try:
            from ddgs import DDGS  # >= 9.x
        except ImportError:
            from duckduckgo_search import DDGS  # older package name
        with DDGS() as ddgs:
            hits = list(ddgs.text(query, max_results=max_results))
        return [{"title": h.get("title", ""), "url": h.get("href") or h.get("url", ""),
                 "snippet": h.get("body", "")} for h in hits if h.get("href") or h.get("url")]
    except Exception:
        return _html_fallback_search(query, max_results)


def _html_fallback_search(query: str, max_results: int) -> List[Dict[str, str]]:
    try:
        from bs4 import BeautifulSoup
        resp = requests.get("https://html.duckduckgo.com/html/",
                            params={"q": query}, headers={"User-Agent": USER_AGENT}, timeout=20)
        soup = BeautifulSoup(resp.text, "html.parser")
        out: List[Dict[str, str]] = []
        for a in soup.select("a.result__a")[:max_results]:
            href = a.get("href", "")
            # DDG wraps targets as /l/?uddg=<real-url>
            if "uddg=" in href:
                qs = parse_qs(urlparse(href).query)
                href = unquote(qs.get("uddg", [href])[0])
            if href.startswith("http"):
                out.append({"title": a.get_text(" ", strip=True), "url": href, "snippet": ""})
        return out
    except Exception:
        return []


def fetch_page(url: str, max_chars: int = MAX_PAGE_CHARS) -> str:
    """Read the FULL page text (not a snippet), truncated for the context window."""
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        if resp.status_code >= 400:
            return ""
        ctype = resp.headers.get("content-type", "")
        if "html" not in ctype and "text" not in ctype and "json" not in ctype:
            return ""
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "noscript"]):
            tag.decompose()
        text = " ".join(soup.get_text(" ", strip=True).split())
        return text[:max_chars]
    except Exception:
        return ""


def check_url(url: str) -> bool:
    """Citation reachability: HEAD, then GET on servers that reject HEAD."""
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = requests.head(url, headers=headers, timeout=15, allow_redirects=True)
        if resp.status_code < 400:
            return True
        if resp.status_code in (403, 405, 501):
            resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True, stream=True)
            return resp.status_code < 400
        return False
    except requests.RequestException:
        return False


def rate_reliability(url: str, sources_cfg: Dict) -> str:
    """Tier a URL by the substring rules in research.yaml sources.reliability."""
    tiers = (sources_cfg or {}).get("reliability") or {}
    for tier in ("high", "medium"):
        for marker in tiers.get(tier, []) or []:
            if marker.lower() in url.lower():
                return tier
    return "low"


def is_denied(url: str, sources_cfg: Dict) -> bool:
    return any(d.lower() in url.lower() for d in (sources_cfg or {}).get("denylist", []) or [])
