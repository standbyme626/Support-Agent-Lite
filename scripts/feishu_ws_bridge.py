"""Feishu long-connection (WebSocket) inbound bridge.

Official long-connection mode (no public URL / no Verification Token):
https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case

The bridge receives `im.message.receive_v1` events over the official SDK's
ws client, re-wraps them into the official v2.0 envelope shape
({"schema":"2.0","header":...,"event":...}) and forwards them to the local
FastAPI webhook endpoint, so the protocol entry point of the platform stays
exactly the same as the push mode (`POST /webhooks/feishu`).

Run:
    .venv/bin/python scripts/feishu_ws_bridge.py

Requires in .env: FEISHU_APP_ID, FEISHU_APP_SECRET.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
import lark_oapi as lark


def _load_env() -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def main() -> None:
    _load_env()
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        sys.exit("FEISHU_APP_ID / FEISHU_APP_SECRET missing (set them in .env)")

    forward_url = os.environ.get("FEISHU_WS_FORWARD_URL", "http://127.0.0.1:8322/webhooks/feishu")
    http = httpx.Client(timeout=15.0)

    def forward(payload: dict) -> None:
        resp = http.post(forward_url, json=payload)
        print(f"[bridge] forwarded -> {resp.status_code} {resp.text[:200]}", flush=True)

    def on_message_receive(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
        try:
            envelope = json.loads(lark.JSON.marshal(data))
            envelope.setdefault("schema", "2.0")
            print(
                "[bridge] im.message.receive_v1"
                f" message_id={envelope.get('event', {}).get('message', {}).get('message_id')}",
                flush=True,
            )
            forward(envelope)
        except Exception as exc:  # keep the ws loop alive no matter what
            print(f"[bridge] ERROR handling event: {exc!r}", flush=True)

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message_receive)
        .build()
    )

    cli = lark.ws.Client(app_id, app_secret, event_handler=event_handler, log_level=lark.LogLevel.INFO)
    print(f"[bridge] starting long connection, forwarding to {forward_url}", flush=True)
    cli.start()


if __name__ == "__main__":
    main()
