"""Outbound contract: what a channel connector can do and how delivery works.

Capabilities describe what a connector may do. Capability is a channel
property, NOT a business rule. Unproven official capabilities stay
disabled (PENDING_OFFICIAL_SPEC) and business core simulates delivery.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ChannelCapability(str, Enum):
    DM_INBOUND = "DM_INBOUND"
    DM_OUTBOUND = "DM_OUTBOUND"
    GROUP_INBOUND = "GROUP_INBOUND"
    GROUP_OUTBOUND = "GROUP_OUTBOUND"
    MESSAGE_REPLY = "MESSAGE_REPLY"
    CARD_ACTION = "CARD_ACTION"
    WEBHOOK_VERIFICATION = "WEBHOOK_VERIFICATION"


class CapabilityStatus(str, Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    PENDING_OFFICIAL_SPEC = "PENDING_OFFICIAL_SPEC"


class TargetKind(str, Enum):
    CONVERSATION = "conversation"
    USER = "user"


@dataclass(frozen=True)
class DeliveryTarget:
    channel: str
    kind: TargetKind
    target_id: str


@dataclass(frozen=True)
class OutboundMessage:
    channel: str
    target: DeliveryTarget
    text: str
    notification_type: str
    trace_id: str | None = None


@dataclass
class DeliveryResult:
    success: bool
    result_code: str | None = None
    error: str | None = None
