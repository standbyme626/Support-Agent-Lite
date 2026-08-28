"""Build intent anchor sets for SemanticIntentRouter (embedding layer).

Anchors come from two sources:
- CLINC150 English utterances (train split, curated source intents)
- zh_golden Chinese labeled samples (each intent bucket)

Embeddings are produced via SiliconFlow Qwen3-Embedding-8B and persisted
under runtime/intent_anchors/ as JSON (texts + vectors), loadable by the
SemanticIntentRouter at runtime.

Usage:
    .venv/bin/python scripts/build_intent_anchors.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.infrastructure.llm import load_env_file  # noqa: E402

load_env_file(ROOT / ".env")

from app.infrastructure.vector_store import SiliconFlowEmbedding  # noqa: E402

OUT_DIR = ROOT / "runtime" / "intent_anchors"
CLINC_DIR = ROOT / "datasets" / "clinc150"
ZH_GOLDEN = ROOT / "datasets" / "zh_golden" / "intent_eval_500.jsonl"
ZH_ANCHORS_PER_INTENT = 15

BUCKET_SOURCES = {
    "support": [
        "account_blocked", "card_declined", "damaged_card", "report_lost_card",
        "report_fraud", "freeze_account", "sync_device", "find_phone",
        "lost_luggage", "jump_start",
    ],
    "faq": [
        "pin_change", "improve_credit_score", "oil_change_how", "oil_change_when",
        "definition", "order_checks", "redeem_rewards", "new_card",
        "expiration_date", "replacement_card_duration", "schedule_maintenance",
    ],
    "progress_query": [
        "order_status", "application_status", "pto_request_status",
        "flight_status", "last_maintenance",
    ],
    "chitchat": [
        "greeting", "goodbye", "thank_you", "are_you_a_bot",
        "what_is_your_name", "who_made_you", "what_can_i_ask_you",
        "how_old_are_you", "where_are_you_from", "do_you_have_pets",
    ],
    "other": [],
}

EN_PER_INTENT = 5


def load_zh_samples() -> dict[str, list[str]]:
    samples: dict[str, list[str]] = {k: [] for k in BUCKET_SOURCES}
    if not ZH_GOLDEN.exists():
        return samples
    for line in ZH_GOLDEN.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec["expected_intent"] in samples:
            samples[rec["expected_intent"]].append(rec["text"])
    return samples


def build_anchors() -> dict[str, list[str]]:
    train = pd.read_parquet(CLINC_DIR / "train-00000-of-00001.parquet")
    intents_df = pd.read_parquet(CLINC_DIR / "intents-00000-of-00001.parquet")
    name2id = {row.name: int(row.id) for row in intents_df.itertuples()}

    anchors: dict[str, list[str]] = {}
    zh_samples = load_zh_samples()
    for intent, src_intents in BUCKET_SOURCES.items():
        texts: list[str] = []
        for si in src_intents:
            lid = name2id.get(si)
            if lid is None:
                continue
            texts += train[train["label"] == lid]["utterance"].head(EN_PER_INTENT).tolist()
        texts += zh_samples.get(intent, [])[:ZH_ANCHORS_PER_INTENT]
        anchors[intent] = texts
    return anchors


def main() -> int:
    anchors = build_anchors()
    all_texts = [t for ts in anchors.values() for t in ts]
    if not all_texts:
        print("no anchors built", file=sys.stderr)
        return 1

    emb = SiliconFlowEmbedding(batch_size=16)
    vectors = emb.embed(all_texts)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "anchors.json").write_text(
        json.dumps({"anchors": anchors, "order": list(anchors.keys())}, ensure_ascii=False),
        encoding="utf-8",
    )
    (OUT_DIR / "vectors.json").write_text(json.dumps(vectors), encoding="utf-8")

    print(f"built anchors: {dict((k, len(v)) for k, v in anchors.items())}")
    print(f"total {len(all_texts)} anchors -> {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())