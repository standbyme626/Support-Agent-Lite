"""C2 hybrid retrieval: TF-IDF keyword + embedding vector recall, RRF
fusion, optional cross-encoder rerank — with deterministic degradation.

Contract (same as the legacy Retriever): `search(query, top_k)` returns
a ranked list of RetrievalHit. Any failure in the vector/rerank path is
contained here and silently degrades to keyword-only results; the
retrieval pipeline can never fail because an external service did.

E2E fixes (2026-08-28):
- candidate_k 8 -> 30: 「请假流程是什么」双路 top-8 都不含请假类文档,
  rerank 再强也救不回候选集外的文档(实测 rerank 年假 0.62 vs 差旅 0.03)。
- answer() 不再透传 keyword-only,走本类 search() 全管线(此前 FAQ 答复
  从未经过向量+rerank——实测根因)。
- last_rerank 可观测:rerank 是否生效/分数/候选数暴露给 trace。
"""
from __future__ import annotations

import sys
from pathlib import Path

from app.application.retriever import FaqDocument, RAGAnswer, RetrievalHit, Retriever
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
        candidate_k: int = 30,
    ) -> None:
        self._keyword = keyword
        self._embedding = embedding
        self._store = store
        self._reranker = reranker
        self._candidate_k = candidate_k
        self.last_rerank: dict | None = None

    @property
    def documents(self) -> list[FaqDocument]:
        return self._keyword.documents

    def search(self, query: str, top_k: int = 3) -> list[RetrievalHit]:
        self.last_rerank = None
        keyword_hits = self._keyword.search(query, top_k=max(top_k, self._candidate_k))
        vector_hits: list[tuple[str, float]] = []
        try:
            if self._embedding is not None and self._store is not None:
                qvec = self._embedding.embed([query])[0]
                vector_hits = self._store.search(qvec, top_k=self._candidate_k)
        except Exception as exc:  # noqa: BLE001 - degrade to keyword-only
            print(f"[hybrid] vector path degraded: {exc!r}", file=sys.stderr)
            self.last_rerank = {"reranked": False, "degraded": "vector", "candidates": len(keyword_hits)}
            return keyword_hits[:top_k]
        if not vector_hits:
            self.last_rerank = {"reranked": False, "degraded": "no-vector-hits", "candidates": len(keyword_hits)}
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
                self.last_rerank = {
                    "reranked": True,
                    "candidates": len(candidates),
                    "top": [{"doc_id": h.document.doc_id, "score": round(s, 4)} for h, s in pairs[:top_k]],
                }
                reranked = []
                for hit, s in pairs[:top_k]:
                    doc = by_id.get(hit.document.doc_id) or hit.document
                    reranked.append(RetrievalHit(doc, round(s, 4)))
                if reranked:
                    return reranked
            except Exception as exc:  # noqa: BLE001 - keep fused order
                print(f"[hybrid] rerank degraded: {exc!r}", file=sys.stderr)
                self.last_rerank = {"reranked": False, "degraded": "rerank", "candidates": len(candidates)}
        return fused[:top_k]

    def answer(
        self,
        query: str,
        *,
        top_k: int = 3,
        min_score: float = 0.25,
        min_query_terms: int = 2,
        min_matched_terms: int = 2,
    ) -> RAGAnswer | None:
        """Grounded answer through the FULL hybrid pipeline (E2E fix 3A).

        Mirrors Retriever.answer's no-answer contract (invariant #7); the
        difference is search() runs vector recall + rerank first.
        """
        terms = self._keyword._tokenize(query)  # noqa: SLF001
        if len(terms) < min_query_terms:
            return None
        hits = self.search(query, top_k=top_k)
        if not hits:
            return None
        best = hits[0]
        if best.score < min_score:
            return None
        haystack = Retriever._haystack(best.document)  # noqa: SLF001
        matched_count = sum(1 for term in terms if term in haystack)
        if matched_count < min_matched_terms:
            return None
        text = Retriever._format_answer(best)  # noqa: SLF001
        return RAGAnswer(text=text, hits=hits)

    # convenience passthroughs used elsewhere in the codebase
    @property
    def idf(self) -> dict[str, float]:
        return self._keyword.idf