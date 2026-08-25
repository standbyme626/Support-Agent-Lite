"""Build the C2 vector index for the zh knowledge base.

Embeds every document in seed/faq/*.json via SiliconFlow
Qwen3-Embedding-8B and persists a local index (numpy by default,
chroma with --backend chroma).

Usage:
    .venv/bin/python scripts/build_vector_index.py [--backend numpy|chroma]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.infrastructure.llm import load_env_file  # noqa: E402

load_env_file(ROOT / ".env")

from app.infrastructure.vector_store import (  # noqa: E402
    ChromaVectorStore,
    NumpyVectorStore,
    SiliconFlowEmbedding,
)


def load_docs(seed_dir: Path) -> list[dict]:
    docs: list[dict] = []
    for path in sorted(seed_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            continue
        for raw in payload:
            if isinstance(raw, dict) and raw.get("doc_id"):
                docs.append(
                    {
                        "doc_id": str(raw["doc_id"]),
                        "text": f"{raw.get('title', '')}\n{raw.get('content', '')}",
                        "title": str(raw.get("title", "")),
                        "category": str(raw.get("category", "")),
                        "source_type": str(raw.get("source_type", "faq")),
                    }
                )
    return docs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-dir", default=str(ROOT / "seed" / "faq"))
    parser.add_argument("--out", default=str(ROOT / "runtime" / "vector_index"))
    parser.add_argument("--backend", choices=["numpy", "chroma"], default="numpy")
    args = parser.parse_args()

    docs = load_docs(Path(args.seed_dir))
    if not docs:
        print("no documents found", file=sys.stderr)
        return 1

    emb = SiliconFlowEmbedding()
    print(f"embedding {len(docs)} documents with {emb.model_name} ...")
    vectors = emb.embed([d["text"] for d in docs])

    if args.backend == "chroma":
        store = ChromaVectorStore(args.out)
    else:
        store = NumpyVectorStore(args.out)
    store.build(docs, vectors)
    store.save()
    print(f"index built: backend={args.backend} docs={store.size} out={args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
