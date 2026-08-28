"""FAQ retrieval evaluation: Recall@3 >= 90% (objective quality gate).

The eval set is curated alongside the FAQ seed corpus; queries are
natural user phrasings, and the expected doc must rank in the top-3.
The number is produced by this test, not pre-written.
"""
from pathlib import Path

from app.application.retriever import Retriever

SEED_DIR = Path(__file__).resolve().parent.parent / "seed" / "faq"

EVAL_SET: list[tuple[str, str]] = [
    ("年假怎么申请", "faq-proc-001"),
    ("忘记邮箱密码怎么重置", "faq-002"),
    ("WiFi连不上怎么办", "faq-003"),
    ("电脑蓝屏了怎么处理", "faq-004"),
    ("打印机脱机打印不了", "faq-005"),
    ("怎么申请安装新软件", "faq-006"),
    ("如何连VPN远程办公", "faq-007"),
    ("共享盘访问不了", "faq-008"),
    ("发票怎么开", "faq-009"),
    ("报销流程是什么", "faq-010"),
    ("电脑开机黑屏怎么办", "faq-011"),
    ("显示器花屏怎么处理", "faq-012"),
    ("考勤打卡失败怎么办", "faq-013"),
    ("如何预订会议室", "faq-014"),
]

MIN_RECALL_AT_3 = 0.90


def test_faq_recall_at_3() -> None:
    retriever = Retriever(SEED_DIR)
    hits = 0
    misses: list[tuple[str, str, list[str]]] = []
    for query, expected in EVAL_SET:
        top = [h.document.doc_id for h in retriever.search(query, top_k=3)]
        if expected in top:
            hits += 1
        else:
            misses.append((query, expected, top))

    recall = hits / len(EVAL_SET)
    assert recall >= MIN_RECALL_AT_3, f"Recall@3={recall:.2%} < {MIN_RECALL_AT_3:.0%}: {misses}"


def test_faq_eval_set_queries_are_all_grounded() -> None:
    """Every eval query must clear the no-answer gate (else the gate is wrong)."""
    retriever = Retriever(SEED_DIR)
    ungrounded = [q for q, _ in EVAL_SET if retriever.answer(q) is None]
    assert ungrounded == [], f"eval queries not grounded: {ungrounded}"
