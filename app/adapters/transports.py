"""Outbound transport layer: mock network, not protocol.

`HttpTransport` is the seam between protocol construction and the wire.
RecordingTransport captures requests (default for all tests/demos);
RealHttpTransport would be the only thing swapped when real credentials
are provided. REAL_CHANNEL_NETWORK defaults to disabled.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RecordedRequest:
    url: str
    method: str
    headers: dict[str, str]
    params: dict[str, str]
    body: dict[str, Any] | str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "method": self.method,
            "headers": dict(self.headers),
            "params": dict(self.params),
            "body": self.body,
        }


class HttpTransport:
    """Network seam. Default: records requests, never touches the internet."""

    def __init__(self) -> None:
        self.records: list[RecordedRequest] = []
        self._fail_next: list[str] = []

    def fail_next(self, code: str) -> None:
        self._fail_next.append(code)

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | str | None = None,
    ) -> dict[str, Any]:
        record = RecordedRequest(
            url=url,
            method="POST",
            headers={k: v for k, v in (headers or {}).items()},
            params={k: v for k, v in (params or {}).items()},
            body=json,
        )
        self.records.append(record)
        if self._fail_next:
            code = self._fail_next.pop(0)
            raise TransportError(code)
        return {"ok": True}

    def get(self, url: str, *, params: dict[str, str] | None = None) -> dict[str, Any]:
        self.records.append(
            RecordedRequest(url=url, method="GET", headers={}, params={k: v for k, v in (params or {}).items()}, body=None)
        )
        return {"errcode": 0, "access_token": "FAKE_ACCESS_TOKEN", "expires_in": 7200}


class TransportError(RuntimeError):
    pass


class RealHttpTransport:
    """Real network transport (opt-in via REAL_CHANNEL_NETWORK=true)."""

    def __init__(self) -> None:
        import httpx

        self._client = httpx.Client(timeout=10.0)
        self.records: list[RecordedRequest] = []

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | str | None = None,
    ) -> dict[str, Any]:
        self.records.append(
            RecordedRequest(url=url, method="POST", headers=dict(headers or {}), params=dict(params or {}), body=json)
        )
        body = json.dumps(json, ensure_ascii=False) if isinstance(json, dict) else (json or "")
        resp = self._client.post(url, headers=headers, params=params, content=body)
        return resp.json()

    def get(self, url: str, *, params: dict[str, str] | None = None) -> dict[str, Any]:
        self.records.append(
            RecordedRequest(url=url, method="GET", headers={}, params=dict(params or {}), body=None)
        )
        resp = self._client.get(url, params=params)
        return resp.json()
