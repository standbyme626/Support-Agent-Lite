"""Localize the customer-support-tickets dataset into zh KB entries.

Pipeline: CSV(en, IT queues) -> dedupe -> Bailian flash rewrite ->
strict schema validation -> seed/faq/kb_dataset_zh.json (canonical array).

Resume-safe: processed subjects recorded in datasets/.localize_done.txt;
crash mid-run never corrupts the canonical file (atomic rewrite at end).

Usage:
    .venv/bin/python scripts/localize_dataset.py --limit 8      # pilot
    .venv/bin/python scripts/localize_dataset.py               # full run
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.infrastructure.llm import load_env_file  # noqa: E402

QUEUES = {"Technical Support", "IT Support", "Service Outages"}
# Queue labels are noisy ("Strategies for Brand Expansion" slipped in as
# "IT Support"); require explicit IT vocabulary before spending tokens.
import re as _re

_IT_RE = _re.compile(
    r"computer|laptop|\bpc\b|server|network|wifi|wi-fi|printer|monitor|email|e-mail"
    r"|account|password|log ?in|sign ?in|software|windows|mac ?os|linux|vpn|firewall"
    r"|virus|malware|ransom|database|backup|update|crash|blue screen|bsod|error"
    r"|screen|keyboard|mouse|\busb\b|\bssd\b|\bhdd\b|router|switch|\bdns\b|\bip\b"
    r"|outlook|excel|word\b|browser|install|boot|reboot|drive|disk|memory|battery"
    r"|encryption|jira|hubspot|crm|\bit\b|system",
    _re.IGNORECASE,
)
CATEGORIES = ["网络故障", "硬件故障", "软件与安全", "账号与权限", "办公设备", "服务器运维", "流程与制度"]
CSV_PATH = ROOT / "datasets/customer-support-tickets/dataset-tickets-multi-lang-4-20k.csv"
OUT_PATH = ROOT / "seed/faq/kb_dataset_zh.json"
DONE_PATH = ROOT / "datasets/.localize_done.txt"

PROMPT_SYSTEM = (
    "你是企业IT服务台知识库编辑。把给定的英文工单(用户问题+客服答复)改写成一条"
    "中文知识库条目。要求：\n"
    '1) 只输出 JSON，不要解释或代码块标记；\n'
    '2) schema: {"title": string, "content": string, "tags": string[], "category": string}\n'
    "3) title 为简短的中文问题式标题；content 用「现象：…\\n原因：…\\n解决：…」三段结构，"
    "基于答复内容改写而非逐句直译；tags 为 3~6 个中文关键词；category 必须从以下选择：\n"
    f"   {json.dumps(CATEGORIES, ensure_ascii=False)}\n"
    "4) 脱敏：删除真实姓名/邮箱/电话/主机名/内网地址，用泛称替代。\n"
)


def extract_json(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
        return obj if isinstance(obj, dict) else None
    except ValueError:
        return None


def valid_entry(obj: dict | None) -> bool:
    if not obj or not isinstance(obj.get("title"), str) or not isinstance(obj.get("content"), str):
        return False
    if len(obj["title"]) < 6 or len(obj["content"]) < 60:
        return False
    if not isinstance(obj.get("tags"), list) or not obj["tags"]:
        return False
    return obj.get("category") in CATEGORIES


def select_rows() -> list[dict]:
    seen: set[str] = set()
    picked: list[dict] = []
    with open(CSV_PATH, encoding="utf-8", errors="replace") as fh:
        for row in csv.DictReader(fh):
            if row.get("language") != "en" or row.get("queue") not in QUEUES:
                continue
            body, answer, subject = row.get("body", ""), row.get("answer", ""), row.get("subject", "")
            if not (60 <= len(body) <= 1500 and 40 <= len(answer) <= 900):
                continue
            if not _IT_RE.search(f"{subject} {body}"):
                continue  # not IT-related despite the queue label
            key = subject.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            picked.append(row)
    return picked


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--model", default=os.environ.get("BAILIAN_BATCH_MODEL", "qwen3.7-flash-2026-07-15"))
    ap.add_argument("--sleep", type=float, default=1.2)
    args = ap.parse_args()

    load_env_file()
    api_key = os.environ["BAILIAN_API_KEY"]
    base_url = os.environ.get("BAILIAN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")

    done: set[str] = set()
    if DONE_PATH.exists():
        done = set(DONE_PATH.read_text().split())
    existing: list[dict] = []
    if OUT_PATH.exists():
        existing = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    seq = [int(e["doc_id"][6:]) for e in existing if e["doc_id"].startswith("kb-ds-")]
    next_seq = max(seq, default=0) + 1

    candidates = [r for r in select_rows() if r["subject"][:80] not in done]
    if args.limit:
        candidates = candidates[: args.limit]

    # Title-similarity guard: the source queues are synthetic and topic-
    # concentrated ("data analysis platform crashes" x N). Skip candidates
    # whose normalized title overlaps an existing KB entry.
    from app.application.retriever import tokenize

    def _title_terms(text: str) -> set[str]:
        return set(tokenize(text))

    seen_title_terms: list[set[str]] = [
        _title_terms(e["title"]) for e in json.loads(OUT_PATH.read_text(encoding="utf-8"))
    ] if OUT_PATH.exists() else []
    # include the rest of the zh corpus so cross-file dupes are caught too
    for extra in sorted((ROOT / "seed/faq").glob("*.json")):
        if extra.name == OUT_PATH.name:
            continue
        for e in json.loads(extra.read_text(encoding="utf-8")):
            seen_title_terms.append(_title_terms(e["title"]))

    def _too_similar(title: str) -> bool:
        terms = _title_terms(title)
        if not terms:
            return True
        for other in seen_title_terms:
            if not other:
                continue
            if len(terms & other) / len(terms | other) >= 0.42:
                return True
        return False

    filtered: list[dict] = []
    for row in candidates:
        if _too_similar(row["subject"]):
            done.add(row["subject"][:80])
            with open(DONE_PATH, "a") as fh:
                fh.write(row["subject"][:80] + "\n")
            continue
        filtered.append(row)
    print(f"after similarity filter: {len(filtered)} (dropped {len(candidates) - len(filtered)})")
    candidates = filtered
    print(f"candidates this run: {len(candidates)} (already done: {len(done)}, kb total: {len(existing)})")

    import httpx

    client = httpx.Client(timeout=60)
    ok_count = fail_count = 0
    total_tokens = 0

    for row in candidates:
        key = row["subject"][:80]
        user_msg = (
            f"工单主题：{row['subject']}\n\n用户描述：{row['body'][:1200]}\n\n客服答复：{row['answer'][:800]}"
        )
        def _do_call():
            return client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": args.model,
                    "messages": [
                        {"role": "system", "content": PROMPT_SYSTEM},
                        {"role": "user", "content": user_msg},
                    ],
                    "temperature": 0.3,
                },
            )

        # Hard wall-clock watchdog: a trickling response can starve httpx
        # read timeouts forever; SIGALRM guarantees forward progress.
        def _alarm(signum, frame):
            raise TimeoutError("llm call exceeded 75s")

        _old_alarm = signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(75)
        try:
            resp = _do_call()
            resp.raise_for_status()
            data = resp.json()
            total_tokens += int((data.get("usage") or {}).get("total_tokens", 0))
            raw = str(data["choices"][0]["message"]["content"])
        except Exception as exc:  # noqa: BLE001 - keep the batch alive
            signal.alarm(0)
            signal.signal(signal.SIGALRM, _old_alarm)
            print(f"  CALL-FAIL {key[:40]!r}: {exc!r}", flush=True)
            fail_count += 1
            time.sleep(args.sleep * 2)
            continue
        signal.alarm(0)
        signal.signal(signal.SIGALRM, _old_alarm)

        obj = extract_json(raw)
        if not valid_entry(obj):
            print(f"  INVALID   {key[:40]!r}: {str(raw)[:90]}", flush=True)
            fail_count += 1
            done.add(key)
            with open(DONE_PATH, "a") as fh:
                fh.write(key + "\n")
            time.sleep(args.sleep)
            continue

        entry = {
            "doc_id": f"kb-ds-{next_seq:04d}",
            "source_type": "dataset_zh",
            "category": obj["category"],
            "title": obj["title"].strip(),
            "content": obj["content"].strip(),
            "tags": [str(t) for t in obj["tags"]][:6],
            "source_ticket": row["subject"][:100],
        }
        next_seq += 1
        existing.append(entry)
        seen_title_terms.append(_title_terms(entry["title"]))
        done.add(key)
        with open(DONE_PATH, "a") as fh:
            fh.write(key + "\n")
        # atomic-ish canonical write every entry: crash-safe
        tmp = OUT_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(OUT_PATH)
        ok_count += 1
        print(f"  OK {entry['doc_id']} [{entry['category']}] {entry['title'][:40]}", flush=True)
        time.sleep(args.sleep)

    print(f"\ndone: ok={ok_count} fail={fail_count} tokens≈{total_tokens} kb_total={len(existing)}")


if __name__ == "__main__":
    main()
