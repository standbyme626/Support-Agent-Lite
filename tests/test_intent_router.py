"""Phase 4 tests: IntentRouter (deterministic pre-routing only).

V2.1: the LLM classification fallback (`llm_classify_fn`) was removed —
semantic understanding belongs to the SupportAgent; two competing LLM
routing layers would produce contradictory intent signals.
"""
from app.application.intent_router import IntentRouter


def route(text: str):
    return IntentRouter().route(text)


def test_faq_intent_ac01() -> None:
    decision = route("年假怎么申请？")
    assert decision.intent == "faq"
    assert decision.is_low_confidence is False


def test_support_intent_ac02() -> None:
    decision = route("A3 空调坏了")
    assert decision.intent == "support"
    assert decision.is_low_confidence is False


def test_progress_query_ac05() -> None:
    decision = route("昨天空调那个事情怎么样了？")
    assert decision.intent == "progress_query"


def test_progress_query_short() -> None:
    assert route("处理了吗？").intent == "progress_query"


def test_support_keyword_vpn() -> None:
    assert route("VPN 连不上").intent == "support"


def test_faq_beats_support_when_no_issue_keyword() -> None:
    decision = route("怎么申请安装新软件")
    assert decision.intent == "faq"


def test_support_wins_tie_with_faq_phrasing() -> None:
    """'空调坏了怎么办' contains both support (坏了) and faq (怎么办):
    must route to support, never to faq."""
    decision = route("空调坏了怎么办")
    assert decision.intent == "support"


def test_greeting_routes_to_chitchat() -> None:
    decision = route("你好")
    assert decision.intent == "chitchat"
    assert decision.is_low_confidence is False


def test_empty_message_is_low_confidence_other() -> None:
    decision = route("   ")
    assert decision.intent == "other"
    assert decision.is_low_confidence is True


def test_deterministic_same_message_same_intent() -> None:
    router = IntentRouter()
    assert router.route("A3 空调坏了") == router.route("A3 空调坏了")


def test_low_confidence_routes_to_other_deterministically() -> None:
    """Below-threshold text stays 'other' — no LLM fallback (removed in
    V2.1); the workflow's continuation/agent logic owns interpretation."""
    decision = route("我感觉不太对劲")
    assert decision.intent == "other"
    assert decision.is_low_confidence is True
    assert decision.reason.startswith("below-threshold")



# --- E2E 修复用例(2026-08-28) -------------------------------------------------


def test_support_keyword_card_jam() -> None:
    assert route("打印机卡纸了").intent == "support"


def test_progress_keyword_not_fixed_yet() -> None:
    assert route("投影仪还没修好吗").intent == "progress_query"
    assert route("麻烦催一下工单").intent == "progress_query"


def test_guard_signal_detection() -> None:
    from app.application.intent_router import has_support_guard_signal

    assert has_support_guard_signal("这投影仪能修吗")
    assert has_support_guard_signal("门禁卡刷不开了")
    assert not has_support_guard_signal("今天天气不错我们出去散步吧")
    assert not has_support_guard_signal("你好")
