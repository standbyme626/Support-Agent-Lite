"""Notification domain: type, visibility and the outbox record.

Notification type expresses WHY a message is sent (not which platform).
Visibility keeps INTERNAL content away from requester conversations.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def _now() -> datetime:
    return datetime.now(timezone.utc)


class NotificationType(str, Enum):
    REACTIVE_REPLY = "REACTIVE_REPLY"
    PRIVATE_DETAIL = "PRIVATE_DETAIL"
    REQUESTER_STATUS_UPDATE = "REQUESTER_STATUS_UPDATE"
    OPERATOR_WORK_ITEM = "OPERATOR_WORK_ITEM"
    OPERATOR_ACTION_RECEIPT = "OPERATOR_ACTION_RECEIPT"
    REQUESTER_CONFIRMATION_REQUEST = "REQUESTER_CONFIRMATION_REQUEST"
    APPROVAL_REQUEST = "APPROVAL_REQUEST"
    APPROVAL_RESULT = "APPROVAL_RESULT"
    INTERNAL_NOTE = "INTERNAL_NOTE"


class Visibility(str, Enum):
    PUBLIC = "PUBLIC"      # requester group
    PRIVATE = "PRIVATE"    # requester DM only
    INTERNAL = "INTERNAL"  # operator / approver only


class OutboxStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


@dataclass(slots=True)
class NotificationRecord:
    id: str
    source_event_id: str
    notification_type: NotificationType
    visibility: Visibility
    target_type: str  # requester_public | requester_private | operator_queue | action_origin | approver | channel_conversation
    target_key: str   # conversation id / queue / channel user key
    message: str
    status: OutboxStatus = OutboxStatus.PENDING
    attempt_count: int = 0
    ticket_id: str | None = None
    trace_id: str | None = None
    created_at: datetime = field(default_factory=_now)
