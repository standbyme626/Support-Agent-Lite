"""V2.2 semantic layer tests: SemanticIntentRouter + CascadeIntentRouter.

The keyword router stays the deterministic fast path; the embedding
layer catches natural phrasing that misses all keywords (e.g. "我的银行
账户被冻结了" has no support keyword). Anchors are built offline by
scripts/build_intent_anchors.py; tests skip when anchors/credentials
are unavailable (degradation is intentional and covered by the fallback
assertions).
"""
import os
from pathlib import Path

import pytest

from app.application.semantic_intent_router import CascadeIntentRouter, SemanticIntentRouter
from app.infrastructure.llm import load_env_file

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(not SemanticIntentRouter().load(), reason="intent anchors not built")


def _require_embedding() -> None:
    """Semantic tests hit the embedding API: load .env and skip when the
    key is missing (hermetic test runs stay keyword-only by design)."""
    load_env_file(ROOT / ".env")
    if not os.environ.get("SILICONFLOW_API_KEY"):
        pytest.skip("SILICONFLOW_API_KEY not configured")


@pytest.fixture(scope="module")
def semantic() -> SemanticIntentRouter:
    _require_embedding()
    router = SemanticIntentRouter()
    assert router.load()
    return router


@pytest.fixture(scope="module")
def cascade() -> CascadeIntentRouter:
    _require_embedding()
    return CascadeIntentRouter()


def test_semantic_anchors_loaded(semantic: SemanticIntentRouter) -> None:
    assert semantic.available
    assert len(semantic._intents) >= 4


def test_semantic_catches_natural_support_phrasing(semantic: SemanticIntentRouter) -> None:
    decision = semantic.route("为什么我的银行账户被冻结了")
    assert decision is not None
    assert decision.intent == "support"


def test_semantic_catches_order_status(semantic: SemanticIntentRouter) -> None:
    decision = semantic.route("查看我的订单状态")
    assert decision is not None
    assert decision.intent == "progress_query"


def test_semantic_low_score_falls_to_other(semantic: SemanticIntentRouter) -> None:
    decision = semantic.route("今天天气不错我们出去散步吧")
    assert decision is not None
    assert decision.intent == "other"
    assert decision.is_low_confidence


def test_semantic_empty_message(semantic: SemanticIntentRouter) -> None:
    decision = semantic.route("   ")
    assert decision is not None
    assert decision.intent == "other"
    assert decision.is_low_confidence


def test_cascade_rule_fastpath_wins(cascade: CascadeIntentRouter) -> None:
    decision = cascade.route("A3 空调坏了")
    assert decision.intent == "support"
    assert decision.reason in ("rule-fastpath", "rule-low-confidence", "keyword-match")


def test_cascade_embedding_catches_what_rules_miss(cascade: CascadeIntentRouter) -> None:
    decision = cascade.route("我的银行账户被冻结了")
    assert decision.intent == "support"


def test_cascade_chitchat_still_works(cascade: CascadeIntentRouter) -> None:
    decision = cascade.route("你好")
    assert decision.intent == "chitchat"