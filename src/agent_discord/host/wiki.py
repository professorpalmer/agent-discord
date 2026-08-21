"""Portable LLM wiki over HTTP. Same results as the wiki MCP, no MCP bus."""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_LOCAL_WIKI = "http://127.0.0.1:8000"


def wiki_base_url(env: Optional[Mapping[str, str]] = None) -> str:
    source = dict(os.environ if env is None else env)
    raw = (
        source.get("WIKI_BASE_URL")
        or source.get("PORTABLE_WIKI_URL")
        or ""
    ).strip()
    if raw:
        return raw.rstrip("/")
    token = wiki_token(source)
    if token:
        return DEFAULT_LOCAL_WIKI
    return ""


def wiki_token(env: Optional[Mapping[str, str]] = None) -> str:
    source = dict(os.environ if env is None else env)
    return (
        source.get("WIKI_OWNER_TOKEN")
        or source.get("OWNER_TOKEN")
        or source.get("WIKI_SHARE_TOKEN")
        or ""
    ).strip()


def wiki_configured(env: Optional[Mapping[str, str]] = None) -> bool:
    return bool(wiki_base_url(env))


def wiki_hint(env: Optional[Mapping[str, str]] = None) -> str:
    base = wiki_base_url(env)
    if not base:
        return "Wiki HTTP is unset."
    return f"discord-os wiki query \"...\" → {base}/wiki/query"


def wiki_query(
    question: str,
    *,
    env: Optional[Mapping[str, str]] = None,
    opener: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    return _wiki_http(
        "POST",
        "/wiki/query",
        body={"question": question},
        env=env,
        opener=opener,
    )


def wiki_search(
    query: str,
    *,
    limit: int = 10,
    env: Optional[Mapping[str, str]] = None,
    opener: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    qs = urlencode({"q": query, "limit": str(max(1, int(limit)))})
    return _wiki_http("GET", f"/wiki/search?{qs}", env=env, opener=opener)


def _wiki_http(
    method: str,
    path: str,
    *,
    body: Optional[Mapping[str, Any]] = None,
    env: Optional[Mapping[str, str]] = None,
    opener: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    base = wiki_base_url(env)
    if not base:
        return {"error": "WIKI_BASE_URL is unset"}
    url = f"{base}{path}" if path.startswith("/") else f"{base}/{path}"
    headers = {"Accept": "application/json"}
    token = wiki_token(env)
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-Share-Token"] = token
    data = None
    if body is not None:
        data = json.dumps(dict(body)).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    fetch = opener or urlopen
    try:
        with fetch(request, timeout=30) as response:
            raw = response.read()
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
        except Exception:
            detail = str(exc)
        return {"error": f"wiki HTTP {exc.code}", "detail": detail}
    except (URLError, TimeoutError, OSError) as exc:
        return {"error": f"wiki unreachable: {exc}"}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"error": "wiki returned non-JSON"}
    if isinstance(parsed, dict):
        return parsed
    return {"error": "wiki returned a non-object"}
