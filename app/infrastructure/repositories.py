"""Repositories for the Phase 1 domain entities."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Protocol

from app.domain.approval import Approval, ApprovalStatus, InvalidApprovalDecision
from app.domain.identity import ChannelIdentity, Session, User
from app.domain.message import Message
from app.domain.ticket import (
    InvalidStateTransition,
    Ticket,
    TicketEvent,
    TicketEventType,
    TicketStatus,
    validate_transition,
)


def _parse_status(value: str) -> TicketStatus:
    return TicketStatus(value)


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class TicketRepository(Protocol):
    def create(self, ticket: Ticket) -> Ticket: ...
    def get(self, ticket_id: str) -> Ticket | None: ...
    def list_by_user(self, user_id: str) -> list[Ticket]: ...
    def transition(self, ticket_id: str, target: TicketStatus, payload: dict | None = None) -> Ticket: ...
    def events(self, ticket_id: str) -> list[TicketEvent]: ...


class SqliteRepository:
    """Shared base: all writes go through this connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._conn.isolation_level = None  # explicit transaction control


class UserRepository(SqliteRepository):
    def create(self, user: User) -> User:
        self._conn.execute(
            "INSERT INTO users (id, display_name, created_at) VALUES (?, ?, ?)",
            (user.id, user.display_name, user.created_at.isoformat()),
        )
        return user

    def get(self, user_id: str) -> User | None:
        row = self._conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return User(id=row["id"], display_name=row["display_name"], created_at=_parse_dt(row["created_at"])) if row else None


class ChannelIdentityRepository(SqliteRepository):
    def create(self, identity: ChannelIdentity) -> ChannelIdentity:
        self._conn.execute(
            "INSERT INTO channel_identities (id, user_id, channel, channel_user_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (identity.id, identity.user_id, identity.channel, identity.channel_user_id, identity.created_at.isoformat()),
        )
        return identity

    def find(self, channel: str, channel_user_id: str) -> ChannelIdentity | None:
        row = self._conn.execute(
            "SELECT * FROM channel_identities WHERE channel = ? AND channel_user_id = ?",
            (channel, channel_user_id),
        ).fetchone()
        return (
            ChannelIdentity(
                id=row["id"],
                user_id=row["user_id"],
                channel=row["channel"],
                channel_user_id=row["channel_user_id"],
                created_at=_parse_dt(row["created_at"]),
            )
            if row
            else None
        )

    def list_by_user(self, user_id: str) -> list[ChannelIdentity]:
        rows = self._conn.execute(
            "SELECT * FROM channel_identities WHERE user_id = ?", (user_id,)
        ).fetchall()
        return [ChannelIdentity(id=r["id"], user_id=r["user_id"], channel=r["channel"], channel_user_id=r["channel_user_id"], created_at=r["created_at"]) for r in rows]


class SessionRepository(SqliteRepository):
    def create(self, session: Session) -> Session:
        self._conn.execute(
            "INSERT INTO sessions (id, user_id, channel, channel_conversation_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session.id, session.user_id, session.channel, session.channel_conversation_id, session.created_at.isoformat()),
        )
        return session

    def get(self, session_id: str) -> Session | None:
        row = self._conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return (
            Session(
                id=row["id"],
                user_id=row["user_id"],
                channel=row["channel"],
                channel_conversation_id=row["channel_conversation_id"],
                created_at=_parse_dt(row["created_at"]),
            )
            if row
            else None
        )

    def list_by_user(self, user_id: str) -> list[Session]:
        rows = self._conn.execute(
            "SELECT * FROM sessions WHERE user_id = ?", (user_id,)
        ).fetchall()
        return [Session(id=r["id"], user_id=r["user_id"], channel=r["channel"], channel_conversation_id=r["channel_conversation_id"], created_at=_parse_dt(r["created_at"])) for r in rows]


class MessageRepository(SqliteRepository):
    """Per-session chat message history (recent messages for context)."""

    def add(self, message: Message) -> Message:
        self._conn.execute(
            "INSERT INTO messages (id, session_id, user_id, role, text, trace_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                message.id,
                message.session_id,
                message.user_id,
                message.role,
                message.text,
                message.trace_id,
                message.created_at.isoformat(),
            ),
        )
        return message

    def recent(self, session_id: str, limit: int = 6) -> list[Message]:
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return list(
            reversed(
                [
                    Message(
                        id=r["id"],
                        session_id=r["session_id"],
                        user_id=r["user_id"],
                        role=r["role"],
                        text=r["text"],
                        trace_id=r["trace_id"],
                        created_at=_parse_dt(r["created_at"]),
                    )
                    for r in rows
                ]
            )
        )


