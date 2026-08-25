"""Retriever: local lexical FAQ retrieval with source attribution.

REWRITE of the legacy retrieval core (no vector index, no embeddings):
query terms are ASCII words plus CJK bigrams; documents are scored by
term overlap on title + content + tags.

Invariant #7: low-confidence retrieval must NOT become free-form model
answers. `answer()` returns None when the top hit is below threshold,
and the workflow then falls back to an explicit no-answer reply.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

_CJK = r"\u4e00-\u9fff\u3400-\u4dbf"
_ASCII_TERM = re.compile(r"[a-z0-9]+")
_CJK_TERM = re.compile(rf"[{_CJK}]")

# Interrogative function-word bigrams carry no topic signal; letting them
# match lets chit-chat queries ("今天天气怎么样") collide with unrelated
# entries whose titles contain question phrasing.
_QUESTION_STOPWORDS = frozenset({"怎么", "什么", "么样", "如何", "为什么", "咋么", "多少"})


def tokenize(text: str) -> set[str]:
    """ASCII words + CJK bigrams. Single CJK chars are excluded: they
    are too noisy for grounding decisions (e.g. 你/好 matching any doc)."""
    normalized = text.lower()
    terms = set(_ASCII_TERM.findall(normalized))
    cjk = "".join(_CJK_TERM.findall(normalized))
    if len(cjk) > 1:
        terms.update(cjk[i : i + 2] for i in range(len(cjk) - 1))
    return terms


@dataclass(frozen=True)
class FaqDocument:
    doc_id: str
    title: str
    content: str
    source_type: str = "faq"
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetrievalHit:
    document: FaqDocument
    score: float

    @property
    def source(self) -> str:
        return f"{self.document.source_type}:{self.document.doc_id}"


@dataclass
class RAGAnswer:
    """A grounded answer: reply text plus the sources it was built from."""

    text: str
    hits: list[RetrievalHit]


class Retriever:
    """Loads FAQ documents from a seed dir and ranks them against a query."""

    def __init__(self, seed_dir: str | Path) -> None:
        self._documents: list[FaqDocument] = []
        self._load(Path(seed_dir))
        self._idf: dict[str, float] = self._compute_idf()

    @property
    def documents(self) -> list[FaqDocument]:
        return list(self._documents)

    def search(self, query: str, *, top_k: int = 3) -> list[RetrievalHit]:
        terms = self._tokenize(query)
        if not terms:
            return []
        scored: list[RetrievalHit] = []
        for doc in self._documents:
            score = self._score(doc, terms, self._idf)
            if score > 0.0:
                scored.append(RetrievalHit(document=doc, score=score))
        scored.sort(key=lambda hit: hit.score, reverse=True)
        return scored[:top_k]

    def answer(
        self,
        query: str,
        *,
        top_k: int = 3,
        min_score: float = 0.25,
        min_query_terms: int = 2,
        min_matched_terms: int = 2,
    ) -> RAGAnswer | None:
        """Return a grounded answer, or None when confidence is too low.

        No-answer protection (invariant #7): the top hit must clear a
        score threshold, carry enough query signal, AND match at least
        two distinct terms — a single common-word collision (e.g. 天气
        inside a hardware entry) must never ground an unrelated query.
        """
        terms = self._tokenize(query)
        if len(terms) < min_query_terms:
            return None
        hits = self.search(query, top_k=top_k)
        if not hits:
            return None
        best = hits[0]
        if best.score < min_score:
            return None
        haystack = self._haystack(best.document)
        matched_count = sum(1 for term in terms if term in haystack)
        if matched_count < min_matched_terms:
            return None
        text = self._format_answer(best)
        return RAGAnswer(text=text, hits=hits)

    def _load(self, seed_dir: Path) -> None:
        if not seed_dir.exists():
            return
        for path in sorted(seed_dir.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                continue
            for raw in payload:
                if not isinstance(raw, dict):
                    continue
                self._documents.append(
                    FaqDocument(
                        doc_id=str(raw["doc_id"]),
                        title=str(raw["title"]),
                        content=str(raw["content"]),
                        source_type=str(raw.get("source_type", "faq")),
                        tags=tuple(str(t) for t in raw.get("tags", ())),
                    )
                )

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        return tokenize(text) - _QUESTION_STOPWORDS

    def _compute_idf(self) -> dict[str, float]:
        """Document-frequency weights: distinctive terms dominate scoring."""
        n = len(self._documents)
        if n == 0:
            return {}
        df: dict[str, int] = {}
        for doc in self._documents:
            for term in self._tokenize(self._haystack(doc)):
                df[term] = df.get(term, 0) + 1
        return {term: math.log(1.0 + n / (1 + count)) for term, count in df.items()}

    @classmethod
    def _score(cls, doc: FaqDocument, terms: set[str], idf: dict[str, float]) -> float:
        """IDF-weighted overlap: distinctive matched terms win, and terms
        that match nothing in the corpus still count against the score.
        """
        max_idf = max(idf.values(), default=1.0)
        get = lambda term: idf.get(term, max_idf)  # noqa: E731
        haystack = cls._haystack(doc)
        matched = [term for term in terms if term in haystack]
        if not matched:
            return 0.0
        total = sum(get(term) for term in terms)
        weight = sum(get(term) for term in matched)
        title_hits = sum(get(term) for term in matched if term in doc.title.lower())
        return (weight + 0.5 * title_hits) / total

    @staticmethod
    def _haystack(doc: FaqDocument) -> str:
        return f"{doc.title} {doc.content} {' '.join(doc.tags)}".lower()

    @staticmethod
    def _format_answer(best: RetrievalHit) -> str:
        doc = best.document
        return f"【{doc.doc_id} {doc.title}】{doc.content}\n（来源：{doc.doc_id} {doc.title}）"
