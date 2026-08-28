"""Offline eval for the V2.2 cascade intent router on office_golden eval_500.

Reuses the cached query vectors (runtime/eval_cache/office_eval_queries.json)
built during dataset assembly; only the cascade logic runs locally with
numpy. Writes a persisted report (JSON + MD) with per-bucket accuracy and
a confusion matrix — the P3 acceptance artifact.

Usage:
    .venv/bin/python scripts/eval_office_router.py
    .venv/bin/python scripts/eval_office_router.py --re-embed
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.infrastructure.llm import load_env_file  # noqa: E402

load_env_file(ROOT / ".env")

from app.application.intent_router import IntentRouter  # noqa: E402

EVAL = ROOT / "datasets" / "office_golden" / "eval_500.jsonl"
ANCHORS_DIR = ROOT / "runtime" / "intent_anchors"
CACHE = ROOT / "runtime" / "eval_cache" / "office_eval_queries.json"
REPORT_JSON = ROOT / "runtime" / "office_eval_report.json"
REPORT_MD = ROOT / "runtime" / "office_eval_report.md"

INTENTS = ["support", "faq", "progress_query", "chitchat", "other"]
RULE_CONF = 0.7
EMB_THRESHOLD = 0.62


def load_records() -> list[dict]:
    return [
        json.loads(line)
        for line in EVAL.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def evaluate(records: list[dict], query_vecs: list[list[float]]) -> dict:
    import numpy as np

    anchors = json.loads((ANCHORS_DIR / "anchors.json").read_text(encoding="utf-8"))
    anchor_vecs = json.loads((ANCHORS_DIR / "vectors.json").read_text(encoding="utf-8"))
    order = list(anchors["anchors"].keys())
    anchor_map: dict[str, np.ndarray] = {}
    idx = 0
    for intent in order:
        n = len(anchors["anchors"][intent])
        block = np.asarray(anchor_vecs[idx : idx + n], dtype=np.float32)
        norms = np.linalg.norm(block, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        anchor_map[intent] = block / norms
        idx += n

    queries = np.asarray(query_vecs, dtype=np.float32)
    q_norms = np.linalg.norm(queries, axis=1, keepdims=True)
    q_norms[q_norms == 0] = 1.0
    queries = queries / q_norms

    all_cos = np.zeros((len(queries), len(INTENTS)), dtype=np.float32)
    for j, intent in enumerate(INTENTS):
        block = anchor_map.get(intent)
        if len(block):
            all_cos[:, j] = (queries @ block.T).max(axis=1)
    best_j = all_cos.argmax(axis=1)
    best_s = all_cos.max(axis=1)

    rule_router = IntentRouter()
    src_cnt: Counter = Counter()
    correct = 0
    per_bucket: dict[str, Counter] = {}
    confusion: dict[str, Counter] = {}
    for i, r in enumerate(records):
        exp = r["expected_intent"]
        dec = rule_router.route(r["text"])
        if dec.intent != "other" and not dec.is_low_confidence and dec.confidence >= RULE_CONF:
            pred, src = dec.intent, "rule-fastpath"
        elif best_s[i] >= EMB_THRESHOLD:
            pred, src = INTENTS[best_j[i]], "embedding"
        elif dec.intent != "other" and not dec.is_low_confidence:
            pred, src = dec.intent, "rule-low-conf"
        else:
            pred, src = "other", "fallback"
        src_cnt[src] += 1
        per_bucket.setdefault(exp, Counter())["total"] += 1
        confusion.setdefault(exp, Counter())[pred] += 1
        if pred == exp:
            correct += 1
            per_bucket[exp]["hit"] += 1

    per = {k: {"hit": c["hit"], "total": c["total"], "acc": round(c["hit"] / c["total"], 4)} for k, c in per_bucket.items()}
    overall = round(correct / len(records), 4)
    return {
        "overall": overall,
        "n": len(records),
        "per_bucket": per,
        "confusion": {k: dict(c) for k, c in confusion.items()},
        "route_sources": dict(src_cnt),
    }


def write_report(result: dict) -> None:
    REPORT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = [
        "# office_golden eval_500 — V2.2 cascade router",
        "",
        f"- 时间: {__import__('datetime').datetime.now().isoformat()}",
        f"- 总体准确率: **{result['overall']:.1%}** (n={result['n']})",
        f"- 路由来源: {result['route_sources']}",
        "",
        "| 意图 | 命中/总数 | 准确率 |",
        "|---|---|---|",
    ]
    for k in INTENTS:
        p = result["per_bucket"].get(k, {"hit": 0, "total": 0, "acc": 0})
        lines.append(f"| {k} | {p['hit']}/{p['total']} | {p['acc']:.1%} |")
    lines += ["", "### 混淆矩阵 (期望 → 预测)", ""]
    header = "| 期望\\预测 | " + " | ".join(INTENTS) + " |"
    lines.append(header)
    lines.append("|" + "---|" * (len(INTENTS) + 1))
    for k in INTENTS:
        row = confusion[k] if (confusion := result["confusion"]).get(k) else {}
        lines.append(f"| {k} | " + " | ".join(str(row.get(i, 0)) for i in INTENTS) + " |")
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"report: {REPORT_JSON} / {REPORT_MD}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--re-embed", action="store_true", help="re-embed queries instead of using cache")
    args = ap.parse_args()

    records = load_records()
    if len(records) < 400:
        print(f"office eval set incomplete ({len(records)} lines)")
        return 1

    if args.re_embed or not CACHE.exists():
        from app.infrastructure.vector_store import SiliconFlowEmbedding

        texts = [r["text"] for r in records]
        emb = SiliconFlowEmbedding(batch_size=8, timeout=30.0, retries=2)
        vectors: list[list[float]] = []
        for i in range(0, len(texts), 8):
            vectors += emb.embed(texts[i : i + 8])
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(vectors), encoding="utf-8")
    else:
        vectors = json.loads(CACHE.read_text(encoding="utf-8"))
        print(f"using cached query vectors: {CACHE}")
    if len(vectors) != len(records):
        print(f"cache mismatch: {len(vectors)} vectors vs {len(records)} records; re-run with --re-embed")
        return 1

    result = evaluate(records, vectors)
    print(f"overall={result['overall']:.1%} n={result['n']}")
    for k in INTENTS:
        p = result["per_bucket"].get(k)
        if p:
            print(f"  {k}: {p['hit']}/{p['total']} = {p['acc']:.1%}")
    write_report(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())