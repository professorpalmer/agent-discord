"""HTTP transport for Marionette — stdlib urllib with an injectable seam for tests."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str

    def json(self) -> Any:
        if not self.body:
            return None
        return json.loads(self.body.decode("utf-8"))

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


@runtime_checkable
class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        body: Optional[bytes] = None,
        timeout: float = 30.0,
    ) -> HttpResponse: ...


class UrllibTransport:
    """Default stdlib transport. Does not claim any remote endpoint is available."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        body: Optional[bytes] = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        req = urllib.request.Request(
            url,
            data=body,
            method=method.upper(),
            headers=dict(headers or {}),
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                hdrs = {k.lower(): v for k, v in resp.headers.items()}
                return HttpResponse(
                    status=int(resp.status),
                    headers=hdrs,
                    body=raw,
                    url=str(resp.geturl()),
                )
        except urllib.error.HTTPError as exc:
            raw = exc.read() if exc.fp is not None else b""
            hdrs = {k.lower(): v for k, v in (exc.headers.items() if exc.headers else [])}
            return HttpResponse(
                status=int(exc.code),
                headers=hdrs,
                body=raw,
                url=url,
            )
