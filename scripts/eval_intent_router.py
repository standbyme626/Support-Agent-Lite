"""Offline eval for the V2.2 cascade intent router on zh_golden.

Batch-embeds all 505 queries once (fast path: embeddings cached under
/tmp/opencode or runtime/eval_cache), then evaluates the cascade logic
locally with numpy — seconds instead of the ~10 min a per-query API
loop takes.

Usage:
    .venv/bin/python scripts/eval_intent_router.py            # batch embed + evaluate
    .venv/bin/python scripts/eval_intent_router.py --cached   # reuse cached query vectors
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.infrastructure.llm import load_env_file  # noqa: E402

load_env_file(ROOT / ".env")

from app.application.intent_router import IntentRouter  # noqa: E402
from app.infrastructure.vector_store import SiliconFlowEmbedding  # noqa: E402

ZH_GOLDEN = ROOT / "datasets" / "zh_golden" / "intent_eval_500.jsonl"
ANCHORS_DIR = ROOT / "runtime" / "intent_anchors"
CACHE = ROOT / "runtime" / "eval_cache" / "zh_golden_queries.json"

INTENTS = ["support", "faq", "progress_query", "chitchat", "other"]
RULE_CONF = 0.7
EMB_THRESHOLD = 0.62


def load_records() -> list[dict]:
    return [
        json.loads(line)
        for line in ZH_GOLDEN.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def load_anchor_vectors() -> tuple[dict[str, list[str]], list[list[float]]]:
    payload = json.loads((ANCHORS_DIR / "anchors.json").read_text(encoding="utf-8"))
    vectors = json.loads((ANCHORS_DIR / "vectors.json").read_text(encoding="utf-8"))
    return payload["anchors"], vectors


def query_vectors(records: list[dict], cached: bool) -> list[list[float]]:
    if cached and CACHE.exists():
        data = json.loads(CACHE.read_text(encoding="utf-8"))
        if len(data) == len(records):
            print(f"using cached query vectors: {CACHE}")
            return data
    texts = [r["text"] for r in records]
    emb = SiliconFlowEmbedding(batch_size=8, timeout=30.0, retries=2)
    vectors: list[list[float]] = []
    t0 = time.time()
    for i in range(0, len(texts), 8):
        chunk = texts[i : i + 8]
        vectors += emb.embed(chunk)
        if i % 80 == 0:
            print(f"  embedded {min(i + 8, len(texts))}/{len(texts)} ({time.time() - t0:.0f}s)")
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(vectors), encoding="utf-8")
    print(f"cached {len(vectors)} query vectors -> {CACHE}")
    return vectors


def evaluate(records: list[dict], query_vecs: list[list[float]]) -> None:
    import numpy as np

    anchors, anchor_vecs = load_anchor_vectors()
    order = list(anchors.keys())
    anchor_map: dict[str, np.ndarray] = {}
    idx = 0
    for intent in order:
        n = len(anchors[intent])
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
    stats: dict[str, Counter] = {}
    correct = 0
    src_cnt: Counter = Counter()
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
        stats.setdefault(exp, Counter())["total"] += 1
        if pred == exp:
            correct += 1
            stats[exp]["hit"] += 1

    overall = correct / len(records)
    print(f"\n=== cascade router eval: overall={overall:.1%} (n={len(records)}) ===")
    print(f"route sources: {dict(src_cnt)}")
    for k in INTENTS:
        c = stats[k]
        print(f"  {k}: {c['hit']}/{c['total']} = {c['hit'] / c['total']:.1%}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cached", action="store_true", help="reuse cached query vectors")
    args = parser.parse_args()

    records = load_records()
    if len(records) < 400:
        print(f"zh_golden dataset still building ({len(records)} lines)")
        return 1
    vectors = query_vectors(records, args.cached)
    evaluate(records, vectors)
    return 0


if __name__ == "__main__":
    sys.exit(main())