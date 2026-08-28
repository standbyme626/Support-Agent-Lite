"""Offline eval: local bge-small-zh-v1.5 (CPU) vs API Qwen3-Embedding-8B
for the V2.2 cascade intent router.

Reuses the exact cascade arbitration (rule layer wins on any keyword
signal; embedding anchors decide the long tail) but swaps the embedding
backend to a local sentence-transformers model. Reports per-bucket
accuracy, overall, and wall-clock embedding time for both eval sets.

Usage:
    .venv/bin/python scripts/eval_local_bge.py          # both sets
    .venv/bin/python scripts/eval_local_bge.py --set office
    .venv/bin/python scripts/eval_local_bge.py --set zh
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

from app.application.intent_router import IntentRouter  # noqa: E402

ANCHORS_DIR = ROOT / "runtime" / "intent_anchors"
ZH_GOLDEN = ROOT / "datasets" / "zh_golden" / "intent_eval_500.jsonl"
OFFICE = ROOT / "datasets" / "office_golden" / "eval_500.jsonl"

INTENTS = ["support", "faq", "progress_query", "chitchat", "other"]
RULE_CONF = 0.7
EMB_THRESHOLD = 0.62


def load_records(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def evaluate(records: list[dict], query_vecs: list[list[float]]) -> dict:
    import numpy as np

    payload = json.loads((ANCHORS_DIR / "anchors.json").read_text(encoding="utf-8"))
    anchors = payload["anchors"]
    order = list(anchors.keys())

    # local anchor embeddings: reuse texts, encode with local model
    anchor_map: dict[str, np.ndarray] = {}
    all_texts = []
    for intent in order:
        all_texts += anchors[intent]
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer("BAAI/bge-small-zh-v1.5")
    t0 = time.time()
    vecs = model.encode(all_texts, normalize_embeddings=True, show_progress_bar=False)
    print(f"anchor embed: {len(all_texts)} texts in {time.time()-t0:.1f}s", flush=True)
    idx = 0
    for intent in order:
        n = len(anchors[intent])
        anchor_map[intent] = np.asarray(vecs[idx : idx + n], dtype=np.float32)
        idx += n

    t0 = time.time()
    queries = model.encode([r["text"] for r in records], normalize_embeddings=True, show_progress_bar=False)
    print(f"query embed: {len(records)} texts in {time.time()-t0:.1f}s", flush=True)
    queries = np.asarray(queries, dtype=np.float32)

    all_cos = np.zeros((len(queries), len(INTENTS)), dtype=np.float32)
    for j, intent in enumerate(INTENTS):
        block = anchor_map.get(intent)
        if len(block):
            all_cos[:, j] = (queries @ block.T).max(axis=1)
    best_j = all_cos.argmax(axis=1)
    best_s = all_cos.max(axis=1)

    rule_router = IntentRouter()
    correct = 0
    per_bucket: dict[str, Counter] = {}
    confusion: dict[str, Counter] = {}
    src_cnt: Counter = Counter()
    for i, r in enumerate(records):
        exp = r["expected_intent"]
        dec = rule_router.route(r["text"])
        if dec.intent != "other" and not dec.is_low_confidence:
            pred, src = dec.intent, "rule"
        elif best_s[i] >= EMB_THRESHOLD:
            pred, src = INTENTS[best_j[i]], "embedding"
        else:
            pred, src = "other", "fallback"
        src_cnt[src] += 1
        per_bucket.setdefault(exp, Counter())["total"] += 1
        confusion.setdefault(exp, Counter())[pred] += 1
        if pred == exp:
            correct += 1
            per_bucket[exp]["hit"] += 1

    return {
        "overall": round(correct / len(records), 4),
        "n": len(records),
        "per_bucket": {k: {"hit": c["hit"], "total": c["total"], "acc": round(c["hit"] / c["total"], 4)} for k, c in per_bucket.items()},
        "confusion": {k: dict(c) for k, c in confusion.items()},
        "route_sources": dict(src_cnt),
    }


def report(name: str, result: dict) -> None:
    print(f"\n=== {name}: overall={result['overall']:.1%} n={result['n']} ===")
    print(f"route sources: {result['route_sources']}")
    for k in INTENTS:
        p = result["per_bucket"].get(k)
        if p:
            print(f"  {k}: {p['hit']}/{p['total']} = {p['acc']:.1%}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", choices=["both", "office", "zh"], default="both")
    args = ap.parse_args()

    if args.set in ("both", "office"):
        recs = load_records(OFFICE)
        report("office_golden (local bge)", evaluate(recs, []))
    if args.set in ("both", "zh"):
        recs = load_records(ZH_GOLDEN)
        report("zh_golden (local bge)", evaluate(recs, []))
    return 0


if __name__ == "__main__":
    sys.exit(main())