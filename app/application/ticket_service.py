"""TicketResolver and TicketService.

Phase 2 core. Resolution order (never LLM-random):

    explicit ticket id in message
        → session's active ticket
            → user's active tickets
                → 0: create new ticket
                → 1: that ticket
                → >1: clarification (candidate list)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from app.domain.ticket import Ticket, TicketStatus
from app.infrastructure.repositories import TicketStore

EXPLICIT_TICKET_RE = re.compile(r"\b(T\d{3,})\b")


class ResolutionKind(str, Enum):
    EXPLICIT = "explicit"
    SESSION = "session"
    ONLY_ACTIVE = "only_active"
    CREATE_NEW = "create_new"
    CLARIFY = "clarify"


@dataclass
class TicketResolution:
    kind: ResolutionKind
    ticket: Ticket | None = None
    candidates: list[Ticket] = field(default_factory=list)


class TicketService:
    """Ticket operations (create/claim/resolve/close) with event guarantees."""

    def __init__(self, store: TicketStore) -> None:
        self._store = store

    def create(self, user_id: str, title: str, description: str) -> Ticket:
        ticket = Ticket(id=new_ticket_id(self._store), user_id=user_id, title=title, description=description)
        return self._store.create(ticket)

    def claim(self, ticket_id: str) -> Ticket:
        return self._store.transition(ticket_id, TicketStatus.IN_PROGRESS)

    def resolve(self, ticket_id: str, payload: dict | None = None) -> Ticket:
        return self._store.transition(ticket_id, TicketStatus.RESOLVED, payload)

    def close(self, ticket_id: str, payload: dict | None = None) -> Ticket:
        return self._store.transition(ticket_id, TicketStatus.CLOSED, payload)

    def active_tickets(self, user_id: str) -> list[Ticket]:
        return [
            t
            for t in self._store.list_by_user(user_id)
            if t.status in (TicketStatus.OPEN, TicketStatus.IN_PROGRESS)
        ]

    def recent(self, user_id: str) -> Ticket | None:
        """Most recent ticket of a user regardless of status (progress queries)."""
        tickets = self._store.list_by_user(user_id)
        return tickets[-1] if tickets else None

    def get(self, ticket_id: str) -> Ticket | None:
        return self._store.get(ticket_id)

    def list_by_queue(self, queue: str | None) -> list[Ticket]:
        return self._store.list_by_queue(queue)

    def set_operational(
        self,
        ticket_id: str,
        *,
        summary: str | None = None,
        category: str | None = None,
        priority: str | None = None,
        queue: str | None = None,
    ) -> Ticket:
        return self._store.set_operational(ticket_id, summary=summary, category=category, priority=priority, queue=queue)


class TicketResolver:
    """Decides which ticket a message refers to, for a canonical user.

    Resolution never consults the LLM (invariant: no random pick).
    """

    def __init__(self, service: TicketService, store: TicketStore) -> None:
        self._service = service
        self._store = store

    def resolve(
        self,
        text: str,
        user_id: str,
        session_ticket_id: str | None = None,
    ) -> TicketResolution:
        explicit = self._find_explicit(text, user_id)
        if explicit is not None:
            return TicketResolution(kind=ResolutionKind.EXPLICIT, ticket=explicit)

        if session_ticket_id:
            ticket = self._store.get(session_ticket_id)
            if ticket is not None and ticket.user_id == user_id:
                return TicketResolution(kind=ResolutionKind.SESSION, ticket=ticket)

        active = self._service.active_tickets(user_id)
        if not active:
            return TicketResolution(kind=ResolutionKind.CREATE_NEW)
        if len(active) == 1:
            return TicketResolution(kind=ResolutionKind.ONLY_ACTIVE, ticket=active[0])
        return TicketResolution(kind=ResolutionKind.CLARIFY, candidates=active)

    def _find_explicit(self, text: str, user_id: str) -> Ticket | None:
        match = EXPLICIT_TICKET_RE.search(text)
        if not match:
            return None
        ticket = self._store.get(match.group(1))
        if ticket is None or ticket.user_id != user_id:
            return None
        return ticket


def new_ticket_id(store: TicketStore) -> str:
    """Next T1001-style id (highest existing numeric suffix + 1)."""
    rows = store._conn.execute("SELECT id FROM tickets").fetchall()  # noqa: SLF001
    max_seq = 0
    for row in rows:
        m = re.fullmatch(r"T(\d+)", row["id"])
        if m:
            max_seq = max(max_seq, int(m.group(1)))
    return f"T{max_seq + 1:04d}"
