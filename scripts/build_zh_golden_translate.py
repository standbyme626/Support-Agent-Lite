"""Build zh golden eval set using MyMemory free translation API.

Usage:
    .venv/bin/python scripts/build_zh_golden_translate.py --limit 10   # test 10
    .venv/bin/python scripts/build_zh_golden_translate.py              # full 500
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "datasets/zh_golden"
CLINC_DIR = ROOT / "datasets/clinc150"

TARGETS = {"support": 140, "faq": 110, "progress_query": 100, "chitchat": 60, "other": 90}

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
        "greeting", "goodbye", "thank_you", "are_you_a_bot", "what_is_your_name",
        "who_made_you", "what_can_i_ask_you", "how_old_are_you",
        "where_are_you_from", "do_you_have_pets",
    ],
}


import httpx

client = httpx.Client(timeout=30)


def translate_en_to_zh(text: str) -> str | None:
    """Call MyMemory API to translate English to Chinese."""
    for attempt in range(3):
        try:
            resp = client.get(
                "https://api.mymemory.translated.net/get",
                params={"q": text, "langpair": "en|zh-CN"},
            )
            resp.raise_for_status()
            data = resp.json()
            translated = data.get("responseData", {}).get("translatedText", "")
            if translated and translated.lower() != text.lower():
                return translated.strip()
            return None
        except Exception as e:
            if attempt < 2:
                time.sleep(1)
            else:
                print(f"  TRANSLATE-FAIL: {e}", flush=True)
                return None
    return None


def _sample_pool() -> list[dict]:
    """Deterministic sample across curated source intents + OOS(NaN test)."""
    import random

    rng = random.Random(42)
    intents = pd.read_parquet(CLINC_DIR / "intents-00000-of-00001.parquet")
    names = intents["name"].tolist()
    train = pd.read_parquet(CLINC_DIR / "train-00000-of-00001.parquet")
    train = train.dropna(subset=["label"])
    train["label"] = train["label"].astype(int)
    train["intent"] = train["label"].map(lambda i: names[i] if 0 <= i < len(names) else "")

    pool: list[dict] = []
    for bucket, sources in BUCKET_SOURCES.items():
        per = TARGETS[bucket] // len(sources)
        remainder = TARGETS[bucket] - per * len(sources)
        for si, intent in enumerate(sources):
            rows = train[train["intent"] == intent]
            n = min(per + (1 if si < remainder else 0), len(rows))
            take = n + 2
            sampled = rows.sample(n=min(take, len(rows)), random_state=rng.randint(0, 2**31))
            for _, row in sampled.iterrows():
                text = str(row["utterance"]).strip()
                if not (3 <= len(text) <= 120):
                    continue
                pool.append({"bucket": bucket, "clinc_intent": intent, "text": text})
                if sum(1 for p in pool if p["clinc_intent"] == intent) >= per + (1 if si < remainder else 0):
                    break

    test = pd.read_parquet(CLINC_DIR / "test-00000-of-00001.parquet")
    oos = test[test["label"].isna()]
    oos = oos[oos["utterance"].str.len().between(3, 120)]
    sampled_oos = oos.sample(n=TARGETS["other"], random_state=rng.randint(0, 2**31))
    for _, row in sampled_oos.iterrows():
        pool.append({"bucket": "other", "clinc_intent": "oos", "text": str(row["utterance"]).strip()})
    return pool


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=0.3)
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / "intent_eval_500.jsonl"

    pool = _sample_pool()
    if args.limit:
        counts: dict[str, int] = {}
        limited: list[dict] = []
        for p in pool:
            b = p["bucket"]
            if counts.get(b, 0) >= args.limit:
                continue
            counts[b] = counts.get(b, 0) + 1
            limited.append(p)
        pool = limited

    print(f"pool this run: {len(pool)}", flush=True)

    seq = 0
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            if line.strip():
                seq += 1

    seen_texts: set[str] = set()
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                seen_texts.add(rec["text"])
            except Exception:
                continue

    ok = fail = 0
    for item in pool:
        seq += 1
        rid = f"zh-intent-{seq:04d}"

        text_zh = translate_en_to_zh(item["text"])
        if not text_zh or not (4 <= len(text_zh) <= 60):
            print(f"  INVALID   {rid}: {text_zh!r}", flush=True)
            fail += 1
            time.sleep(args.sleep)
            continue

        if text_zh in seen_texts:
            print(f"  DEDUP     {rid}: {text_zh[:30]!r}", flush=True)
            fail += 1
            time.sleep(args.sleep)
            continue

        record = {
            "id": rid,
            "text": text_zh,
            "expected_intent": item["bucket"],
            "clinc_intent": item["clinc_intent"],
            "source_split": "test" if item["clinc_intent"] == "oos" else "train",
            "style": "translated",
        }
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        seen_texts.add(text_zh)
        ok += 1
        print(f"  OK {rid} [{item['bucket']}] {text_zh[:40]}", flush=True)
        time.sleep(args.sleep)

    print(f"\ndone: ok={ok} fail={fail} total={seq}", flush=True)


if __name__ == "__main__":
    main()
