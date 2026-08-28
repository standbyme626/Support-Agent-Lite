"""Build datasets/zh_golden/: zh intent-eval set + diagnosis few-shots.

升级计划 §5.2 抽样本地化方案(替代全量翻译):
  intent_eval_500.jsonl      CLINC150 → 项目意图体系(faq/support/
                             progress_query/chitchat/other),LLM 中文改写,
                             混入业务词表,~10% 直译对照,OOS(test split
                             NaN 标签)作为"other/安全拒答"样本;
  diagnosis_fewshot_50.jsonl it-support-tickets 精选四元组本地化
                             (报修→诊断→根因→处置)。

质量控制:意图形态保持约束(prompt 层)+ 回译一致性校验(zh→en token
recall,仅内容类桶)+ 近重复合并。Resume-safe:已完成 id 记录在
datasets/.zh_golden_done.txt,逐条原子追加。

Usage:
    .venv/bin/python scripts/build_zh_golden.py intents --limit 12   # pilot
    .venv/bin/python scripts/build_zh_golden.py intents              # full 500
    .venv/bin/python scripts/build_zh_golden.py diagnosis            # 50
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.infrastructure.llm import load_env_file  # noqa: E402

OUT_DIR = ROOT / "datasets/zh_golden"
DONE_PATH = ROOT / "datasets/.zh_golden_done.txt"
CLINC_DIR = ROOT / "datasets/clinc150"
ITS_PATH = ROOT / "datasets/it-support-tickets/train.parquet"

TARGETS = {"support": 140, "faq": 110, "progress_query": 100, "chitchat": 60, "other": 90}
# CLINC150 source intents per bucket, curated by UTTERANCE FORM so the
# expected intent survives topical rewrite into the enterprise-IT context.
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
DIRECT_EVERY = 10  # ~10% 直译对照样本

SYSTEM_PROMPT = (
    "你是企业IT服务台的评测集构建员。把给定的英文用户话术改写成中文企业办公"
    "场景下的自然表达。要求:\n"
    "1) 只输出改写后的中文句子本身,不要解释、引号或任何标记;\n"
    "2) 严格保持指定的「意图形态」——评测集靠形态标注答案,形态变了样本就废了;\n"
    "3) 长度 8~40 个汉字,口语自然,像员工在企业群里发消息;\n"
    "4) 不要出现真实人名/公司名/品牌名。\n"
)

BUCKET_RULES = {
    "support": (
        "意图形态=报告故障/请求维修(设备或系统出了问题,需要人处理)。"
        "必须保留明确的故障语义。可自然融入这些词:门禁/工牌/共享盘/打印机/"
        "空调/VPN/邮箱/考勤机/会议室系统/工单。"
    ),
    "faq": (
        "意图形态=咨询操作方法/流程/定义(疑问句)。必须是问句语气"
        "(如何/怎么/什么/能否)。可自然融入企业IT场景(邮箱/共享盘/VPN等)。"
    ),
    "progress_query": (
        "意图形态=查询自己提交过的事项的处理进度或当前状态。"
        "可改为查询工单/报修单的进度表达。"
    ),
    "chitchat": (
        "意图形态=寒暄/问候/感谢/道别/询问机器人身份。直接意译成中文日常"
        "表达即可,不要套办公故障场景。"
    ),
    "other": (
        "意图形态=与IT服务台能力无关的生活类请求(如订票/点歌/菜谱/算术/"
        "天气等)。忠实意译为中文生活类请求;禁止出现IT/办公/故障词汇——"
        "这类样本用于训练「安全拒答」判定。"
    ),
}

BACKTRANSLATE_PROMPT = "把下面的中文句子翻译回英文,只输出英文句子本身:\n\n"


class Llm:
    """Minimal Bailian chat client (thread-safe; hard socket timeouts).

    Note: SIGALRM watchdogs are unusable inside worker threads, so forward
    progress is guaranteed by explicit connect/read timeouts instead.
    """

    def __init__(self, model: str) -> None:
        import httpx

        load_env_file()
        self._key = os.environ["LLM_API_KEY"]
        self._base = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1")
        self.model = model
        self._client = httpx.Client(timeout=httpx.Timeout(75, connect=10))
        self.tokens = 0

    def chat(self, system: str, user: str, *, temperature: float = 0.3) -> str:
        import httpx as _httpx

        for attempt in range(4):
            try:
                resp = self._client.post(
                    f"{self._base}/chat/completions",
                    headers={"Authorization": f"Bearer {self._key}"},
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        "temperature": temperature,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                self.tokens += int((data.get("usage") or {}).get("total_tokens", 0))
                return str(data["choices"][0]["message"]["content"]).strip()
            except _httpx.HTTPStatusError as exc:
                if exc.response.status_code in (403, 429) and attempt < 3:
                    time.sleep(2 ** (attempt + 1))  # 2/4/8s backoff
                    continue
                raise
        raise RuntimeError("unreachable: retry loop exhausted")  # type: ignore[misc]


def _token_recall(original_en: str, back_en: str) -> float:
    orig = {w for w in original_en.lower().split() if len(w) > 2}
    if not orig:
        return 1.0
    back = {w for w in back_en.lower().split()}
    return len(orig & back) / len(orig)


def _append_line(path: Path, record: dict) -> None:
    line = json.dumps(record, ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _load_done() -> set[str]:
    return set(DONE_PATH.read_text(encoding="utf-8").split()) if DONE_PATH.exists() else set()


def _mark_done(key: str) -> None:
    with open(DONE_PATH, "a", encoding="utf-8") as fh:
        fh.write(key + "\n")


def _item_key(bucket: str, text: str) -> str:
    import hashlib

    return "int:" + hashlib.md5(f"{bucket}:{text}".encode()).hexdigest()[:12]


# --- intents ------------------------------------------------------------------


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
            take = n + 2  # spares for dedup/filters
            sampled = rows.sample(n=min(take, len(rows)), random_state=rng.randint(0, 2**31))
            for _, row in sampled.iterrows():
                text = str(row["utterance"]).strip()
                if not (3 <= len(text) <= 120):
                    continue
                pool.append({"bucket": bucket, "clinc_intent": intent, "text": text})
                if sum(1 for p in pool if p["clinc_intent"] == intent) >= per + (1 if si < remainder else 0):
                    break

    # OOS: NaN-label utterances from the official test split → "other"
    test = pd.read_parquet(CLINC_DIR / "test-00000-of-00001.parquet")
    oos = test[test["label"].isna()]
    oos = oos[oos["utterance"].str.len().between(3, 120)]
    sampled_oos = oos.sample(n=TARGETS["other"], random_state=rng.randint(0, 2**31))
    for _, row in sampled_oos.iterrows():
        pool.append({"bucket": "other", "clinc_intent": "oos", "text": str(row["utterance"]).strip()})
    return pool


def _rewrite_one(llm: Llm, seq_box: list, lock, out: Path, seen_texts: set, item: dict) -> tuple[bool, str]:
    bucket = item["bucket"]
    key = _item_key(bucket, item["text"])
    with lock:
        seq_box[0] += 1
        rid = f"zh-intent-{seq_box[0]:04d}"
        direct = seq_box[0] % DIRECT_EVERY == 0
    if direct:
        user_msg = (
            f"意图形态要求:{BUCKET_RULES[bucket]}\n"
            "本次任务:直译对照样本——只需把原句准确翻译成中文。\n\n"
            f"原句:{item['text']}"
        )
    else:
        user_msg = (
            f"意图形态要求:{BUCKET_RULES[bucket]}\n"
            "本次任务:中文改写(不直译,换一种同形态的表达),"
            "与常见说法措辞错开。\n\n"
            f"原句:{item['text']}"
        )

    try:
        text_zh = llm.chat(SYSTEM_PROMPT, user_msg).strip().strip('"“”')
    except Exception as exc:  # noqa: BLE001 - keep the batch alive
        print(f"  CALL-FAIL {rid}: {exc!r}", flush=True)
        return False, ""

    if not (4 <= len(text_zh) <= 60):
        print(f"  INVALID   {rid}: {text_zh[:40]!r}", flush=True)
        return False, ""

    back_recall = None
    if bucket in ("support", "faq", "progress_query") and not direct:
        try:
            back = llm.chat("你是翻译助手。", BACKTRANSLATE_PROMPT + text_zh, temperature=0)
            back_recall = round(_token_recall(item["text"], back), 3)
        except Exception as exc:  # noqa: BLE001
            print(f"  BACK-FAIL {rid}: {exc!r}", flush=True)

    record = {
        "id": rid,
        "text": text_zh,
        "expected_intent": bucket,
        "clinc_intent": item["clinc_intent"],
        "source_split": "test" if item["clinc_intent"] == "oos" else "train",
        "style": "direct" if direct else "rewritten",
        "back_recall": back_recall,
    }
    with lock:
        if text_zh in seen_texts:
            return False, ""  # 近重复丢弃(不标记 done,下次可重试别的表达)
        seen_texts.add(text_zh)
        _append_line(out, record)
        _mark_done(key)
    print(f"  OK {rid} [{bucket}] {text_zh[:36]}", flush=True)
    return True, rid


def build_intents(llm: Llm, limit: int, sleep: float) -> None:
    from concurrent.futures import ThreadPoolExecutor

    out = OUT_DIR / "intent_eval_500.jsonl"
    done = _load_done()
    seen_texts: set[str] = set()
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                seen_texts.add(rec["text"])
            except Exception:
                continue

    pool = [p for p in _sample_pool() if _item_key(p["bucket"], p["text"]) not in done]
    if limit:
        counts: dict[str, int] = {}
        limited: list[dict] = []
        for p in pool:
            b = p["bucket"]
            if counts.get(b, 0) >= limit:
                continue
            counts[b] = counts.get(b, 0) + 1
            limited.append(p)
        pool = limited
    print(f"pool this run: {len(pool)}", flush=True)

    seq_box = [sum(1 for _ in out.open(encoding="utf-8")) if out.exists() else 0]
    lock = threading.Lock()
    ok = fail = 0
    workers = 6

    def _job(item: dict) -> tuple[bool, str]:
        nonlocal ok, fail
        success, _ = _rewrite_one(llm, seq_box, lock, out, seen_texts, item)
        with lock:
            ok += 1 if success else 0
            fail += 0 if success else 1
        time.sleep(sleep)
        return success, ""

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(_job, pool))
    print(f"\ndone: ok={ok} fail={fail} total_tokens≈{llm.tokens}", flush=True)


# --- diagnosis few-shots ------------------------------------------------------


def build_diagnosis(llm: Llm, limit: int, sleep: float) -> None:
    out = OUT_DIR / "diagnosis_fewshot_50.jsonl"
    done = _load_done()
    df = pd.read_parquet(ITS_PATH)
    df = df[df["status"] == "closed"]

    picked: list[dict] = []
    seen_cause: set[str] = set()
    for _, row in df.iterrows():
        ticket = row["ticket"] or {}
        cause = str(row.get("root_cause") or "").strip()
        steps = row["resolution"].get("steps") if isinstance(row.get("resolution"), dict) else None
        corr = row.get("correspondence") or []
        diag = str((row.get("diagnostics") or {}).get("summary", "")).strip()
        title = str(ticket.get("submitted_title", "")).strip()
        desc = str(ticket.get("submitted_description", "")).strip()
        if not (cause and diag and title and desc and isinstance(steps, list) and steps):
            continue
        key = cause[:48].lower()
        if key in seen_cause:
            continue  # 根因多样性优先
        seen_cause.add(key)
        dialogue = "\n".join(
            f"{t.get('role','?')}: {str(t.get('message',''))[:200]}" for t in corr[:6]
        )
        picked.append(
            {
                "source_record_id": row["record_id"],
                "title": title,
                "desc": desc[:600],
                "dialogue": dialogue[:1200],
                "diag": diag[:600],
                "cause": cause[:400],
                "steps": [str(s)[:220] for s in steps[:6]],
            }
        )
        if len(picked) >= (limit or 50):
            break
    print(f"picked records: {len(picked)}")

    SYSTEM = (
        "你是企业IT服务台的工程师,把给定的英文事件记录本地化为中文四元组"
        "(报修→诊断→根因→处置),用于诊断助手的少样本示例。只输出 JSON:\n"
        '{"ticket_title": string(报修标题,简短中文), "ticket_desc": string(员工'
        "报修描述,口语化中文,80字内), \"diagnosis\": string(诊断过程要点,100字内,"
        "可含关键检查步骤), \"root_cause\": string(根因一句话), "
        "\"resolution_steps\": string[](处置步骤数组,每条一句中文,3~5条)}\n"
        "要求:基于原文改写而非逐字直译;主机名/IP/账号名等用泛称(如\"文件服务器\""
        "/\"域控制器\");不编造原文没有的事实。"
    )

    ok = fail = 0
    for item in picked:
        rid = f"zh-diag-{item['source_record_id']}"
        if f"diag:{item['source_record_id']}" in done:
            continue
        user_msg = (
            f"报修标题:{item['title']}\n报修描述:{item['desc']}\n"
            f"处理对话摘录:\n{item['dialogue']}\n\n诊断结论:{item['diag']}\n"
            f"根因:{item['cause']}\n处置步骤:{json.dumps(item['steps'], ensure_ascii=False)}"
        )
        try:
            raw = llm.chat(SYSTEM, user_msg, temperature=0.2)
            start, end = raw.find("{"), raw.rfind("}")
            obj = json.loads(raw[start : end + 1])
            assert isinstance(obj.get("ticket_title"), str) and obj["ticket_title"]
            assert isinstance(obj.get("resolution_steps"), list) and obj["resolution_steps"]
            assert isinstance(obj.get("root_cause"), str) and obj["root_cause"]
        except Exception as exc:  # noqa: BLE001
            preview = raw[:60] if "raw" in locals() else ""
            print(f"  FAIL {rid}: {exc!r}: {preview}", flush=True)
            fail += 1
            time.sleep(sleep * 2)
            continue

        record = {
            "id": rid,
            "source_record_id": item["source_record_id"],
            "ticket_title": obj["ticket_title"].strip(),
            "ticket_desc": str(obj.get("ticket_desc", "")).strip(),
            "diagnosis": str(obj.get("diagnosis", "")).strip(),
            "root_cause": obj["root_cause"].strip(),
            "resolution_steps": [str(s).strip() for s in obj["resolution_steps"]][:6],
        }
        _append_line(out, record)
        _mark_done(f"diag:{item['source_record_id']}")
        ok += 1
        print(f"  OK {rid} [{record['ticket_title'][:30]}]", flush=True)
        time.sleep(sleep)

    print(f"\ndone: ok={ok} fail={fail} tokens≈{llm.tokens}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("task", choices=["intents", "diagnosis"])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--model", default=os.environ.get("LLM_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free"))
    ap.add_argument("--sleep", type=float, default=0.5)
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    llm = Llm(args.model)
    if args.task == "intents":
        build_intents(llm, args.limit, args.sleep)
    else:
        build_diagnosis(llm, args.limit, args.sleep)


if __name__ == "__main__":
    main()
