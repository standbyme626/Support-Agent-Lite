"""V2 reliability: AC-15 concurrent claim, AC-25 concurrent duplicate webhook.

Service-level concurrency on one shared connection (sqlite serializes
writes; BEGIN IMMEDIATE + atomic WHERE guards decide the winner).
"""
from __future__ import annotations

import threading

from app.application.identity_service import IdentityResolver
from app.application.session_service import SessionService
from app.infrastructure.db import apply_migrations, connect
from app.infrastructure.idempotency import IdempotencyStore
from app.infrastructure.repositories import (
    ChannelIdentityRepository,
    SessionRepository,
    TicketStore,
    UserRepository,
)
from app.main import build_ingress
from app.adapters.wecom import WeComAdapter

CONCURRENCY = 25


def test_ac15_concurrent_claim_exactly_one_winner() -> None:
    """25 operators claim T0001 at once: exactly 1 succeeds."""
    conn = connect(":memory:")
    apply_migrations(conn)
    users = UserRepository(conn)
    identity = IdentityResolver(users, ChannelIdentityRepository(conn))
    store = TicketStore(conn)
    from app.application.ticket_service import TicketService

    requester = identity.resolve("wecom", "zhangsan", "张三")
    TicketService(store).create(requester.id, "A3 空调坏了", "不制冷")
    for i in range(CONCURRENCY):
        identity.resolve("wecom", f"op_{i}", f"李师傅{i}")

    winners: list[str] = []
    errors: list[str] = []
    lock = threading.Lock()

    def claim(i: int) -> None:
        try:
            ticket = store.claim("T0001", f"user_op_{i}")  # type: ignore[arg-type]
            with lock:
                winners.append(ticket.id)
        except Exception as exc:
            with lock:
                errors.append(type(exc).__name__)

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(CONCURRENCY)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1, f"winners={len(winners)}"
    assert len(errors) == CONCURRENCY - 1
    assert all(e == "AlreadyClaimed" for e in errors)
    # exactly one claimed event
    events = [e.event_type.value for e in store.events("T0001")]
    assert events.count("claimed") == 1
    assert store.get("T0001").assignee_user_id is not None


def test_ac25_concurrent_duplicate_webhook_exactly_once() -> None:
    """25 identical webhooks at once: 1 processed, 24 duplicates, 1 ticket,
    zero 500s (atomic idempotency claim + identity race fix)."""
    ingress, conn, store = build_ingress(db_path=":memory:")
    payload = {"MsgId": "same-msg", "FromUserName": "zhangsan", "Content": "A3 空调坏了", "CreateTime": 1000}

    results: list[bool] = []
    lock = threading.Lock()

    def send() -> None:
        try:
            result = ingress.process("wecom", payload)
            with lock:
                results.append(result.duplicate)
        except Exception as exc:
            with lock:
                results.append(str(exc))

    threads = [threading.Thread(target=send) for _ in range(CONCURRENCY)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == CONCURRENCY
    assert results.count(False) == 1  # exactly one business execution
    assert results.count(True) == CONCURRENCY - 1  # rest are duplicates
    tickets = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    assert tickets == 1
    users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert users == 1  # identity race did not create duplicates / 500s


def test_concurrent_webhook_different_messages_no_interference() -> None:
    """Distinct messages concurrently still resolve distinct users cleanly."""
    ingress, conn, _ = build_ingress(db_path=":memory:")
    results: list[str] = []
    lock = threading.Lock()

    def send(i: int) -> None:
        try:
            result = ingress.process(
                "wecom",
                {"MsgId": f"m{i}", "FromUserName": f"user{i}", "Content": "VPN 连不上", "CreateTime": 1000},
            )
            with lock:
                results.append(result.user.id)
        except Exception as exc:
            with lock:
                results.append(f"ERR:{exc}")

    threads = [threading.Thread(target=send, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 10
    assert all(r.startswith("user_") for r in results)
    assert len(set(results)) == 10
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 10


def test_idempotency_release_on_failure() -> None:
    """Business failure releases the idempotency claim: a retry reprocesses."""
    conn = connect(":memory:")
    apply_migrations(conn)
    idem = IdempotencyStore(conn)
    assert idem.claim("wecom:m1", "trace_1") is True
    # simulate business failure -> rollback + release
    with conn:
        conn.execute("DELETE FROM processed_messages WHERE idempotency_key = 'wecom:m1'")
    assert idem.claim("wecom:m1", "trace_2") is True  # retryable
    assert idem.claim("wecom:m1", "trace_3") is False  # claimed again -> dup
