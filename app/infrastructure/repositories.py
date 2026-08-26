"""Repositories for the Phase 1 domain entities (V2: conversations, roles,
outbox, pending actions, session ticket contexts)."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Protocol, Any, Iterator

from app.domain.approval import Approval, ApprovalStatus, InvalidApprovalDecision
from app.domain.conversation import Conversation, ConversationPurpose, ConversationType
from app.domain.identity import ChannelIdentity, Session, User
from app.domain.memory import Memory, MemoryKind, SessionCompaction
from app.domain.message import Message
from app.domain.notification import NotificationRecord, NotificationType, OutboxStatus, Visibility
from app.domain.pending_action import ApprovableAction, PendingAction, PendingActionStatus
from app.domain.role import RoleAssignment, UserRole
from app.domain.ticket import (
    AlreadyClaimed,
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


_txn_state = threading.local()


def _txn_depth() -> int:
    return getattr(_txn_state, "depth", 0)


@contextmanager
def txn(conn) -> Iterator[Any]:
    """Nested-safe explicit transaction on a shared connection.

    Python's `with conn:` commits at every nested exit, which breaks
    multi-write atomicity (ticket state + event + outbox must commit
    together). This helper uses a depth counter: only the outermost
    context commits/rolls back. It also holds the connection's
    serialization lock for the whole transaction.
    """
    lock = getattr(conn, "_txn_lock", None)
    if lock is not None:
        lock.acquire()
    try:
        if _txn_depth() == 0:
            conn.execute("BEGIN IMMEDIATE")
        _txn_state.depth = _txn_depth() + 1
        try:
            yield conn
        except Exception:
            _txn_state.depth = _txn_depth() - 1
            if _txn_depth() == 0:
                conn.execute("ROLLBACK")
            raise
        else:
            _txn_state.depth = _txn_depth() - 1
            if _txn_depth() == 0:
                conn.execute("COMMIT")
    finally:
        if lock is not None:
            lock.release()


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

    def find_by_user_on_channel(self, channel: str, user_id: str) -> ChannelIdentity | None:
        row = self._conn.execute(
            "SELECT * FROM channel_identities WHERE channel = ? AND user_id = ? LIMIT 1",
            (channel, user_id),
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

    def recent(self, session_id: str, limit: int = 6, *, after_id: str | None = None) -> list[Message]:
        """Recent messages in chronological order.

        `after_id` (compaction retained-tail 语义): only messages AFTER
        the given message id — compacted history is replaced by the
        session's rolling summary and must not re-enter the window.
        """
        if after_id is not None:
            anchor = self._conn.execute(
                "SELECT rowid FROM messages WHERE id = ? AND session_id = ?", (after_id, session_id)
            ).fetchone()
            if anchor is not None:
                rows = self._conn.execute(
                    "SELECT * FROM messages WHERE session_id = ? AND rowid > ? "
                    "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (session_id, anchor["rowid"], limit),
                ).fetchall()
                return self._rows_to_messages(rows)
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return self._rows_to_messages(rows)

    def list_after(self, session_id: str, *, after_id: str | None = None) -> list[Message]:
        """ALL messages after the given id (chronological); compactor's
        candidate view of the uncompacted tail. NOTE: ascending build —
        `_rows_to_messages` reverses and must NOT be reused here."""
        if after_id is not None:
            anchor = self._conn.execute(
                "SELECT rowid FROM messages WHERE id = ? AND session_id = ?", (after_id, session_id)
            ).fetchone()
            if anchor is not None:
                rows = self._conn.execute(
                    "SELECT * FROM messages WHERE session_id = ? AND rowid > ? ORDER BY created_at, rowid",
                    (session_id, anchor["rowid"]),
                ).fetchall()
                return self._ascending(rows)
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at, rowid", (session_id,)
        ).fetchall()
        return self._ascending(rows)

    @staticmethod
    def _ascending(rows: list[sqlite3.Row]) -> list[Message]:
        return [
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

    @staticmethod
    def _rows_to_messages(rows: list[sqlite3.Row]) -> list[Message]:
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
        with txn(self._conn):
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


class MemoryRepository(SqliteRepository):
    """Long-term memory store, keyed to the canonical user."""

    def add(self, memory: Memory) -> Memory:
        self._conn.execute(
            "INSERT INTO memories (id, user_id, ticket_id, kind, fact, confidence, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                memory.id,
                memory.user_id,
                memory.ticket_id,
                memory.kind.value,
                memory.fact,
                memory.confidence,
                memory.source,
                memory.created_at.isoformat(),
            ),
        )
        return memory

    def list_by_user(self, user_id: str, kind: MemoryKind | None = None) -> list[Memory]:
        if kind is None:
            rows = self._conn.execute(
                "SELECT * FROM memories WHERE user_id = ? ORDER BY created_at", (user_id,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM memories WHERE user_id = ? AND kind = ? ORDER BY created_at",
                (user_id, kind.value),
            ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    def list_all(self, kind: MemoryKind | None = None) -> list[Memory]:
        if kind is None:
            rows = self._conn.execute("SELECT * FROM memories ORDER BY created_at").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM memories WHERE kind = ? ORDER BY created_at", (kind.value,)
            ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    def list_by_ticket(self, ticket_id: str) -> list[Memory]:
        rows = self._conn.execute(
            "SELECT * FROM memories WHERE ticket_id = ? ORDER BY created_at", (ticket_id,)
        ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    @staticmethod
    def _row_to_memory(row: sqlite3.Row) -> Memory:
        return Memory(
            id=row["id"],
            user_id=row["user_id"],
            ticket_id=row["ticket_id"],
            kind=MemoryKind(row["kind"]),
            fact=row["fact"],
            confidence=row["confidence"],
            source=row["source"] if "source" in row.keys() else "",
            created_at=_parse_dt(row["created_at"]),
        )


class SessionCompactionRepository(SqliteRepository):
    """Append-only rolling-summary entries per session (pi compaction).

    Context building reads ONLY the latest entry; older rows stay for
    audit (每个数字可从存储复查).
    """

    def add(self, compaction: SessionCompaction) -> SessionCompaction:
        self._conn.execute(
            "INSERT INTO session_compactions "
            "(id, session_id, summary, first_kept_message_id, messages_compacted, chars_before, summarizer, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                compaction.id,
                compaction.session_id,
                compaction.summary,
                compaction.first_kept_message_id,
                compaction.messages_compacted,
                compaction.chars_before,
                compaction.summarizer,
                compaction.created_at.isoformat(),
            ),
        )
        return compaction

    def latest_for(self, session_id: str) -> SessionCompaction | None:
        row = self._conn.execute(
            "SELECT * FROM session_compactions WHERE session_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return SessionCompaction(
            id=row["id"],
            session_id=row["session_id"],
            summary=row["summary"],
            first_kept_message_id=row["first_kept_message_id"],
            messages_compacted=row["messages_compacted"],
            chars_before=row["chars_before"],
            summarizer=row["summarizer"],
            created_at=_parse_dt(row["created_at"]),
        )

    def count_for(self, session_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM session_compactions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return int(row[0])


class TicketStore(SqliteRepository):
    """Transactional Ticket + TicketEvent store.

    Invariant #5: ticket current state and TicketEvent are committed
    atomically in a single transaction.
    """

    def create(
        self,
        ticket: Ticket,
        *,
        trace_id: str | None = None,
        conversation_id: str | None = None,
    ) -> Ticket:
        with txn(self._conn):  # atomic
            self._insert_ticket(ticket)
            self._insert_event(
                TicketEvent(
                    id=_eid(),
                    ticket_id=ticket.id,
                    event_type=TicketEventType.CREATED,
                    trace_id=trace_id,
                    conversation_id=conversation_id,
                )
            )
        return ticket

    def transition(
        self,
        ticket_id: str,
        target: TicketStatus,
        payload: dict | None = None,
        *,
        actor_user_id: str | None = None,
        trace_id: str | None = None,
        conversation_id: str | None = None,
    ) -> Ticket:
        """Atomically move to `target` when the ticket is in the expected
        prior state. Concurrent double-claims: exactly one rowcount wins."""
        ticket = self.get(ticket_id)
        if ticket is None:
            raise KeyError(f"ticket not found: {ticket_id}")
        current = ticket.status
        validate_transition(current, target)
        with txn(self._conn):  # atomic: state change + event
            cursor = self._conn.execute(
                "UPDATE tickets SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                (target.value, _ts(), ticket_id, current.value),
            )
            if cursor.rowcount == 0:
                raise InvalidStateTransition(
                    f"ticket {ticket_id} changed concurrently: expected {current.value}"
                )
            self._insert_event(
                TicketEvent(
                    id=_eid(),
                    ticket_id=ticket_id,
                    event_type=_event_for(current, target),
                    payload=payload,
                    actor_user_id=actor_user_id,
                    trace_id=trace_id,
                    conversation_id=conversation_id,
                )
            )
        return self.get(ticket_id)  # type: ignore[return-value]

    def claim(
        self,
        ticket_id: str,
        assignee_user_id: str,
        *,
        actor_user_id: str | None = None,
        trace_id: str | None = None,
        conversation_id: str | None = None,
    ) -> Ticket:
        """Atomic OPEN + no-assignee claim. Exactly one concurrent winner.

        `claim` produces a `claimed` event and persists the assignee.
        """
        with txn(self._conn):
            cursor = self._conn.execute(
                "UPDATE tickets SET status = ?, assignee_user_id = ?, updated_at = ? "
                "WHERE id = ? AND status = ? AND assignee_user_id IS NULL",
                (TicketStatus.IN_PROGRESS.value, assignee_user_id, _ts(), ticket_id, TicketStatus.OPEN.value),
            )
            if cursor.rowcount == 0:
                ticket = self.get(ticket_id)
                if ticket is None:
                    raise KeyError(f"ticket not found: {ticket_id}")
                raise AlreadyClaimed(f"ticket {ticket_id} is already claimed or not claimable")
            self._insert_event(
                TicketEvent(
                    id=_eid(),
                    ticket_id=ticket_id,
                    event_type=TicketEventType.CLAIMED,
                    payload={"assignee_user_id": assignee_user_id},
                    actor_user_id=actor_user_id or assignee_user_id,
                    trace_id=trace_id,
                    conversation_id=conversation_id,
                )
            )
        return self.get(ticket_id)  # type: ignore[return-value]

    def set_operational(
        self,
        ticket_id: str,
        *,
        summary: str | None = None,
        category: str | None = None,
        priority: str | None = None,
        queue: str | None = None,
    ) -> Ticket:
        """Persist accepted business values (agent suggestions become ticket
        fields only through the workflow / policy — never by the agent)."""
        with txn(self._conn):
            self._conn.execute(
                "UPDATE tickets SET summary = COALESCE(?, summary), category = COALESCE(?, category), "
                "priority = COALESCE(?, priority), queue = COALESCE(?, queue), updated_at = ? WHERE id = ?",
                (summary, category, priority, queue, _ts(), ticket_id),
            )
        return self.get(ticket_id)  # type: ignore[return-value]

    def add_event(
        self,
        ticket_id: str,
        event_type: TicketEventType,
        payload: dict | None = None,
        *,
        actor_user_id: str | None = None,
        trace_id: str | None = None,
        conversation_id: str | None = None,
    ) -> TicketEvent:
        """Record an event without a status change (e.g. escalated)."""
        with txn(self._conn):
            event = TicketEvent(
                id=_eid(),
                ticket_id=ticket_id,
                event_type=event_type,
                payload=payload,
                actor_user_id=actor_user_id,
                trace_id=trace_id,
                conversation_id=conversation_id,
            )
            self._insert_event(event)
        return event

    def get(self, ticket_id: str) -> Ticket | None:
        row = self._conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return self._row_to_ticket(row) if row else None

    def stats_grouped(self, column: str) -> dict[str, int]:
        """Constrained read-only aggregation for the ticket_stats tool.

        `column` must be one of a fixed whitelist (validated by the caller);
        values are interpolated only after that check — never user input.
        """
        if column not in ("status", "queue", "category", "priority"):
            raise ValueError(f"unsupported stats column: {column}")
        rows = self._conn.execute(
            f"SELECT {column} AS k, COUNT(*) AS n FROM tickets GROUP BY {column}"  # noqa: S608 - column whitelisted above
        ).fetchall()
        return {str(r["k"]): int(r["n"]) for r in rows}

    def stats_filtered(
        self,
        column: str,
        *,
        status: str | None = None,
        queue: str | None = None,
        category: str | None = None,
        priority: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, int]:
        """Grouped counts under equality filters + an ISO time window.

        Same column whitelist as stats_grouped; all filter values are
        parameterized. Read-only (C10 问数 sub-agent execution surface).
        """
        if column not in ("status", "queue", "category", "priority"):
            raise ValueError(f"unsupported stats column: {column}")
        where = ["1=1"]
        params: list[str] = []
        if status:
            where.append("status = ?")
            params.append(status.upper())
        for field_name, value in (("queue", queue), ("category", category), ("priority", priority)):
            if value:
                where.append(f"{field_name} = ?")  # noqa: S608 - fixed column names
                params.append(value)
        if since:
            where.append("created_at >= ?")
            params.append(since)
        if until:
            where.append("created_at < ?")
            params.append(until)
        rows = self._conn.execute(
            f"SELECT {column} AS k, COUNT(*) AS n FROM tickets WHERE {' AND '.join(where)} "  # noqa: S608 - whitelisted
            "GROUP BY " + column,
            tuple(params),
        ).fetchall()
        return {str(r["k"]): int(r["n"]) for r in rows}

    def list_by_user(self, user_id: str) -> list[Ticket]:
        rows = self._conn.execute(
            "SELECT * FROM tickets WHERE user_id = ? ORDER BY created_at", (user_id,)
        ).fetchall()
        return [self._row_to_ticket(r) for r in rows]

    def list_by_queue(self, queue: str | None) -> list[Ticket]:
        if queue is None:
            rows = self._conn.execute(
                "SELECT * FROM tickets WHERE status IN ('OPEN', 'IN_PROGRESS') ORDER BY created_at"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM tickets WHERE status IN ('OPEN', 'IN_PROGRESS') AND queue = ? ORDER BY created_at",
                (queue,),
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
                created_at=_parse_dt(r["created_at"]),
                actor_user_id=r["actor_user_id"],
                trace_id=r["trace_id"],
                conversation_id=r["conversation_id"],
            )
            for r in rows
        ]

    def _insert_ticket(self, ticket: Ticket) -> None:
        self._conn.execute(
            "INSERT INTO tickets (id, user_id, title, description, status, created_at, updated_at, "
            "assignee_user_id, summary, category, priority, queue, source_conversation_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ticket.id,
                ticket.user_id,
                ticket.title,
                ticket.description,
                ticket.status.value,
                ticket.created_at.isoformat(),
                ticket.updated_at.isoformat(),
                ticket.assignee_user_id,
                ticket.summary,
                ticket.category,
                ticket.priority,
                ticket.queue,
                ticket.source_conversation_id,
            ),
        )

    def _insert_event(self, event: TicketEvent) -> None:
        self._conn.execute(
            "INSERT INTO ticket_events (id, ticket_id, event_type, payload, created_at, "
            "actor_user_id, trace_id, conversation_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.id,
                event.ticket_id,
                event.event_type.value,
                json.dumps(event.payload) if event.payload else None,
                event.created_at.isoformat(),
                event.actor_user_id,
                event.trace_id,
                event.conversation_id,
            ),
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
            assignee_user_id=row["assignee_user_id"],
            summary=row["summary"],
            category=row["category"],
            priority=row["priority"],
            queue=row["queue"],
            source_conversation_id=row["source_conversation_id"],
        )


def _eid() -> str:
    from uuid import uuid4

    return uuid4().hex[:12]


class ConversationRepository(SqliteRepository):
    """Conversations: channel + conversation id -> purpose/type/queue."""

    def create(self, conversation: Conversation) -> Conversation:
        self._conn.execute(
            "INSERT INTO conversations (id, channel, channel_conversation_id, conversation_type, "
            "purpose, queue, location, enabled, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                conversation.id,
                conversation.channel,
                conversation.channel_conversation_id,
                conversation.conversation_type.value,
                conversation.purpose.value,
                conversation.queue,
                conversation.location,
                1 if conversation.enabled else 0,
                conversation.created_at.isoformat(),
            ),
        )
        return conversation

    def find(self, channel: str, channel_conversation_id: str) -> Conversation | None:
        row = self._conn.execute(
            "SELECT * FROM conversations WHERE channel = ? AND channel_conversation_id = ?",
            (channel, channel_conversation_id),
        ).fetchone()
        return self._row(row) if row else None

    def update_config(
        self,
        channel: str,
        channel_conversation_id: str,
        *,
        conversation_type: ConversationType,
        purpose: ConversationPurpose,
        queue: str | None,
        location: str | None,
        enabled: bool,
    ) -> Conversation | None:
        """Reconcile an existing conversation's configured fields (seed is
        authoritative for type/purpose/queue/location/enabled)."""
        self._conn.execute(
            "UPDATE conversations SET conversation_type = ?, purpose = ?, queue = ?, location = ?, enabled = ? "
            "WHERE channel = ? AND channel_conversation_id = ?",
            (
                conversation_type.value,
                purpose.value,
                queue,
                location,
                1 if enabled else 0,
                channel,
                channel_conversation_id,
            ),
        )
        return self.find(channel, channel_conversation_id)

    def list_by_purpose(self, purpose: ConversationPurpose) -> list[Conversation]:
        rows = self._conn.execute(
            "SELECT * FROM conversations WHERE purpose = ? AND enabled = 1 ORDER BY created_at",
            (purpose.value,),
        ).fetchall()
        return [self._row(r) for r in rows]

    def find_operator_conversation(self, queue: str | None, channel: str | None = None) -> Conversation | None:
        """Primary operator conversation for a queue. Prefers the same
        channel when given, then any queue match, then any operator
        conversation."""
        if queue:
            if channel:
                row = self._conn.execute(
                    "SELECT * FROM conversations WHERE purpose = 'OPERATOR' AND queue = ? "
                    "AND channel = ? AND enabled = 1 ORDER BY created_at LIMIT 1",
                    (queue, channel),
                ).fetchone()
                if row:
                    return self._row(row)
            row = self._conn.execute(
                "SELECT * FROM conversations WHERE purpose = 'OPERATOR' AND queue = ? AND enabled = 1 "
                "ORDER BY created_at LIMIT 1",
                (queue,),
            ).fetchone()
            if row:
                return self._row(row)
        if channel:
            row = self._conn.execute(
                "SELECT * FROM conversations WHERE purpose = 'OPERATOR' AND channel = ? AND enabled = 1 "
                "ORDER BY created_at LIMIT 1",
                (channel,),
            ).fetchone()
            if row:
                return self._row(row)
        row = self._conn.execute(
            "SELECT * FROM conversations WHERE purpose = 'OPERATOR' AND enabled = 1 "
            "ORDER BY created_at LIMIT 1"
        ).fetchone()
        return self._row(row) if row else None

    def find_requester_conversation(self, queue: str | None, channel: str | None = None) -> Conversation | None:
        """Requester-facing (public) conversation. Prefers same channel and
        queue; used when a ticket is created outside a requester
        conversation (e.g. from an operator group)."""
        if queue:
            if channel:
                row = self._conn.execute(
                    "SELECT * FROM conversations WHERE purpose = 'REQUESTER' AND queue = ? "
                    "AND channel = ? AND enabled = 1 ORDER BY created_at LIMIT 1",
                    (queue, channel),
                ).fetchone()
                if row:
                    return self._row(row)
            row = self._conn.execute(
                "SELECT * FROM conversations WHERE purpose = 'REQUESTER' AND queue = ? AND enabled = 1 "
                "ORDER BY created_at LIMIT 1",
                (queue,),
            ).fetchone()
            if row:
                return self._row(row)
        if channel:
            row = self._conn.execute(
                "SELECT * FROM conversations WHERE purpose = 'REQUESTER' AND channel = ? AND enabled = 1 "
                "ORDER BY created_at LIMIT 1",
                (channel,),
            ).fetchone()
            if row:
                return self._row(row)
        return None

    def find_approval_conversation(self) -> Conversation | None:
        row = self._conn.execute(
            "SELECT * FROM conversations WHERE purpose = 'APPROVAL' AND enabled = 1 "
            "ORDER BY created_at LIMIT 1"
        ).fetchone()
        return self._row(row) if row else None

    def find_by_channel_conversation_id(self, channel_conversation_id: str) -> Conversation | None:
        row = self._conn.execute(
            "SELECT * FROM conversations WHERE channel_conversation_id = ? AND enabled = 1 "
            "ORDER BY created_at LIMIT 1",
            (channel_conversation_id,),
        ).fetchone()
        return self._row(row) if row else None

    def list_all(self) -> list[Conversation]:
        rows = self._conn.execute("SELECT * FROM conversations ORDER BY created_at").fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row: sqlite3.Row) -> Conversation:
        return Conversation(
            id=row["id"],
            channel=row["channel"],
            channel_conversation_id=row["channel_conversation_id"],
            conversation_type=ConversationType(row["conversation_type"]),
            purpose=ConversationPurpose(row["purpose"]),
            queue=row["queue"],
            location=row["location"],
            enabled=bool(row["enabled"]),
            created_at=_parse_dt(row["created_at"]),
        )


class RoleRepository(SqliteRepository):
    """Role assignments on canonical users."""

    def create(self, assignment: RoleAssignment) -> RoleAssignment:
        self._conn.execute(
            "INSERT INTO user_roles (id, user_id, role, queue, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                assignment.id,
                assignment.user_id,
                assignment.role.value,
                assignment.queue,
                assignment.created_at.isoformat(),
            ),
        )
        return assignment

    def has_role(self, user_id: str, role: UserRole) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM user_roles WHERE user_id = ? AND role = ?", (user_id, role.value)
        ).fetchone()
        return row is not None

    def list_by_user(self, user_id: str) -> list[RoleAssignment]:
        rows = self._conn.execute(
            "SELECT * FROM user_roles WHERE user_id = ?", (user_id,)
        ).fetchall()
        return [
            RoleAssignment(
                id=r["id"],
                user_id=r["user_id"],
                role=UserRole(r["role"]),
                queue=r["queue"],
                created_at=_parse_dt(r["created_at"]),
            )
            for r in rows
        ]


class PendingActionRepository(SqliteRepository):
    """HITL pending actions: the executable unit behind an approval."""

    def create(self, action: PendingAction) -> PendingAction:
        self._conn.execute(
            "INSERT INTO pending_actions (id, ticket_id, action_type, payload, requested_by, "
            "approval_id, execution_status, executed_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                action.id,
                action.ticket_id,
                action.action_type.value,
                json.dumps(action.payload) if action.payload else None,
                action.requested_by,
                action.approval_id,
                action.execution_status.value,
                action.executed_at.isoformat() if action.executed_at else None,
                action.created_at.isoformat(),
            ),
        )
        return action

    def get(self, action_id: str) -> PendingAction | None:
        row = self._conn.execute(
            "SELECT * FROM pending_actions WHERE id = ?", (action_id,)
        ).fetchone()
        return self._row(row) if row else None

    def list_by_ticket(self, ticket_id: str) -> list[PendingAction]:
        rows = self._conn.execute(
            "SELECT * FROM pending_actions WHERE ticket_id = ? ORDER BY created_at", (ticket_id,)
        ).fetchall()
        return [self._row(r) for r in rows]

    def list_all(self) -> list[PendingAction]:
        rows = self._conn.execute("SELECT * FROM pending_actions ORDER BY created_at").fetchall()
        return [self._row(r) for r in rows]

    def mark_executed(self, action_id: str, *, expect_status: str = "PENDING") -> bool:
        """Exactly-once execution guard (CAS on execution_status)."""
        with txn(self._conn):
            cursor = self._conn.execute(
                "UPDATE pending_actions SET execution_status = 'EXECUTED', executed_at = ? "
                "WHERE id = ? AND execution_status = ?",
                (_ts(), action_id, expect_status),
            )
        return cursor.rowcount == 1

    def mark_skipped(self, action_id: str) -> bool:
        with txn(self._conn):
            cursor = self._conn.execute(
                "UPDATE pending_actions SET execution_status = 'SKIPPED' "
                "WHERE id = ? AND execution_status = 'PENDING'",
                (action_id,),
            )
        return cursor.rowcount == 1

    @staticmethod
    def _row(row: sqlite3.Row) -> PendingAction:
        return PendingAction(
            id=row["id"],
            ticket_id=row["ticket_id"],
            action_type=ApprovableAction(row["action_type"]),
            payload=json.loads(row["payload"]) if row["payload"] else None,
            requested_by=row["requested_by"],
            approval_id=row["approval_id"],
            execution_status=PendingActionStatus(row["execution_status"]),
            executed_at=_parse_dt(row["executed_at"]) if row["executed_at"] else None,
            created_at=_parse_dt(row["created_at"]),
        )


class NotificationOutboxRepository(SqliteRepository):
    """Transactional outbox: business event + outbox record commit together."""

    def add(self, record: NotificationRecord) -> NotificationRecord:
        self._conn.execute(
            "INSERT INTO notification_outbox (id, source_event_id, notification_type, visibility, "
            "target_type, target_key, message, status, attempt_count, ticket_id, trace_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.source_event_id,
                record.notification_type.value,
                record.visibility.value,
                record.target_type,
                record.target_key,
                record.message,
                record.status.value,
                record.attempt_count,
                record.ticket_id,
                record.trace_id,
                record.created_at.isoformat(),
            ),
        )
        return record

    def pending(self, limit: int = 100) -> list[NotificationRecord]:
        rows = self._conn.execute(
            "SELECT * FROM notification_outbox WHERE status = 'pending' ORDER BY created_at LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row(r) for r in rows]

    def failed(self, limit: int = 100) -> list[NotificationRecord]:
        rows = self._conn.execute(
            "SELECT * FROM notification_outbox WHERE status = 'failed' ORDER BY created_at LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row(r) for r in rows]

    def get(self, outbox_id: str) -> NotificationRecord | None:
        row = self._conn.execute(
            "SELECT * FROM notification_outbox WHERE id = ?", (outbox_id,)
        ).fetchone()
        return self._row(row) if row else None

    def list_by_ticket(self, ticket_id: str) -> list[NotificationRecord]:
        rows = self._conn.execute(
            "SELECT * FROM notification_outbox WHERE ticket_id = ? ORDER BY created_at", (ticket_id,)
        ).fetchall()
        return [self._row(r) for r in rows]

    def mark(self, outbox_id: str, status: OutboxStatus, attempt: int, result_code: str | None = None) -> None:
        with txn(self._conn):
            self._conn.execute(
                "UPDATE notification_outbox SET status = ?, attempt_count = ?, "
                "result_code = COALESCE(?, result_code) WHERE id = ?",
                (status.value, attempt, result_code, outbox_id),
            )

    def add_attempt(
        self, outbox_id: str, attempt_number: int, success: bool, result_code: str | None, error: str | None
    ) -> None:
        self._conn.execute(
            "INSERT INTO delivery_attempts (id, outbox_id, attempt_number, success, result_code, error, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_eid(), outbox_id, attempt_number, 1 if success else 0, result_code, error, _ts()),
        )

    def attempts(self, outbox_id: str) -> list[dict]:
        """Delivery attempt history for one outbox record (case trace)."""
        rows = self._conn.execute(
            "SELECT attempt_number, success, result_code, error, created_at "
            "FROM delivery_attempts WHERE outbox_id = ? ORDER BY attempt_number",
            (outbox_id,),
        ).fetchall()
        return [
            {
                "attempt_number": r["attempt_number"],
                "success": bool(r["success"]),
                "result_code": r["result_code"],
                "error": r["error"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    @staticmethod
    def _row(row: sqlite3.Row) -> NotificationRecord:
        return NotificationRecord(
            id=row["id"],
            source_event_id=row["source_event_id"],
            notification_type=NotificationType(row["notification_type"]),
            visibility=Visibility(row["visibility"]),
            target_type=row["target_type"],
            target_key=row["target_key"],
            message=row["message"],
            status=OutboxStatus(row["status"]),
            attempt_count=row["attempt_count"],
            ticket_id=row["ticket_id"],
            trace_id=row["trace_id"],
            created_at=_parse_dt(row["created_at"]),
        )


class SessionTicketContextRepository(SqliteRepository):
    """Persisted session -> ticket context (requester side).

    This is a hint for continuation, NOT the resolution source of truth:
    the user-centric TicketResolver remains the primary algorithm.
    """

    def set(self, session_id: str, ticket_id: str) -> None:
        self._conn.execute(
            "INSERT INTO session_ticket_contexts (session_id, ticket_id, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET ticket_id = excluded.ticket_id, updated_at = excluded.updated_at",
            (session_id, ticket_id, _ts()),
        )

    def get(self, session_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT ticket_id FROM session_ticket_contexts WHERE session_id = ?", (session_id,)
        ).fetchone()
        return row["ticket_id"] if row else None


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
    "MemoryRepository",
    "TicketStore",
    "TicketRepository",
    "ApprovalRepository",
    "ConversationRepository",
    "RoleRepository",
    "PendingActionRepository",
    "NotificationOutboxRepository",
    "SessionTicketContextRepository",
    "InvalidStateTransition",
    "AlreadyClaimed",
]
