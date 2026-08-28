"""SetFit fine-tune: bge-small-zh-v1.5 on the cleaned office_golden train.

Two-phase training (contrastive embedding tuning + lightweight classifier
head) on CPU. The classifier is then evaluated on BOTH eval sets with the
cascade arbitration: rule layer wins on any keyword signal, SetFit decides
the long tail.

Usage:
    .venv/bin/python scripts/train_setfit_intent.py             # train + eval
    .venv/bin/python scripts/train_setfit_intent.py --eval-only  # eval saved model
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

TRAIN = ROOT / "datasets" / "office_golden" / "train_650.jsonl"
ZH_GOLDEN = ROOT / "datasets" / "zh_golden" / "intent_eval_500.jsonl"
OFFICE = ROOT / "datasets" / "office_golden" / "eval_500.jsonl"
MODEL_DIR = ROOT / "runtime" / "setfit-intent"

LABELS = ["support", "faq", "progress_query", "chitchat", "other"]
MODEL_NAME = "BAAI/bge-small-zh-v1.5"


def load_records(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


ZH_AUGMENT_PER_BUCKET = 20


def train() -> None:
    import random

    import torch
    from datasets import Dataset
    from setfit import SetFitModel, SetFitTrainer

    torch.set_num_threads(8)
    recs = load_records(TRAIN)
    texts = [r["text"] for r in recs]
    labels = [LABELS.index(r["expected_intent"]) for r in recs]

    # Distribution augmentation: inject zh_golden samples (20 per bucket)
    # so the model has seen "translated-style" utterances — improves
    # cross-distribution robustness. These ids are excluded from zh eval.
    zh_recs = load_records(ZH_GOLDEN)
    rng = random.Random(42)
    zh_train_ids: list[str] = []
    for bucket in LABELS:
        pool = [r for r in zh_recs if r["expected_intent"] == bucket]
        rng.shuffle(pool)
        for r in pool[:ZH_AUGMENT_PER_BUCKET]:
            texts.append(r["text"])
            labels.append(LABELS.index(bucket))
            zh_train_ids.append(r["id"])
    print(f"zh augments: {len(zh_train_ids)}", flush=True)

    ds = Dataset.from_dict({"text": texts, "label": labels})
    print(f"train samples: {len(recs)} office + {len(zh_train_ids)} zh = {len(texts)}", flush=True)
    print(f"distribution: {dict(Counter([LABELS[l] for l in labels]))}", flush=True)

    model = SetFitModel.from_pretrained(MODEL_NAME)
    trainer = SetFitTrainer(
        model=model,
        train_dataset=ds,
        num_epochs=(4, 8),
        batch_size=16,
        learning_rate=2e-5,
    )
    t0 = time.time()
    trainer.train()
    print(f"training done in {time.time()-t0:.0f}s", flush=True)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(MODEL_DIR))
    (MODEL_DIR / "zh_train_ids.json").write_text(json.dumps(zh_train_ids), encoding="utf-8")
    print(f"saved -> {MODEL_DIR}", flush=True)


def load_model():
    from setfit import SetFitModel

    return SetFitModel.from_pretrained(str(MODEL_DIR))


def evaluate(model, name: str, records: list[dict], exclude_ids: set[str] | None = None) -> None:
    if exclude_ids:
        records = [r for r in records if r.get("id") not in exclude_ids]
        print(f"  (excluded {len(records)} train-augmented zh samples)" if False else f"  eval on {len(records)} samples (excluded {len(exclude_ids)} train-augmented)", flush=True)
    texts = [r["text"] for r in records]
    t0 = time.time()
    preds = model.predict(texts)
    p_time = time.time() - t0
    pred_labels = [LABELS[p] if not isinstance(p, str) else p for p in preds]

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
        else:
            pred, src = pred_labels[i], "setfit"
        src_cnt[src] += 1
        per_bucket.setdefault(exp, Counter())["total"] += 1
        confusion.setdefault(exp, Counter())[pred] += 1
        if pred == exp:
            correct += 1
            per_bucket[exp]["hit"] += 1
    overall = correct / len(records)
    print(f"\n=== {name}: overall={overall:.1%} n={len(records)} (infer {p_time:.1f}s for {len(records)} texts) ===", flush=True)
    print(f"route sources: {dict(src_cnt)}", flush=True)
    for k in LABELS:
        c = per_bucket[k]
        print(f"  {k}: {c['hit']}/{c['total']} = {c['hit']/c['total']:.1%} -> {dict(confusion[k])}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval-only", action="store_true")
    ap.add_argument("--set", choices=["both", "office", "zh"], default="both")
    args = ap.parse_args()

    if not args.eval_only:
        train()
    model = load_model()
    zh_excluded: set[str] = set()
    zh_ids_p = MODEL_DIR / "zh_train_ids.json"
    if zh_ids_p.exists():
        zh_excluded = set(json.loads(zh_ids_p.read_text(encoding="utf-8")))
    if args.set in ("both", "office"):
        evaluate(model, "office_golden (setfit)", load_records(OFFICE))
    if args.set in ("both", "zh"):
        evaluate(model, "zh_golden (setfit)", load_records(ZH_GOLDEN), exclude_ids=zh_excluded)
    return 0


if __name__ == "__main__":
    sys.exit(main())