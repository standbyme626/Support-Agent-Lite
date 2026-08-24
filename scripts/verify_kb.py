"""KB quality gate: schema, duplicates, coverage stats, retrieval smoke tests.

Run after build_kb.py (and after any manual KB edits):
    .venv/bin/python scripts/verify_kb.py
Exits non-zero on hard failures (schema/id errors, failed smoke queries).
Near-duplicate title pairs are reported as warnings for manual triage.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "seed" / "faq"

sys.path.insert(0, str(ROOT))
from app.application.retriever import Retriever, tokenize  # noqa: E402


def load_all() -> list[dict]:
    entries: list[dict] = []
    ids: set[str] = set()
    for path in sorted(SEED.glob("*.json")):
        for raw in json.loads(path.read_text(encoding="utf-8")):
            for field in ("doc_id", "title", "content"):
                if not raw.get(field):
                    raise SystemExit(f"FAIL {path.name}: missing {field}: {raw}")
            if raw["doc_id"] in ids:
                raise SystemExit(f"FAIL duplicate doc_id {raw['doc_id']}")
            ids.add(raw["doc_id"])
            if len(str(raw["content"])) < 30:
                raise SystemExit(f"FAIL {raw['doc_id']}: content <30 chars")
            entries.append(raw)
    return entries


def near_duplicates(entries: list[dict], threshold: float = 0.72) -> list[tuple[str, str, float]]:
    pairs = []
    tokenized = [(e["doc_id"], e["title"], set(tokenize(e["title"]))) for e in entries]
    for i in range(len(tokenized)):
        for j in range(i + 1, len(tokenized)):
            a_id, a_title, a_terms = tokenized[i]
            b_id, b_title, b_terms = tokenized[j]
            if not a_terms or not b_terms:
                continue
            jaccard = len(a_terms & b_terms) / len(a_terms | b_terms)
            if jaccard >= threshold:
                pairs.append((f"{a_id}:{a_title}", f"{b_id}:{b_title}", round(jaccard, 2)))
    return pairs


# Smoke queries: realistic user phrasings -> expected top-1 doc_id.
# These encode the quality bar: colloquial phrasing must hit the right entry.
SMOKE_QUERIES: list[tuple[str, str]] = [
    ("邮箱收不到外面人发的邮件", "faq-it-011"),
    ("电脑最近特别卡打开个网页都要等半天", "faq-it-013"),
    ("工牌门禁卡丢了进不了办公室", "faq-it-015"),
    ("怀疑电脑中毒了文件被加密还弹出付款窗口", "kb-sw-0004"),
    ("投影仪连上没画面会议马上开始了", "faq-it-012"),
    ("新来的同事第一天需要领电脑和开账号", "faq-it-020"),
    ("打印机打出来是空白纸", "faq-005"),
    ("无线网信号满格但是上不了网", "kb-net-0007"),
    ("屏幕花了一块一块的彩色条纹", "faq-012"),
    ("开机以后一直转圈进不了桌面", "faq-it-006"),
    ("共享盘双击打不开提示权限不足", "faq-008"),
    ("在家怎么连公司内网系统", "faq-007"),
    ("网络交换机灯全灭了整个部门都断网了", "kb-net-0002"),
    ("电脑一插U盘就蓝屏重启", "kb-hw-0004"),
]


def main() -> None:
    entries = load_all()
    print(f"schema/unique-id check : OK ({len(entries)} entries)")

    cats = Counter(e.get("category", "(未分类)") for e in entries)
    stypes = Counter(e.get("source_type", "faq") for e in entries)
    print(f"source_type            : {dict(stypes)}")
    print(f"categories             : {dict(cats)}")

    dupes = near_duplicates(entries)
    if dupes:
        print(f"WARN near-duplicate titles ({len(dupes)}):")
        for a, b, score in dupes:
            print(f"  [{score}] {a}  <->  {b}")
    else:
        print("near-duplicate titles  : none")

    retriever = Retriever(SEED)
    failures = []
    print(f"\nsmoke retrieval ({len(SMOKE_QUERIES)} queries):")
    for query, expected in SMOKE_QUERIES:
        hits = retriever.search(query, top_k=3)
        top = hits[0].document.doc_id if hits else "(no hit)"
        mark = "OK " if top == expected else "MISS"
        if top != expected:
            failures.append((query, expected, top))
        alt = ", ".join(h.document.doc_id for h in hits[1:]) or "-"
        print(f"  [{mark}] {query[:26]:<28} -> {top}  (alt: {alt})")

    if failures:
        print(f"\nFAIL {len(failures)} smoke queries missed expectations")
        sys.exit(1)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
