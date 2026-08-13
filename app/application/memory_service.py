"""MemoryService: extraction on close + next-session recall.

Long-term memory lifecycle:
    ticket CLOSED -> remember() extracts stable facts (AC-09)
    new session  -> recall() matches the new message against the user's
                    facts and supplies them as context (AC-10)
"""
from __future__ import annotations

from dataclasses import dataclass

from app.application.memory_extractor import MemoryExtractor
from app.application.retriever import tokenize
from app.domain.memory import Memory, MemoryKind
from app.infrastructure.repositories import MemoryRepository, TicketStore


@dataclass
class RecallHit:
    memory: Memory
    score: float

    @property
    def fact(self) -> str:
        return self.memory.fact


class MemoryService:
    """Stores and retrieves canonical-user memory."""

    def __init__(
        self,
        store: TicketStore,
        memories: MemoryRepository,
        extractor: MemoryExtractor | None = None,
    ) -> None:
        self._store = store
        self._memories = memories
        self._extractor = extractor or MemoryExtractor()

    def remember(self, ticket_id: str) -> list[Memory]:
        """Extract and store memory for a CLOSED ticket (idempotent)."""
        existing = self._memories.list_by_ticket(ticket_id)
        if existing:
            return existing
        ticket = self._store.get(ticket_id)
        if ticket is None:
            raise KeyError(f"ticket not found: {ticket_id}")
        if ticket.status != "CLOSED":
            raise ValueError(f"cannot extract memory from non-closed ticket: {ticket.status.value}")
        result = self._extractor.extract(ticket, events=self._store.events(ticket_id))
        for memory in result.memories:
            self._memories.add(memory)
        return result.memories

    def recall(
        self,
        user_id: str,
        text: str,
        *,
        top_k: int = 3,
        min_score: float = 0.20,
    ) -> list[RecallHit]:
        """Match a new message against the user's stored facts."""
        terms = tokenize(text)
        if not terms:
            return []
        hits: list[RecallHit] = []
        for memory in self._memories.list_by_user(user_id):
            score = self._score(terms, memory.fact)
            if score >= min_score and self._matched_count(terms, memory.fact) >= 1:
                hits.append(RecallHit(memory=memory, score=score))
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:top_k]

    def list(self, user_id: str | None = None, kind: MemoryKind | None = None) -> list[Memory]:
        if user_id is None:
            return self._memories.list_all(kind)
        return self._memories.list_by_user(user_id, kind)

    @staticmethod
    def _score(terms: set[str], fact: str) -> float:
        matched = sum(1 for term in terms if term in fact)
        if matched == 0:
            return 0.0
        return matched / len(terms)

    @staticmethod
    def _matched_count(terms: set[str], fact: str) -> int:
        return sum(1 for term in terms if term in fact)
