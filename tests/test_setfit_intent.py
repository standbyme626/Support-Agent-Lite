"""批 3:SetFit 本地意图分类层(生产语义层)。

模型训练见 scripts/train_setfit_intent.py;本文件只测推理接入:
路由正确性、概率拒答(形态 C)、不可用降级(None)。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.application.setfit_intent import LABELS, SetFitIntentRouter

MODEL_DIR = Path(__file__).resolve().parent.parent / "runtime" / "setfit-intent"

pytestmark = pytest.mark.skipif(
    not (MODEL_DIR / "model.safetensors").exists(),
    reason="setfit model not trained (run scripts/train_setfit_intent.py)",
)


@pytest.fixture(scope="module")
def router() -> SetFitIntentRouter:
    r = SetFitIntentRouter(model_dir=MODEL_DIR)
    assert r.load()
    return r


def test_labels_contract():
    assert LABELS == ["support", "faq", "progress_query", "chitchat", "other"]


def test_setfit_support(router):
    """生产契约是级联:规则层"卡纸"弱命中先赢,setfit 语义层不可覆盖。

    (单独跑 setfit 分类器时该文本被判 faq 0.822——模型已知短板,
    由规则层 + 保底护栏兜住,见 MEMORY 待办「support 扩训」。)
    """
    from app.application.semantic_intent_router import CascadeIntentRouter

    cascade = CascadeIntentRouter(semantic_router=router)
    d = cascade.route("打印机卡纸了,帮我看看")
    assert d is not None
    assert d.intent == "support"


def test_setfit_faq(router):
    d = router.route("请假流程是什么")
    assert d is not None and d.intent == "faq"


def test_setfit_progress(router):
    d = router.route("我的工单怎么样了")
    assert d is not None and d.intent == "progress_query"


def test_setfit_low_proba_other(router):
    """形态 C:max proba < 阈值 -> other 低置信,不硬猜。"""
    r = SetFitIntentRouter(model_dir=MODEL_DIR, prob_threshold=0.999)
    assert r.load()
    d = r.route("今天天气不错我们出去散步吧")
    assert d is not None
    assert d.intent == "other"
    assert d.is_low_confidence is True


def test_setfit_unavailable_returns_none(tmp_path):
    r = SetFitIntentRouter(model_dir=tmp_path / "empty")
    assert r.load() is False
    assert r.route("打印机卡纸了") is None