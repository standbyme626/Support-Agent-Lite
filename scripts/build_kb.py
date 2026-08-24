"""Build the unified KB seed files.

Merges the current curated FAQ with legacy Chinese seeds
(reference/seed_data), dedupes by normalized title (the curated version
wins), validates schema, and emits:

  seed/faq/kb_legacy_faq.json   legacy IT FAQ entries not already covered
  seed/faq/kb_sop.json          operations SOPs (source_type=sop)

Re-runnable: existing outputs are regenerated from scratch.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "seed" / "faq"

REQUIRED_FIELDS = ("doc_id", "title", "content")


def norm_title(title: str) -> str:
    """Normalize a title for duplicate comparison: drop whitespace,
    punctuation, and common prefixes."""
    cleaned = re.sub(r"[\s，。？?！!：:（）()【】\[\]—\-–_/\\]", "", title)
    return re.sub(r"^(sop|faq|如何|怎么办|怎么处理)", "", cleaned.lower())


def load(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def validate(entries: list[dict], origin: str) -> list[dict]:
    checked: list[dict] = []
    seen_ids: set[str] = set()
    for raw in entries:
        missing = [f for f in REQUIRED_FIELDS if not raw.get(f)]
        if missing:
            raise ValueError(f"{origin}: {raw.get('doc_id', '?')} missing {missing}")
        if raw["doc_id"] in seen_ids:
            raise ValueError(f"{origin}: duplicate doc_id {raw['doc_id']}")
        seen_ids.add(raw["doc_id"])
        if len(str(raw["content"])) < 30:
            raise ValueError(f"{origin}: {raw['doc_id']} content too short")
        entry = {
            "doc_id": str(raw["doc_id"]),
            "source_type": str(raw.get("source_type", "faq")),
            "title": str(raw["title"]),
            "content": str(raw["content"]),
            "tags": [str(t) for t in raw.get("tags", ())],
        }
        if raw.get("category"):
            entry["category"] = str(raw["category"])
        checked.append(entry)
    return checked


def main() -> None:
    current = validate(load(ROOT / "seed/faq/faq_documents.json"), "current")
    legacy_faq = validate(load(ROOT / "reference/seed_data/faq/faq_it_support.json"), "legacy-faq")
    sops: list[dict] = []
    for sop_file in sorted((ROOT / "reference/seed_data/sop").glob("*.json")):
        sops.extend(validate(load(sop_file), f"sop:{sop_file.name}"))

    known_titles = {norm_title(e["title"]) for e in current}
    known_ids = {e["doc_id"] for e in current}

    merged, skipped = [], []
    for entry in legacy_faq:
        if norm_title(entry["title"]) in known_titles:
            skipped.append((entry["doc_id"], entry["title"], "duplicate-title"))
            continue
        if entry["doc_id"] in known_ids:
            skipped.append((entry["doc_id"], entry["title"], "duplicate-id"))
            continue
        merged.append(entry)
        known_titles.add(norm_title(entry["title"]))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "kb_legacy_faq.json").write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT_DIR / "kb_sop.json").write_text(
        json.dumps(sops, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"current kept      : {len(current)}")
    print(f"legacy faq merged : {len(merged)} -> kb_legacy_faq.json")
    print(f"sops imported     : {len(sops)} -> kb_sop.json")
    for doc_id, title, reason in skipped:
        print(f"  skipped {doc_id} [{reason}]: {title}")
    total = len(current) + len(merged) + len(sops)
    print(f"TOTAL zh KB entries: {total}")


if __name__ == "__main__":
    main()
