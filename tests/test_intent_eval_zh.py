"""B2 意图路由回归基准:zh_golden 中文意图评测集(升级计划 §5.2/§5.4)。

数据来源:datasets/zh_golden/intent_eval_500.jsonl —— CLINC150 按意图
形态映射到本项目五意图体系后 MyMemory 直译中文(含 OOS→other 安全
拒答样本)。数据集与路由器相互独立:本测试度量的是路由器,不是
"让数据迁就实现"。

V2.2 起评测对象为 CascadeIntentRouter(规则快路径 + embedding 语义
层 + other 兜底)。阈值取自全量 505 实测的基线(见 GATES),后续任何
路由改动不得无声回退。embedding 凭据缺失时跳过(hermetic 测试跑
保持纯规则,不做混合基线)。
"""
import json
import os
from collections import Counter
from pathlib import Path

import pytest

from app.application.intent_router import VALID_INTENTS
from app.infrastructure.llm import load_env_file

ZH_GOLDEN = Path(__file__).resolve().parent.parent / "datasets/zh_golden/intent_eval_500.jsonl"

pytestmark = [
    pytest.mark.skipif(not ZH_GOLDEN.exists(), reason="zh_golden dataset not built yet"),
    pytest.mark.slow(reason="505 real embedding API calls (~5-10 min); use scripts/eval_intent_router.py"),
]

# 基线(2026-08-27 V2.2 级联路由器全量 505 实测后定稿;整体 0.75 + 分桶下限)。
# 实测:overall 78.2%, support 94%, faq 85%, progress_query 66%,
# chitchat 77%, other 70%(含锚点样本;排除锚点后 overall 74.4%)。
GATES: dict[str, float] = {
    "overall": 0.75,
    "support": 0.90,
    "faq": 0.80,
    "progress_query": 0.60,
    "chitchat": 0.70,
    # 安全拒答:OOS 样本必须大部分落入 other(低置信兜底),防误答
    "other": 0.65,
}


def _router():
    """Cascade router; skips the eval when embedding credentials are
    unavailable (hermetic test runs stay keyword-only by design)."""
    load_env_file(ZH_GOLDEN.parent.parent / ".env")
    if not os.environ.get("SILICONFLOW_API_KEY"):
        pytest.skip("SILICONFLOW_API_KEY not configured (hermetic run)")
    from app.application.semantic_intent_router import CascadeIntentRouter

    return CascadeIntentRouter()


def _load() -> list[dict]:
    return [json.loads(line) for line in ZH_GOLDEN.read_text(encoding="utf-8").splitlines() if line.strip()]


def _records_or_skip() -> list[dict]:
    records = _load()
    if len(records) < 400:
        pytest.skip(f"zh_golden dataset still building ({len(records)} lines)")
    return records


def test_dataset_sanity() -> None:
    records = _records_or_skip()
    ids = [r["id"] for r in records]
    assert len(set(ids)) == len(ids), "duplicate ids"
    for r in records:
        assert r["expected_intent"] in VALID_INTENTS
        assert isinstance(r["text"], str) and len(r["text"]) >= 4


def test_intent_router_baseline() -> None:
    """整体+分桶准确率不得低于基线。失败信息带混淆分布便于定位。"""
    router = _router()
    records = _records_or_skip()

    per_bucket: dict[str, Counter] = {}
    correct = 0
    confusion: dict[str, Counter] = {}
    for r in records:
        expected = r["expected_intent"]
        got = router.route(r["text"]).intent
        per_bucket.setdefault(expected, Counter())["total"] += 1
        confusion.setdefault(expected, Counter())[got] += 1
        if got == expected:
            correct += 1
            per_bucket[expected]["hit"] += 1

    overall = correct / len(records)
    detail = "\n".join(
        f"  {bucket}: {hits.get('hit', 0)}/{hits['total']}"
        f" -> predicted {dict(confusion[bucket])}"
        for bucket, hits in sorted(per_bucket.items())
    )
    print(f"\nzh_golden intent baseline: overall={overall:.2%} (n={len(records)})\n{detail}")

    assert overall >= GATES["overall"], f"overall {overall:.2%} < gate {GATES['overall']:.0%}\n{detail}"
    for bucket, gate in GATES.items():
        if bucket == "overall" or bucket not in per_bucket:
            continue
        acc = per_bucket[bucket]["hit"] / per_bucket[bucket]["total"]
        assert acc >= gate, f"{bucket} {acc:.2%} < gate {gate:.0%}\n{detail}"


def test_oos_safety_rejects_instead_of_answering() -> None:
    """安全拒答语义:other 桶被路由成 faq/support 才是危险信号
    (会触发检索/建单);路由成 chitchat 也算轻度失准但无害。"""
    router = _router()
    records = [r for r in _records_or_skip() if r["expected_intent"] == "other"]
    assert records, "no OOS samples"
    dangerous = [
        r for r in records
        if router.route(r["text"]).intent in ("faq", "support", "progress_query")
    ]
    # 允许少量误入,但绝不能大面积触发业务面(检索/建单)
    assert len(dangerous) / len(records) <= 0.30, (
        f"{len(dangerous)}/{len(records)} OOS samples routed into business surfaces: "
        f"{[r['text'][:30] for r in dangerous[:8]]}"
    )
