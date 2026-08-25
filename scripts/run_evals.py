"""C3 one-command quality regression.

Usage:
    .venv/bin/python scripts/run_evals.py            # offline core
    .venv/bin/python scripts/run_evals.py --with-hybrid   # include vector path

Produces runtime/eval_report.md + .json — the README quality table's
numbers come from here, not from hand-editing.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.application.evals import (  # noqa: E402
    export_traces_jsonl,
    run_hybrid_eval,
    run_rag_eval,
    write_report,
)
from app.application.retriever import Retriever  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-dir", default=str(ROOT / "seed" / "faq"))
    parser.add_argument("--index-dir", default=str(ROOT / "runtime" / "vector_index"))
    parser.add_argument("--out-dir", default=str(ROOT / "runtime"))
    parser.add_argument("--db", default=str(ROOT / "runtime" / "support_agent.db"))
    parser.add_argument("--with-hybrid", action="store_true")
    parser.add_argument("--skip-agent", action="store_true")
    args = parser.parse_args()

    retriever = Retriever(args.seed_dir)

    rag = run_rag_eval(retriever)
    status = "OK" if rag["recall_at_3"] >= rag["min_recall_at_3"] else "FAIL"
    print(f"[rag]     recall@3={rag['recall_at_3']:.1%} mrr={rag['mrr']} ({status})")

    hybrid = None
    if args.with_hybrid:
        try:
            from app.infrastructure.llm import load_env_file

            load_env_file(ROOT / ".env")
            from app.infrastructure.vector_store import (
                SiliconFlowEmbedding,
                SiliconFlowReranker,
            )

            hybrid = run_hybrid_eval(
                retriever,
                index_dir=args.index_dir,
                embedding=SiliconFlowEmbedding(),
                reranker=SiliconFlowReranker(),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[hybrid]  skipped: {exc!r}")
        if hybrid:
            print(f"[hybrid]  recall@3={hybrid['recall_at_3']:.1%} avg_top1={hybrid['avg_top_score']}")
    else:
        print("[hybrid]  skipped (--with-hybrid not set)")

    agent_line = ""
    if not args.skip_agent:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_agent_eval.py", "tests/test_rag_eval.py", "-q"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
        tail = (proc.stdout or "").strip().splitlines()
        agent_line = tail[-1] if tail else f"exit={proc.returncode}"
        print(f"[pytest]  {agent_line}")

    traces = 0
    db_path = Path(args.db)
    if db_path.exists():
        import sqlite3

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            traces = export_traces_jsonl(conn, Path(args.out_dir) / "traces_export.jsonl")
        except Exception as exc:  # noqa: BLE001 - missing table is fine
            print(f"[traces]  skipped: {exc!r}")
        finally:
            conn.close()
    print(f"[traces]  exported={traces}")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rag": rag,
        "hybrid": hybrid,
        "agent_pytest": agent_line or None,
        "traces_exported": traces,
    }
    md_path = write_report(payload, out_dir=args.out_dir)
    print(f"report  → {md_path}")
    return 0 if status == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