class ApprovalRepository(SqliteRepository):
    """Approval store. Independent of tickets (invariant #6)."""

    def create(self, approval: Approval) -> Approval:
        self._conn.execute(
            "INSERT INTO approvals (id, ticket_id, action, status, requested_by, reason, decided_by, decided_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                approval.id,
                approval.ticket_id,
                approval.action,
                approval.status.value,
                approval.requested_by,
                approval.reason,
                approval.decided_by,
                approval.decided_at.isoformat() if approval.decided_at else None,
                approval.created_at.isoformat(),
            ),
        )
        return approval

    def get(self, approval_id: str) -> Approval | None:
        row = self._conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return self._row_to_approval(row) if row else None

    def list_by_status(self, status: ApprovalStatus | None = None) -> list[Approval]:
        if status is None:
            rows = self._conn.execute("SELECT * FROM approvals ORDER BY created_at").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM approvals WHERE status = ? ORDER BY created_at", (status.value,)
            ).fetchall()
        return [self._row_to_approval(r) for r in rows]

    def list_by_ticket(self, ticket_id: str) -> list[Approval]:
        rows = self._conn.execute(
            "SELECT * FROM approvals WHERE ticket_id = ? ORDER BY created_at", (ticket_id,)
        ).fetchall()
        return [self._row_to_approval(r) for r in rows]

    def decide(
        self,
        approval_id: str,
        target: ApprovalStatus,
        *,
        decided_by: str,
        reason: str | None = None,
    ) -> Approval:
        """PENDING -> APPROVED/REJECTED, atomically. Idempotency guard:
        a second decision on the same approval fails (rowcount == 0).
        """
        if target == ApprovalStatus.PENDING:
            raise ValueError("cannot decide to PENDING")
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE approvals SET status = ?, decided_by = ?, decided_at = ?, reason = COALESCE(?, reason) "
                "WHERE id = ? AND status = ?",
                (target.value, decided_by, _ts(), reason, approval_id, ApprovalStatus.PENDING.value),
            )
        if cursor.rowcount == 0:
            existing = self.get(approval_id)
            if existing is None:
                raise KeyError(f"approval not found: {approval_id}")
            raise InvalidApprovalDecision(f"approval already decided: {existing.status.value}")
        approval = self.get(approval_id)
        if approval is None:
            raise KeyError(f"approval not found: {approval_id}")
        return approval

    @staticmethod
    def _row_to_approval(row: sqlite3.Row) -> Approval:
        return Approval(
            id=row["id"],
            ticket_id=row["ticket_id"],
            action=row["action"],
            status=ApprovalStatus(row["status"]),
            requested_by=row["requested_by"],
            reason=row["reason"],
            decided_by=row["decided_by"],
            decided_at=_parse_dt(row["decided_at"]) if row["decided_at"] else None,
            created_at=_parse_dt(row["created_at"]),
        )


class TicketStore(SqliteRepository):
    """Transactional Ticket + TicketEvent store.

    Invariant #5: ticket current state and TicketEvent are committed
    atomically in a single transaction.
    """

    def create(self, ticket: Ticket) -> Ticket:
        with self._conn:  # atomic
            self._insert_ticket(ticket)
            self._insert_event(TicketEvent(id=_eid(), ticket_id=ticket.id, event_type=TicketEventType.CREATED))
        return ticket

    def transition(self, ticket_id: str, target: TicketStatus, payload: dict | None = None) -> Ticket:
        ticket = self.get(ticket_id)
        if ticket is None:
            raise KeyError(f"ticket not found: {ticket_id}")
        validate_transition(ticket.status, target)
        with self._conn:  # atomic: state change + event
            self._conn.execute(
                "UPDATE tickets SET status = ?, updated_at = ? WHERE id = ?",
                (target.value, _ts(), ticket_id),
            )
            self._insert_event(
                TicketEvent(id=_eid(), ticket_id=ticket_id, event_type=_event_for(ticket.status, target), payload=payload)
            )
        return self.get(ticket_id)  # type: ignore[return-value]

    def get(self, ticket_id: str) -> Ticket | None:
        row = self._conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return self._row_to_ticket(row) if row else None

    def list_by_user(self, user_id: str) -> list[Ticket]:
        rows = self._conn.execute(
            "SELECT * FROM tickets WHERE user_id = ? ORDER BY created_at", (user_id,)
        ).fetchall()
        return [self._row_to_ticket(r) for r in rows]

    def events(self, ticket_id: str) -> list[TicketEvent]:
        rows = self._conn.execute(
            "SELECT * FROM ticket_events WHERE ticket_id = ? ORDER BY created_at", (ticket_id,)
        ).fetchall()
        return [
            TicketEvent(
                id=r["id"],
                ticket_id=r["ticket_id"],
                event_type=TicketEventType(r["event_type"]),
                payload=json.loads(r["payload"]) if r["payload"] else None,
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def _insert_ticket(self, ticket: Ticket) -> None:
        self._conn.execute(
            "INSERT INTO tickets (id, user_id, title, description, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ticket.id, ticket.user_id, ticket.title, ticket.description, ticket.status.value, ticket.created_at.isoformat(), ticket.updated_at.isoformat()),
        )

    def _insert_event(self, event: TicketEvent) -> None:
        self._conn.execute(
            "INSERT INTO ticket_events (id, ticket_id, event_type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            (event.id, event.ticket_id, event.event_type.value, json.dumps(event.payload) if event.payload else None, event.created_at.isoformat()),
        )

    @staticmethod
    def _row_to_ticket(row: sqlite3.Row) -> Ticket:
        return Ticket(
            id=row["id"],
            user_id=row["user_id"],
            title=row["title"],
            description=row["description"],
            status=_parse_status(row["status"]),
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )


def _eid() -> str:
    from uuid import uuid4

    return uuid4().hex[:12]


def _ts() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _event_for(current: TicketStatus, target: TicketStatus) -> TicketEventType:
    from app.domain.ticket import EVENT_FOR_TRANSITION

    return EVENT_FOR_TRANSITION[(current, target)]


__all__ = [
    "UserRepository",
    "ChannelIdentityRepository",
    "SessionRepository",
    "MessageRepository",
    "TicketStore",
    "TicketRepository",
    "ApprovalRepository",
    "InvalidStateTransition",
]
