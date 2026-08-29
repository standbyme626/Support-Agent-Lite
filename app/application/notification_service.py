"""NotificationService: business event -> outbox -> dispatch.

Business logic never hand-writes channel sends. A business event yields
outbox records (same DB transaction as the ticket change). Dispatch runs
after commit; simulated transport failures keep the outbox retryable.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from uuid import uuid4

from app.application.target_resolver import ResolvedTarget, TargetResolver
from app.domain.notification import NotificationRecord, NotificationType, OutboxStatus, Visibility
from app.domain.outbound import OutboundMessage
from app.domain.ticket import Ticket
from app.infrastructure.repositories import NotificationOutboxRepository

SYSTEM_ACTOR = "user_system"

# How long a delivery may stay un-retried. The worker exists so a failed
# send NEVER depends on the next inbound message arriving (production:
# SUPPORT_AGENT_DISPATCH_WORKER=1 in the unit file, 30s cadence).
DEFAULT_DISPATCH_INTERVAL = 30.0


def new_id(prefix: str) -> str:
    return f"{prefix}{uuid4().hex[:12]}"


class ChannelOutboundClient:
    """Protocol adapter for outbound delivery (implemented per channel).

    `deliver` must return success even when the capability is not backed
    by a real network (simulated transport), and must raise
    ChannelCapabilityDisabled when the channel cannot express the target.
    """

    channel: str

    def deliver(self, message: OutboundMessage) -> tuple[bool, str, str | None]:
        """Returns (success, result_code, error)."""
        raise NotImplementedError


class ChannelCapabilityDisabled(RuntimeError):
    pass


class NotificationService:
    def __init__(
        self,
        outbox: NotificationOutboxRepository,
        targets: TargetResolver,
        clients: dict[str, ChannelOutboundClient],
    ) -> None:
        self._outbox = outbox
        self._targets = targets
        self._clients = clients

    # --- enqueue (call inside the business transaction) ---

    def enqueue(
        self,
        *,
        source_event_id: str,
        notification_type: NotificationType,
        visibility: Visibility,
        message: str,
        target: ResolvedTarget,
        ticket_id: str | None = None,
        trace_id: str | None = None,
    ) -> NotificationRecord | None:
        if target.delivery is None:
            return None
        record = NotificationRecord(
            id=new_id("ntf_"),
            source_event_id=source_event_id,
            notification_type=notification_type,
            visibility=visibility,
            target_type=target.reason,
            target_key=f"{target.delivery.kind.value}:{target.delivery.channel}:{target.delivery.target_id}",
            message=message,
            ticket_id=ticket_id,
            trace_id=trace_id,
        )
        try:
            return self._outbox.add(record)
        except Exception:  # UNIQUE(source_event_id, type, target_key) -> dedupe
            return None

    # --- dispatch (after commit; failures never roll back business) ---

    def dispatch(self, max_attempts: int = 3) -> list[str]:
        results: list[str] = []
        # Snapshot first: records that fail during THIS dispatch must not be
        # retried within the same call.
        pending = [r for r in self._outbox.pending(limit=200) if r.attempt_count < max_attempts]
        failed = [r for r in self._outbox.failed(limit=200) if r.attempt_count < max_attempts]
        for record in pending + failed:
            results.append(self._dispatch_one(record))
        return results

    def list_for_ticket(self, ticket_id: str) -> list[NotificationRecord]:
        return self._outbox.list_by_ticket(ticket_id)

    def _dispatch_one(self, record: NotificationRecord) -> str:
        channel, target_id = self._parse_key(record.target_key)
        message = OutboundMessage(
            channel=channel,
            target=record_target(record, channel, target_id),
            text=record.message,
            notification_type=record.notification_type.value,
            trace_id=record.trace_id,
        )
        client = self._clients.get(channel)
        if client is None:
            self._record_failure(record, "no_client")
            return f"{record.id}:no_client"
        try:
            success, code, error = client.deliver(message)
        except ChannelCapabilityDisabled as exc:
            success, code, error = False, "CAPABILITY_DISABLED", str(exc)
        except Exception as exc:  # simulated transport failures land here
            success, code, error = False, "TRANSPORT_ERROR", str(exc)
        attempt = record.attempt_count + 1
        self._outbox.add_attempt(record.id, attempt, success, code, error)
        if success:
            self._outbox.mark(record.id, OutboxStatus.SENT, attempt, code)
            return f"{record.id}:sent:{code}"
        self._outbox.mark(record.id, OutboxStatus.FAILED, attempt, code)
        return f"{record.id}:failed:{code}"

    def _record_failure(self, record: NotificationRecord, code: str) -> None:
        attempt = record.attempt_count + 1
        self._outbox.add_attempt(record.id, attempt, False, code, "no outbound client for channel")
        self._outbox.mark(record.id, OutboxStatus.FAILED, attempt, code)

    @staticmethod
    def _parse_key(target_key: str) -> tuple[str, str]:
        kind, channel, target_id = target_key.split(":", 2)
        return channel, target_id


def record_target(record: NotificationRecord, channel: str, target_id: str):
    from app.domain.outbound import DeliveryTarget, TargetKind

    kind = TargetKind.USER if record.target_key.startswith("user:") else TargetKind.CONVERSATION
    return DeliveryTarget(channel, kind, target_id)


def ticket_public_private_messages(ticket: Ticket, requester_name: str, status_label: str) -> dict:
    """Standard requester-facing message pair for lifecycle updates."""
    return {
        "public": (
            f"{requester_name}，工单 {ticket.id} 当前状态：{status_label}。"
        ),
        "private": (
            f"工单：{ticket.id}\n问题：{ticket.title}\n状态：{status_label}\n"
            f"优先级：{ticket.priority or 'P3'}"
        ),
    }


class DispatchWorker:
    """Periodic outbox sweeper (daemon thread).

    Retries pending/failed deliveries on a fixed cadence so a transient
    channel failure heals by itself. Failures inside sweep() are swallowed:
    the next tick retries anyway. Started only when the production unit
    sets SUPPORT_AGENT_DISPATCH_WORKER=1 — tests stay single-threaded.
    """

    def __init__(self, notifications: NotificationService, interval: float = DEFAULT_DISPATCH_INTERVAL) -> None:
        self._notifications = notifications
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def sweep(self) -> list[str]:
        """One dispatch pass (also handy for tests/ops)."""
        try:
            return self._notifications.dispatch()
        except Exception:  # noqa: BLE001 - never die on one bad pass
            return []

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return

        def _loop() -> None:
            while not self._stop.wait(self._interval):
                self.sweep()

        self._stop.clear()
        self._thread = threading.Thread(target=_loop, name="dispatch-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
