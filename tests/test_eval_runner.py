"""C3: standardized eval runner + trace JSONL export.

One-command regression over the quality assets: RAG recall (keyword),
hybrid rerank precision (vector, when index present), KB gate summary,
and full-chain trace export. Numbers are produced by code, not prose.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.application.evals import (
    HYBRID_CASES,
    RAG_CASES,
    export_traces_jsonl,
    run_hybrid_eval,
    run_rag_eval,
    write_report,
)
from app.application.retriever import Retriever
from app.infrastructure.db import apply_migrations, connect
from app.infrastructure.trace import TraceLogger

SEED_FAQ = Path(__file__).resolve().parent.parent / "seed" / "faq"


@pytest.fixture(scope="module")
def retriever() -> Retriever:
    return Retriever(SEED_FAQ)


@pytest.fixture()
def trace_conn():
    conn = connect(":memory:")
    apply_migrations(conn)
    yield conn
    conn.close()


# --- RAG eval -----------------------------------------------------------------


def test_rag_eval_structure_and_threshold(retriever):
    report = run_rag_eval(retriever)
    assert report["cases"] == len(RAG_CASES)
    assert report["recall_at_3"] >= 0.9
    assert 0.0 <= report["mrr"] <= 1.0
    assert all("hit" in c and "rank" in c for c in report["details"])


def test_rag_cases_cover_new_corpus(retriever):
    # dataset_zh-era entries must appear as expectations, not only legacy faq-*
    expected = {doc_id for _, doc_id in RAG_CASES}
    assert any(doc_id.startswith(("kb-", "sop-it")) for doc_id in expected)


# --- hybrid eval ----------------------------------------------------------------


def test_hybrid_eval_skips_without_index(retriever):
    assert run_hybrid_eval(retriever, index_dir="/nonexistent-path") is None


def test_hybrid_eval_runs_with_fake_backend(retriever):
    class FakeEmbedding:
        def embed(self, texts):  # noqa: ANN001
            return [[0.0] * 8 for _ in texts]

    class FakeStore:
        def load(self):  # noqa: ANN001
            return True

        def search(self, vec, top_k=8):  # noqa: ANN001
            return []

    out = run_hybrid_eval(
        retriever,
        index_dir="unused",
        embedding=FakeEmbedding(),
        store=FakeStore(),
        reranker=None,
    )
    assert out is not None and out["cases"] == len(HYBRID_CASES)


# --- trace export -----------------------------------------------------------------


def test_export_traces_jsonl_roundtrip(trace_conn, tmp_path):
    log = TraceLogger(trace_conn)
    log.event("tr_1", "identity", {"user": "u1"})
    log.event("tr_1", "agent", {"model": "fake"})
    log.event("tr_2", "intent", {"intent": "faq"})
    out = tmp_path / "traces.jsonl"
    n = export_traces_jsonl(trace_conn, out)
    assert n == 3
    lines = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
    assert {l["trace_id"] for l in lines} == {"tr_1", "tr_2"}
    assert lines[0]["stage"] == "identity"
    assert isinstance(lines[0]["payload"], dict)


def test_export_empty_is_zero(trace_conn, tmp_path):
    assert export_traces_jsonl(trace_conn, tmp_path / "empty.jsonl") == 0


# --- report writer --------------------------------------------------------------------


def test_write_report_produces_md_and_json(tmp_path):
    payload = {
        "generated_at": "2026-08-25T00:00:00Z",
        "rag": {"recall_at_3": 1.0, "mrr": 1.0, "cases": 2},
        "hybrid": None,
        "agent_pytest": "286 passed",
    }
    md_path = write_report(payload, out_dir=tmp_path)
    assert md_path.exists()
    json_path = md_path.with_suffix(".json")
    assert json.loads(json_path.read_text(encoding="utf-8")) == payload
    assert "Recall@3" in md_path.read_text(encoding="utf-8")
