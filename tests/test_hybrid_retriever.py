"""C2: vector hybrid retrieval — RRF fusion over TF-IDF + embeddings,
optional rerank, graceful degradation to keyword-only."""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from app.application.hybrid_retriever import HybridRetriever, rrf_fuse
from app.application.retriever import FaqDocument, RetrievalHit, Retriever
from app.infrastructure.vector_store import NumpyVectorStore

SEED_DIR = Path(__file__).resolve().parent.parent / "seed" / "faq"


class FakeEmbedding:
    """Deterministic bag-of-character hashing vectors (no network)."""

    def __init__(self, dim: int = 64) -> None:
        self.dim = dim
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        vecs = []
        for t in texts:
            v = [0.0] * self.dim
            for ch in t:
                v[ord(ch) % self.dim] += 1.0
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            vecs.append([x / norm for x in v])
        return vecs


class FakeReranker:
    """Reverses relevance so fusion order changes are observable."""

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        n = len(documents)
        return [float(n - i) / n for i in range(n)]


class ExplodingEmbedding:
    def embed(self, texts):  # noqa: ANN001
        raise RuntimeError("embedding backend down")


class ReversingReranker:
    """Truly reverses fused order (ascending score) so rerank effect is
    observable: top1 after rerank == bottom of the fused candidate list."""

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        n = len(documents)
        return [float(i + 1) / n for i in range(n)]


# --- numpy store roundtrip ----------------------------------------------------


def test_numpy_store_roundtrip_and_ranking(tmp_path):
    emb = FakeEmbedding()
    store = NumpyVectorStore(tmp_path)
    docs = [
        {"doc_id": "d1", "text": "空调制冷剂不足需要加注", "title": "空调", "category": "device"},
        {"doc_id": "d2", "text": "打印机卡纸如何取出纸张", "title": "打印机", "category": "device"},
    ]
    vecs = emb.embed([d["text"] for d in docs])
    store.build(docs, vecs)
    store.save()
    loaded = NumpyVectorStore(tmp_path)
    assert loaded.load()
    q = emb.embed(["空调不制冷"])[0]
    hits = loaded.search(q, top_k=2)
    assert hits[0][0] == "d1"
    assert 1.0 >= hits[0][1] > 0.0


def test_numpy_store_load_missing_returns_false(tmp_path):
    assert NumpyVectorStore(tmp_path).load() is False


# --- RRF fusion -----------------------------------------------------------------


def _hit(doc_id: str, score: float) -> RetrievalHit:
    return RetrievalHit(FaqDocument(doc_id=doc_id, title=doc_id, content="c"), score)


def test_rrf_interleaves_ranked_lists():
    kw = [_hit("a", 0.9), _hit("b", 0.8), _hit("c", 0.7)]
    vec = [("a", 0.95), ("b", 0.85), ("d", 0.6)]
    fused = rrf_fuse(kw, vec, k=2)
    ids = [f.document.doc_id for f in fused]
    # a leads both lists -> first; single-list docs c/d trail the two-list doc b
    assert ids == ["a", "b", "c", "d"]


def test_rrf_dedupes_documents():
    kw = [_hit("a", 0.9), _hit("b", 0.5)]
    vec = [("a", 0.8)]
    fused = rrf_fuse(kw, vec)
    ids = [f.document.doc_id for f in fused]
    assert len(ids) == len(set(ids)) == 2


# --- hybrid retriever -------------------------------------------------------------


@pytest.fixture(scope="module")
def keyword_retriever() -> Retriever:
    return Retriever(SEED_DIR)


def test_hybrid_without_vector_equals_keyword(keyword_retriever):
    h = HybridRetriever(keyword_retriever)
    out = h.search("空调不制冷怎么办", top_k=3)
    assert out and all(isinstance(r, RetrievalHit) for r in out)


def test_hybrid_uses_vector_when_available(keyword_retriever, tmp_path):
    emb = FakeEmbedding()
    store = NumpyVectorStore(tmp_path)
    docs = [
        {"doc_id": d.doc_id, "text": f"{d.title} {d.content}", "title": d.title,
         "category": "", "source_type": d.source_type}
        for d in keyword_retriever.documents[:20]
    ]
    store.build(docs, emb.embed([d["text"] for d in docs]))
    h = HybridRetriever(keyword_retriever, embedding=emb, store=store, reranker=FakeReranker())
    out = h.search("电脑蓝屏无法开机", top_k=3)
    assert out
    assert emb.calls >= 1


def test_hybrid_degrades_when_embedding_backend_down(keyword_retriever):
    h = HybridRetriever(keyword_retriever, embedding=ExplodingEmbedding(), store=None)
    out = h.search("空调不制冷怎么办", top_k=3)
    assert out  # keyword-only fallback, no exception


# --- E2E 修复用例(2026-08-28):hybrid answer 全管线 + rerank 可观测 ------------


def test_hybrid_answer_uses_full_pipeline(tmp_path):
    """修复 3A:answer() 必须走向量+rerank 全管线,而非 keyword-only 透传。

    ReversingReranker 把融合序完全倒转 -> rerank top1 是与查询无关的文档,
    grounding 门(Invariant 7)拒答(None);keyword-only 却会给出 d1 蓝屏答案。
    两者差异即"answer 走了全管线"的证据。
    """
    import json

    docs = [
        {"doc_id": "d1", "title": "蓝屏", "content": "电脑蓝屏了怎么处理 蓝屏重启", "source_type": "faq"},
        {"doc_id": "d2", "title": "空调", "content": "空调不制冷需要加注制冷剂", "source_type": "faq"},
        {"doc_id": "d3", "title": "年假", "content": "年假申请流程在HR系统提交", "source_type": "faq"},
    ]
    (tmp_path / "kb.json").write_text(json.dumps(docs), encoding="utf-8")
    emb = FakeEmbedding()
    store = NumpyVectorStore(tmp_path / "store")
    store.build(docs, emb.embed([d["content"] for d in docs]))

    mini = Retriever(tmp_path)
    kw = mini.answer("电脑蓝屏了")
    assert kw is not None and kw.hits[0].document.doc_id == "d1"

    h = HybridRetriever(mini, embedding=emb, store=store, reranker=ReversingReranker())
    a = h.answer("电脑蓝屏了")
    assert a is None  # rerank 把无关文档顶上来 -> grounding 拒答
    assert h.last_rerank is not None and h.last_rerank["reranked"] is True
    assert h.search("电脑蓝屏了", top_k=1)[0].document.doc_id != "d1"  # rerank 生效


def test_hybrid_answer_grounding_gate_still_holds(keyword_retriever, tmp_path):
    """修复 3A 不破坏 Invariant 7:低重叠查询仍拒答(None)。"""
    emb = FakeEmbedding()
    store = NumpyVectorStore(tmp_path)
    docs = [
        {"doc_id": d.doc_id, "text": f"{d.title} {d.content}", "title": d.title,
         "category": "", "source_type": d.source_type}
        for d in keyword_retriever.documents[:20]
    ]
    store.build(docs, emb.embed([d["text"] for d in docs]))
    h = HybridRetriever(keyword_retriever, embedding=emb, store=store, reranker=FakeReranker())
    assert h.answer("量子力学今天学什么") is None


def test_hybrid_answer_degrades_without_vector(keyword_retriever):
    """向量缺失时 answer 与 keyword-only 同结果(降级契约)。"""
    h = HybridRetriever(keyword_retriever)
    a = h.answer("年假怎么申请")
    assert a is not None and "年假" in a.text
