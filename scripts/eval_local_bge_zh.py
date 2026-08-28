"""Experiment: local bge-small-zh-v1.5 with PURE CHINESE anchors.

The production anchors mix CLINC150 English + zh_golden Chinese; bge-zh
matches Chinese anchors far better. This script builds anchors from
office_golden train (clean Chinese, labeled), scans the embedding
threshold, and reports both eval sets — the decisive test for whether
local bge can replace the API layer without fine-tuning.

Usage:
    .venv/bin/python scripts/eval_local_bge_zh.py
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.application.intent_router import IntentRouter  # noqa: E402

TRAIN = ROOT / "datasets" / "office_golden" / "train_650.jsonl"
ZH_GOLDEN = ROOT / "datasets" / "zh_golden" / "intent_eval_500.jsonl"
OFFICE = ROOT / "datasets" / "office_golden" / "eval_500.jsonl"

INTENTS = ["support", "faq", "progress_query", "chitchat", "other"]
ANCHORS_PER_INTENT = 40


def load_records(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def build_zh_anchors() -> dict[str, list[str]]:
    anchors: dict[str, list[str]] = {k: [] for k in INTENTS}
    for rec in load_records(TRAIN):
        anchors[rec["expected_intent"]].append(rec["text"])
    return {k: v[:ANCHORS_PER_INTENT] for k, v in anchors.items()}


def run(name: str, records: list[dict], model, anchors: dict[str, list[str]], threshold: float) -> dict:
    import numpy as np

    order = list(anchors.keys())
    anchor_map: dict[str, np.ndarray] = {}
    all_texts = [t for k in order for t in anchors[k]]
    vecs = model.encode(all_texts, normalize_embeddings=True, show_progress_bar=False)
    idx = 0
    for intent in order:
        n = len(anchors[intent])
        anchor_map[intent] = np.asarray(vecs[idx : idx + n], dtype=np.float32)
        idx += n

    t0 = time.time()
    queries = model.encode([r["text"] for r in records], normalize_embeddings=True, show_progress_bar=False)
    q_t = time.time() - t0
    queries = np.asarray(queries, dtype=np.float32)

    all_cos = np.zeros((len(queries), len(INTENTS)), dtype=np.float32)
    for j, intent in enumerate(INTENTS):
        block = anchor_map[intent]
        if len(block):
            all_cos[:, j] = (queries @ block.T).max(axis=1)
    best_j = all_cos.argmax(axis=1)
    best_s = all_cos.max(axis=1)

    rule_router = IntentRouter()
    correct = 0
    per_bucket: dict[str, Counter] = {}
    for i, r in enumerate(records):
        exp = r["expected_intent"]
        dec = rule_router.route(r["text"])
        if dec.intent != "other" and not dec.is_low_confidence:
            pred = dec.intent
        elif best_s[i] >= threshold:
            pred = INTENTS[best_j[i]]
        else:
            pred = "other"
        per_bucket.setdefault(exp, Counter())["total"] += 1
        if pred == exp:
            correct += 1
            per_bucket[exp]["hit"] += 1
    overall = round(correct / len(records), 4)
    per = {k: round(c["hit"] / c["total"], 4) for k, c in per_bucket.items()}
    print(f"{name} thr={threshold}: overall={overall:.1%} per={per} qtime={q_t:.1f}s")
    return {"overall": overall, "per": per}


def main() -> int:
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer("BAAI/bge-small-zh-v1.5")
    anchors = build_zh_anchors()
    print(f"zh anchors: {dict((k, len(v)) for k, v in anchors.items())}", flush=True)

    office = load_records(OFFICE)
    zh = load_records(ZH_GOLDEN)
    for thr in (0.45, 0.50, 0.55, 0.60):
        run("office", office, model, anchors, thr)
        run("zh    ", zh, model, anchors, thr)
    return 0


if __name__ == "__main__":
    sys.exit(main())