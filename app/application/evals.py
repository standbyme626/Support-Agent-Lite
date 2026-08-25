"""C3: standardized quality evaluation assets.

`scripts/run_evals.py` consumes this module to produce the README
quality table from live measurements instead of hand-maintained numbers:

- run_rag_eval:      keyword Recall@3 / MRR over the curated case set
- run_hybrid_eval:   same cases through HybridRetriever (vector+rerank)
                     when an index exists; None otherwise (skipped, not failed)
- export_traces_jsonl: full-chain trace dump for external analysis

All offline and deterministic; the hybrid path degrades exactly like
production retrieval does.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.application.retriever import Retriever

# --- curated eval cases: (query, expected_doc_id) ------------------------------

RAG_CASES: list[tuple[str, str]] = [
    # legacy core FAQ
    ("年假怎么申请", "faq-001"),
    ("忘记邮箱密码怎么重置", "faq-002"),
    ("WiFi连不上怎么办", "faq-003"),
    ("电脑蓝屏了怎么处理", "faq-004"),
    ("打印机脱机打印不了", "faq-005"),
    ("如何连VPN远程办公", "faq-007"),
    ("共享盘访问不了", "faq-008"),
    ("发票怎么开", "faq-009"),
    # dataset-era corpus (kb-* / sop-it-*)
    ("电脑一插U盘就蓝屏重启", "kb-hw-0004"),
    ("交换机灯全灭了整个部门断网", "kb-net-0002"),
    ("数据库备份怎么做", "sop-it-007"),
    ("在家怎么连公司内网系统", "faq-007"),
    ("服务器磁盘空间不足导致数据服务崩溃", "kb-ds-0170"),
    ("新员工入职怎么领取 IT 设备", "faq-it-020"),
]

HYBRID_CASES: list[tuple[str, str]] = [
    ("电脑一插U盘就蓝屏重启", "kb-hw-0004"),
    ("屏幕一直黑着开不了机", "faq-011"),
    ("在家办公访问不了公司资料库", "faq-007"),
    ("部门网络全断了", "kb-net-0002"),
    ("定期备份数据库的操作步骤", "sop-it-007"),
    ("打印机一直显示脱机状态", "faq-005"),
]

MIN_RECALL_AT_3 = 0.90


def _rank_of(retriever: Retriever, query: str, doc_id: str, top_k: int = 3) -> int:
    hits = retriever.search(query, top_k=top_k)
    for i, hit in enumerate(hits, start=1):
        if hit.document.doc_id == doc_id:
            return i
    return 0


def run_rag_eval(retriever: Retriever) -> dict:
    details = []
    hits = 0
    rr_sum = 0.0
    for query, doc_id in RAG_CASES:
        rank = _rank_of(retriever, query, doc_id)
        hit = rank > 0
        hits += int(hit)
        rr_sum += 1.0 / rank if rank else 0.0
        details.append({"query": query, "expected": doc_id, "hit": hit, "rank": rank})
    n = len(RAG_CASES)
    return {
        "cases": n,
        "hits": hits,
        "recall_at_3": round(hits / n, 4),
        "mrr": round(rr_sum / n, 4),
        "min_recall_at_3": MIN_RECALL_AT_3,
        "details": details,
    }


def run_hybrid_eval(
    retriever: Retriever,
    *,
    index_dir: str | Path,
    embedding=None,
    store=None,
    reranker=None,
) -> dict | None:
    """Same measurement through the vector+rerank path.

    Returns None when no usable index/backend is configured — a skipped
    section in the report, never a failure.
    """
    try:
        from app.application.hybrid_retriever import HybridRetriever
        from app.infrastructure.vector_store import NumpyVectorStore

        vec_store = store
        if vec_store is None:
            candidate = NumpyVectorStore(index_dir)
            if not candidate.load():
                return None
            vec_store = candidate
        hybrid = HybridRetriever(
            retriever,
            embedding=embedding,
            store=vec_store,
            reranker=reranker,
        )
    except Exception:  # noqa: BLE001 - report skip, never crash the runner
        return None

    details = []
    hits = 0
    score_sum = 0.0
    for query, doc_id in HYBRID_CASES:
        top_hits = hybrid.search(query, top_k=3)
        rank = next((i for i, h in enumerate(top_hits, 1) if h.document.doc_id == doc_id), 0)
        top_score = top_hits[0].score if top_hits else 0.0
        hits += int(rank > 0)
        score_sum += float(top_score)
        details.append({
            "query": query,
            "expected": doc_id,
            "hit": rank > 0,
            "rank": rank,
            "top_score": round(float(top_score), 4),
        })
    n = len(HYBRID_CASES)
    return {
        "cases": n,
        "hits": hits,
        "recall_at_3": round(hits / n, 4),
        "avg_top_score": round(score_sum / n, 4),
        "details": details,
    }


# --- trace export ------------------------------------------------------------------


def export_traces_jsonl(conn, out_path: str | Path) -> int:
    """Dump every trace event as one JSON object per line (C3 standard)."""
    rows = conn.execute(
        "SELECT trace_id, stage, payload, created_at FROM trace_events ORDER BY id"
    ).fetchall()
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for row in rows:
            payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"] or "{}")
            fh.write(
                json.dumps(
                    {
                        "trace_id": row["trace_id"],
                        "stage": row["stage"],
                        "payload": payload,
                        "created_at": row["created_at"],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return len(rows)


# --- report writer -------------------------------------------------------------------


def write_report(payload: dict, *, out_dir: str | Path = "runtime") -> Path:
    """Persist the evaluation report as markdown + JSON pair."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    md = out / "eval_report.md"
    js = md.with_suffix(".json")
    js.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 质量评测报告(自动生成)",
        "",
        f"生成时间:{payload.get('generated_at', '-')}",
        "",
        "| 指标 | 结果 |",
        "| --- | --- |",
    ]
    rag = payload.get("rag")
    if rag:
        lines.append(
            f"| RAG Recall@3 | {rag['recall_at_3']:.1%}({rag.get('hits', rag['cases'])}/{rag['cases']}) |"
        )
        lines.append(f"| RAG MRR | {rag['mrr']} |")
    hybrid = payload.get("hybrid")
    if hybrid:
        lines.append(f"| 混合检索 Recall@3 | {hybrid['recall_at_3']:.1%}({hybrid['hits']}/{hybrid['cases']}) |")
        lines.append(f"| 混合检索平均 Top1 分数 | {hybrid['avg_top_score']} |")
    else:
        lines.append("| 混合检索 | 跳过(无索引/后端不可用) |")
    if payload.get("agent_pytest"):
        lines.append(f"| Agent golden set(pytest) | {payload['agent_pytest']} |")
    traces = payload.get("traces_exported")
    if traces is not None:
        lines.append(f"| Trace 导出条数 | {traces} |")
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md
