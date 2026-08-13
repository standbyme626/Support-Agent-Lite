"""Phase 4 tests: IntentRouter (rules-first, deterministic)."""
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


def test_greeting_routes_to_other() -> None:
    decision = route("你好")
    assert decision.intent == "other"
    assert decision.is_low_confidence is True


def test_empty_message_is_low_confidence_other() -> None:
    decision = route("   ")
    assert decision.intent == "other"
    assert decision.is_low_confidence is True


def test_deterministic_same_message_same_intent() -> None:
    router = IntentRouter()
    assert router.route("A3 空调坏了") == router.route("A3 空调坏了")


def test_llm_fallback_hook_used_when_below_threshold() -> None:
    calls = []

    def fake_llm(text: str) -> tuple[str, float]:
        calls.append(text)
        return "support", 0.9

    router = IntentRouter(llm_classify_fn=fake_llm)
    decision = router.route("我感觉不太对劲")
    assert decision.intent == "support"
    assert decision.reason == "llm-classify"
    assert calls == ["我感觉不太对劲"]


def test_llm_fallback_ignored_when_rule_matches() -> None:
    def fake_llm(text: str) -> tuple[str, float]:  # noqa: ARG001
        return "faq", 0.9

    router = IntentRouter(llm_classify_fn=fake_llm)
    assert router.route("A3 空调坏了").intent == "support"
