"""C2 hybrid retrieval: TF-IDF keyword + embedding vector recall, RRF
fusion, optional cross-encoder rerank — with deterministic degradation.

Contract (same as the legacy Retriever): `search(query, top_k)` returns
a ranked list of RetrievalHit. Any failure in the vector/rerank path is
contained here and silently degrades to keyword-only results; the
retrieval pipeline can never fail because an external service did.
"""
from __future__ import annotations

import sys
from pathlib import Path

from app.application.retriever import FaqDocument, RetrievalHit, Retriever
from app.infrastructure.vector_store import NumpyVectorStore

RRF_K = 60


def rrf_fuse(
    keyword_hits: list[RetrievalHit],
    vector_hits: list[tuple[str, float]],
    k: int = RRF_K,
) -> list[RetrievalHit]:
    """Reciprocal-rank fusion of two ranked lists into one.

    Documents appearing in both lists get combined evidence; scores keep
    the keyword TF-IDF value so downstream gates behave unchanged.
    """
    docs = {h.document.doc_id: h for h in keyword_hits}
    fused: dict[str, float] = {}
    for rank, hit in enumerate(keyword_hits):
        fused[hit.document.doc_id] = fused.get(hit.document.doc_id, 0.0) + 1.0 / (k + rank + 1)
    for rank, (doc_id, _score) in enumerate(vector_hits):
        fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
        if doc_id not in docs:
            # vector-only document: fabricate a minimal doc shell
            docs[doc_id] = RetrievalHit(FaqDocument(doc_id=doc_id, title=doc_id, content=""), 0.0)
    ordered_ids = sorted(fused, key=lambda d: -fused[d])
    return [docs[d] for d in ordered_ids]


class HybridRetriever:
    """Drop-in replacement for Retriever when a vector index exists."""

    def __init__(
        self,
        keyword: Retriever,
        *,
        embedding=None,
        store: NumpyVectorStore | None = None,
        reranker=None,
        candidate_k: int = 8,
    ) -> None:
        self._keyword = keyword
        self._embedding = embedding
        self._store = store
        self._reranker = reranker
        self._candidate_k = candidate_k

    @property
    def documents(self) -> list[FaqDocument]:
        return self._keyword.documents

    def search(self, query: str, top_k: int = 3) -> list[RetrievalHit]:
        keyword_hits = self._keyword.search(query, top_k=max(top_k, self._candidate_k))
        vector_hits: list[tuple[str, float]] = []
        try:
            if self._embedding is not None and self._store is not None:
                qvec = self._embedding.embed([query])[0]
                vector_hits = self._store.search(qvec, top_k=self._candidate_k)
        except Exception as exc:  # noqa: BLE001 - degrade to keyword-only
            print(f"[hybrid] vector path degraded: {exc!r}", file=sys.stderr)
            return keyword_hits[:top_k]
        if not vector_hits:
            return keyword_hits[:top_k]
        by_id = {d.doc_id: d for d in self._keyword.documents}
        fused = rrf_fuse(keyword_hits, vector_hits)
        candidates = fused[: max(top_k * 2, self._candidate_k)]
        if self._reranker is not None and candidates:
            try:
                texts = []
                for hit in candidates:
                    doc = by_id.get(hit.document.doc_id)
                    texts.append(f"{hit.document.title} {(doc.content if doc else '')[:400]}")
                scores = self._reranker.rerank(query, texts)
                pairs = sorted(
                    zip(candidates, scores), key=lambda p: -p[1]
                )
                reranked = []
                for hit, s in pairs[:top_k]:
                    doc = by_id.get(hit.document.doc_id) or hit.document
                    reranked.append(RetrievalHit(doc, round(s, 4)))
                if reranked:
                    return reranked
            except Exception as exc:  # noqa: BLE001 - keep fused order
                print(f"[hybrid] rerank degraded: {exc!r}", file=sys.stderr)
        return fused[:top_k]

    # convenience passthroughs used elsewhere in the codebase
    def answer(self, question: str) -> object:
        return self._keyword.answer(question)

    @property
    def idf(self) -> dict[str, float]:
        return self._keyword.idf
