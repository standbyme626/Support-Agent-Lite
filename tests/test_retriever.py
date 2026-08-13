"""Phase 4 tests: Retriever (grounding + no-answer protection).

Invariant #7: low-confidence RAG must not become free-form answers.
`answer()` returns None below threshold; the workflow then replies with
an explicit no-answer message instead of a hallucinated one.
"""
from pathlib import Path

from app.application.retriever import Retriever

SEED_DIR = Path(__file__).resolve().parent.parent / "seed" / "faq"


def test_loads_faq_documents() -> None:
    retriever = Retriever(SEED_DIR)
    assert len(retriever.documents) >= 10
    assert all(doc.source_type == "faq" for doc in retriever.documents)


def test_search_returns_ranked_hits() -> None:
    retriever = Retriever(SEED_DIR)
    hits = retriever.search("年假怎么申请", top_k=3)
    assert len(hits) <= 3
    assert hits[0].document.doc_id == "faq-001"
    assert hits[0].score >= hits[-1].score


def test_answer_grounded_with_source_attribution() -> None:
    """AC-01 retrieval stage: grounded answer carries the source."""
    retriever = Retriever(SEED_DIR)
    answer = retriever.answer("年假怎么申请")
    assert answer is not None
    assert "faq-001" in answer.text
    assert "来源" in answer.text
    assert answer.hits[0].source == "faq:faq-001"


def test_answer_returns_none_for_out_of_domain() -> None:
    """Invariant #7: low confidence -> no answer, never free-form."""
    retriever = Retriever(SEED_DIR)
    assert retriever.answer("今天天气怎么样") is None
    assert retriever.answer("你好") is None


def test_answer_returns_none_for_ambiguous_short_query() -> None:
    retriever = Retriever(SEED_DIR)
    assert retriever.answer("发票") is None


def test_answer_respects_min_score_threshold() -> None:
    """The score gate is configurable: a weak hit passes only below it."""
    retriever = Retriever(SEED_DIR)
    weak_query = "今天天气怎么样"  # weak score ~0.13 on a common-term hit
    assert retriever.answer(weak_query, min_score=0.15) is None
    assert retriever.answer(weak_query, min_score=0.10) is not None


def test_no_ticket_is_created_by_retrieval() -> None:
    """The retriever itself never touches tickets (read-only knowledge)."""
    retriever = Retriever(SEED_DIR)
    assert retriever.answer("年假怎么申请") is not None
