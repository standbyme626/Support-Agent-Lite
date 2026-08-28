"""Build office-scenario zh golden datasets (eval 500 + train ~600).

Three data paths:
- Path A: seed/faq titles (real office FAQ, zero generation)
- Path B: CLINC150 scenario migration (financial -> office support)
  via Bailian qwen3.8-flash, 3 variants per source sample
- Path C: direct generation for chitchat / other (content is
  cross-scenario; only the phrasing needs to be natural Chinese)

Pipeline: sample sources -> generate (resumable) -> filter -> split
train/eval (source-level disjoint) -> report.

Usage:
    .venv/bin/python scripts/build_office_dataset.py            # full run
    .venv/bin/python scripts/build_office_dataset.py --stage gen # resume generation
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from pathlib import Path

import httpx
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.infrastructure.llm import load_env_file  # noqa: E402

load_env_file(ROOT / ".env")

OUT_DIR = ROOT / "datasets" / "office_golden"
GEN_DIR = ROOT / "runtime" / "office_gen"
CLINC_DIR = ROOT / "datasets" / "clinc150"
SEED_FAQ = ROOT / "seed" / "faq"

MODEL = "qwen3.8-flash"
BASE_URL = os.environ.get("BAILIAN_BASE_URL", "")
API_KEY = os.environ.get("BAILIAN_API_KEY", "")

SRC_PER_INTENT = 12         # CLINC150 samples per source intent (support/progress)
VARIANTS = 3                # variants per migrated source sample
DIRECT_VARIANTS = 15        # variants per direct generation (chitchat/other)
SEED = 42

# --- source intent lists ------------------------------------------------

SUPPORT_SOURCES = [
    "account_blocked", "card_declined", "damaged_card", "report_lost_card",
    "report_fraud", "freeze_account", "sync_device", "find_phone",
    "lost_luggage", "jump_start",
]
PROGRESS_SOURCES = [
    "order_status", "application_status", "pto_request_status",
    "flight_status", "last_maintenance",
]
CHITCHAT_KINDS = [
    "greeting", "goodbye", "thank_you", "are_you_a_bot", "what_is_your_name",
    "who_made_you", "what_can_i_ask_you", "how_old_are_you",
    "where_are_you_from", "do_you_have_pets",
    "compliment", "apology", "small_talk", "encouragement",
    "work_hours_query", "joke", "self_intro", "reaction",
]
OTHER_KINDS = [
    "天气查询", "股票投资", "新闻八卦", "餐饮推荐", "娱乐影音",
    "旅行规划", "宠物饲养", "编程求助", "健康养生", "房产家居",
    "星座命理", "游戏娱乐", "购物种草", "健身运动", "音乐分享",
    "读书心得", "追星八卦", "生活吐槽",
]

# --- prompts --------------------------------------------------------------

MIGRATE_SYSTEM = """你是企业办公支持助手的数据标注员。任务：把"金融客服场景"的用户问题，迁移改写为"企业办公支持场景"（IT/行政/设施/后勤）里员工会说的自然中文表达。

实体映射规则（金融→办公）：
  银行账户/信用卡/借记卡/储蓄账户 → OA账号/域账号/门禁卡/工牌/饭卡
  密码/PIN码 → 公司邮箱密码/OA登录密码/WiFi密码
  航班/订单/包裹/快递 → 工单/申请单/报销单/维修单/采购单
  转账/取款/汇款/还款 → 请假/报销/审批/报修/申请
  商店/超市/ATM → 工位/会议室/机房/食堂

要求：
1. 保留原句的问法句式、语气和意图
2. 输出严格 JSON：{"variants": ["变体1", "变体2", "变体3"]}
3. 3 个变体要彼此不同（口语化程度、句式结构有差异）
4. 禁止出现任何金融内容词：银行、信用卡、储蓄、取款、转账、汇款、汇率、ATM、借记卡、航班、外卖、快递包裹
5. 只输出 JSON，不要任何解释"""

CHITCHAT_SYSTEM = """你是数据标注员。任务：为"企业办公支持助手"生成自然的中文闲聊样本（员工在群里对客服助手说的话）。

要求：
1. 输出严格 JSON：{"variants": ["v1", "v2", ...]}，共 15 个变体
2. 15 个变体彼此不同（问候/寒暄/感谢/告别/夸赞/吐槽等不同表达方式）
3. 语气自然口语化，像真实员工在群里发消息
4. 不要包含具体人名
5. 只输出 JSON，不要任何解释"""

OTHER_SYSTEM = """你是数据标注员。任务：为"企业办公支持助手"生成中文的"超出服务范围"样本（OOS）——员工发的内容与办公支持完全无关，助手不应触发任何业务处理。

要求：
1. 输出严格 JSON：{"variants": ["v1", "v2", ...]}，共 15 个变体
2. 15 个变体彼此不同
3. 语气自然，像真实员工闲聊/私事
4. 内容与"报修/工单/审批/流程咨询"无关（但可以是职场闲聊，如聊股市、聊天气、聊生活）
5. 只输出 JSON，不要任何解释"""

REWRITE_SYSTEM = """你是数据标注员。任务：把"知识库官方问题标题"改写成员工在群里会说的口语化提问。

要求：
1. 保留原问题意图和核心信息
2. 输出严格 JSON：{"variants": ["v1", "v2", "v3"]}
3. 变体要口语化（去掉"如何/指南/说明"等官方腔），像真实员工提问
4. 只输出 JSON，不要任何解释"""

# --- hand-written templates (zero-cost, eval quality anchors) ------------

HANDWRITTEN_SUPPORT = [
    "A3 空调不制冷了", "打印机一直不出纸", "电脑开机黑屏", "公司网络又断了",
    "我的电脑突然蓝屏了", "会议室投影仪坏了", "门禁卡刷不进去", "共享盘打不开了",
    "考勤机一直提示打卡失败", "我的邮箱密码忘了", "电话分机没声音", "工位灯管闪",
    "饮水机漏水了", "WiFi 连上了但上不了网", "我的工牌突然失效了",
    "OA 系统登录不上去", "显示器花屏了", "会议室空调温度调不了",
    "打印机卡纸了", "我的电脑很卡打开软件就死机", "键盘有几个键没反应",
    "门锁坏了锁不上", "路由器重启后还是连不上", "视频会议软件打开就闪退",
    "我的工位电脑开不了机", "网络打印机添加不上", "申请的门禁权限还没开通",
    "椅子坏了一坐就塌", "冰箱不制冷了", "楼下的闸机刷不开",
]
HANDWRITTEN_PROGRESS = [
    "T0001 处理到哪一步了", "我昨天报修的那个事怎么样了", "空调那个工单好了吗",
    "我的年假申请批下来了吗", "报销单审批到哪了", "之前说的网络问题处理了吗",
    "T0012 有人处理了吗", "我上周申请的新电脑有进度吗", "打印机那个维修到哪了",
    "我的软件安装申请审核了吗", "门禁开通申请有结果了吗", "那个蓝屏的事有进展吗",
    "T1024 现在谁在处理", "我报的故障单到哪了", "会议室预约确认了吗",
    "采购单批了没有", "工单处理完了吗", "我的权限申请好了吗",
    "电脑维修大概什么时候能好", "报修两天了还没动静", "那个摄像头什么时候来修",
    "T0088 能催一下吗", "VPN 申请有消息了吗", "我换显示器的申请到哪了",
    "考勤机维修有进度吗", "wifi 问题解决了没有", "之前那个工单还要多久",
    "我的报修单还在处理吗", "T0033 处理到哪了", "能不能查下我那个工单进度",
    "软件安装到我了没有", "审批流程走到哪了", "那个门锁修好了吗",
    "我的故障单状态是什么", "上次说周五能修好 现在怎么样了", "快递柜申请有结果吗",
    "T0100 还在等谁", "空调修到哪一步了", "共享盘权限申请批了吗",
    "我报的事什么时候能有结果",
]

# --- filters ----------------------------------------------------------------

BANNED_WORDS = [
    "银行", "信用卡", "储蓄", "取款", "转账", "汇款", "汇率", "ATM",
    "借记卡", "航班", "外卖", "快递", "包裹", "美元", "英镑", "账户余额",
    "信用卡还款", "存款", "贷款", "利息", "股票市场行情数据", "预订航班",
]
PINYIN_RE = re.compile(r"^[a-z\s]+$")
ENGLISH_RE = re.compile(r"[a-zA-Z]{3,}")

# faq titles that are actually troubleshooting / noise, not process Q&A
TROUBLESHOOT_HINTS = (
    "处理", "排查", "解决", "异常", "故障", "失败", "无法", "不了",
    "不可用", "中断", "恢复", "修复", "报错", "卡", "打不开", "连不上",
    "黑屏", "花屏", "蓝屏", "死机", "不亮", "SOP#", "总思路", "指南",
    "如何应对", "应如何", "怎么办", "为什么", "是什么", "原因", "泄露",
    "安全事件", "告警", "性能下降", "响应时间", "丢失", "消失", "冲突",
)
FAQQA_HINTS = (
    "申请", "流程", "如何", "怎么", "开具", "报销", "补办", "预订",
    "配置", "开通", "变更", "恢复出厂", "设置", "打印", "登录", "注册",
)

# Domain-specific titles that look like process questions ("如何…") but
# are actually technical/medical/finance tutorials — NOT IT-service-desk
# process FAQs. Excluded from the faq bucket (they stay in the RAG corpus
# as knowledge, but never become intent-eval samples).
PROFESSIONAL_HINTS = (
    "Firebase", "Kubernetes", "macOS", "Linux", "Windows", "Python",
    "SQL", "Docker", "API", "Alteryx", "Salesforce", "Airtable",
    "Scikit-learn", "Laravel", "PostgreSQL", "KVM", "Hadoop", "SaaS",
    "云平台", "数据库", "架构", "集成", "部署", "监控系统", "中间件",
    "患者", "医疗", "病案", "影像", "诊断报告", "健康数据", "临床",
    "投资", "量化", "基金", "股票", "期货", "理财", "策略分析",
    "数据分析", "数据安全", "数据可视化", "数字化", "营销", "品牌增长",
    "电商", "外包", "培训课程", "开源软件",
)

# Business-domain words that must never appear in chitchat/other samples
# (a mixed-intent utterance like "谢谢，顺便问下加班调休怎么算" would
# teach the model the wrong decision boundary).
BUSINESS_HINTS = (
    "申请", "报修", "工单", "进度", "报销", "审批", "请假", "调休",
    "加班", "权限", "门禁", "账号", "密码", "VPN", "邮箱", "打印机",
    "电脑", "网络", "WiFi", "会议室", "故障", "维修", "开通", "报销单",
    "流程", "年假", "事假", "出差", "采购", "工位", "考勤", "打卡",
)


def banned_hit(text: str) -> str | None:
    for w in BANNED_WORDS:
        if w in text:
            return w
    return None


def quality_ok(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    if len(t) < 4 or len(t) > 60:
        return False
    if PINYIN_RE.match(t):
        return False
    if ENGLISH_RE.search(t):
        return False
    if banned_hit(t):
        return False
    return True


def is_process_faq(title: str) -> bool:
    """True when a seed/faq title is a pure process/policy question
    (not troubleshooting, not a technical/medical/finance tutorial)."""
    if any(h in title for h in TROUBLESHOOT_HINTS):
        return False
    if any(h in title for h in PROFESSIONAL_HINTS):
        return False
    return any(h in title for h in FAQQA_HINTS) or title.endswith("？") or title.endswith("?")


def business_hit(text: str) -> str | None:
    """First business-domain word found in text (for chitchat/other cleanup)."""
    for w in BUSINESS_HINTS:
        if w in text:
            return w
    return None


# --- bailian client ----------------------------------------------------------

class Bailian:
    def __init__(self, model: str = MODEL, timeout: float = 60.0) -> None:
        self.model = model
        self._timeout = timeout

    def chat(self, system: str, user: str) -> str:
        resp = httpx.post(
            f"{BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.8,
                "max_tokens": 300,
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


def extract_variants(content: str) -> list[str]:
    """Robust JSON extraction: strip markdown fences, regex-find the object."""
    content = content.strip()
    m = re.search(r"\{[^{}]*\"variants\"[^{}]*\}", content, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group())
        vs = data.get("variants", [])
        return [str(v).strip() for v in vs if str(v).strip()]
    except json.JSONDecodeError:
        return []


# --- source sampling -----------------------------------------------------------

def load_clinc() -> tuple[pd.DataFrame, dict[str, int]]:
    train = pd.read_parquet(CLINC_DIR / "train-00000-of-00001.parquet")
    intents_df = pd.read_parquet(CLINC_DIR / "intents-00000-of-00001.parquet")
    name2id = {row.name: int(row.id) for row in intents_df.itertuples()}
    return train, name2id


def sample_sources() -> dict[str, list[dict]]:
    """Deterministic source sampling (fixed seed, reproducible)."""
    rng = random.Random(SEED)
    train, name2id = load_clinc()
    sources: dict[str, list[dict]] = {}

    def pick(intent_names: list[str]) -> list[dict]:
        out: list[dict] = []
        for name in intent_names:
            lid = name2id.get(name)
            if lid is None:
                continue
            utts = train[train["label"] == lid]["utterance"].tolist()
            rng.shuffle(utts)
            for u in utts[:SRC_PER_INTENT]:
                out.append({"intent": name, "text": u})
        return out

    sources["support"] = pick(SUPPORT_SOURCES)
    sources["progress_query"] = pick(PROGRESS_SOURCES)
    # chitchat / other are generated directly (no CLINC source needed), but we
    # keep stable "kind" anchors for prompt variety.
    sources["chitchat"] = [{"intent": k, "text": ""} for k in CHITCHAT_KINDS]
    sources["other"] = [{"intent": k, "text": ""} for k in OTHER_KINDS]
    return sources


def load_faq_titles() -> list[str]:
    titles: list[str] = []
    for p in sorted(SEED_FAQ.glob("*.json")):
        payload = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict) and item.get("title"):
                    titles.append(str(item["title"]).strip())
    return titles


# --- generation (resumable) -----------------------------------------------------

def done_keys() -> set[str]:
    done: set[str] = set()
    for f in GEN_DIR.glob("*.jsonl"):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                done.add(json.loads(line)["key"])
    return done


def append_result(key: str, bucket: str, variants: list[str], meta: dict) -> None:
    GEN_DIR.mkdir(parents=True, exist_ok=True)
    path = GEN_DIR / f"{bucket}.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"key": key, "variants": variants, **meta}, ensure_ascii=False) + "\n")


def run_generation() -> None:
    client = Bailian()
    sources = sample_sources()
    done = done_keys()
    faq_titles = load_faq_titles()

    # --- path B: migrate support / progress from CLINC150 ---
    for bucket in ("support", "progress_query"):
        system = MIGRATE_SYSTEM
        for src in sources[bucket]:
            key = f"{bucket}:{src['intent']}:{src['text'][:60]}"
            if key in done:
                continue
            user = json.dumps({"intent": src["intent"], "source": src["text"]}, ensure_ascii=False)
            variants: list[str] = []
            for attempt in range(3):
                try:
                    content = client.chat(system, user)
                    variants = extract_variants(content)
                    if len(variants) >= 1:
                        break
                except Exception as exc:  # noqa: BLE001
                    print(f"  retry {key[:50]} ({attempt+1}): {exc!r}", flush=True)
                    time.sleep(2)
            if variants:
                append_result(key, bucket, variants, {"src_intent": src["intent"], "source": src["text"]})
                print(f"[{bucket}] {src['text'][:50]} -> {len(variants)} 变体", flush=True)
            else:
                print(f"[{bucket}] FAIL {src['text'][:50]}", flush=True)
            time.sleep(0.3)

    # --- path C: generate chitchat / other directly ---
    for bucket, system in (("chitchat", CHITCHAT_SYSTEM), ("other", OTHER_SYSTEM)):
        for src in sources[bucket]:
            key = f"{bucket}:{src['intent']}"
            if key in done:
                continue
            user = f"子类: {src['intent']}\n生成 15 个自然中文样本。"
            variants: list[str] = []
            for attempt in range(3):
                try:
                    content = client.chat(system, user)
                    variants = extract_variants(content)
                    if len(variants) >= 5:
                        break
                except Exception as exc:  # noqa: BLE001
                    print(f"  retry {key} ({attempt+1}): {exc!r}", flush=True)
                    time.sleep(2)
            if variants:
                append_result(key, bucket, variants, {"kind": src["intent"]})
                print(f"[{bucket}] {src['intent']} -> {len(variants)} 变体", flush=True)
            else:
                print(f"[{bucket}] FAIL {src['intent']}", flush=True)
            time.sleep(0.3)

    # --- path A: rewrite faq titles into colloquial phrasing ---
    rng = random.Random(SEED)
    rng.shuffle(faq_titles)
    rewrite_targets = faq_titles
    for title in rewrite_targets:
        key = f"faq_rewrite:{title[:60]}"
        if key in done:
            continue
        user = f"官方标题: {title}"
        variants: list[str] = []
        for attempt in range(3):
            try:
                content = client.chat(REWRITE_SYSTEM, user)
                variants = extract_variants(content)
                if len(variants) >= 1:
                    break
            except Exception as exc:  # noqa: BLE001
                print(f"  retry {key[:50]} ({attempt+1}): {exc!r}", flush=True)
                time.sleep(2)
        if variants:
            append_result(key, "faq_rewrite", variants, {"source_title": title})
            print(f"[faq] {title[:40]} -> {len(variants)} 变体", flush=True)
        else:
            print(f"[faq] FAIL {title[:40]}", flush=True)
        time.sleep(0.3)


# --- filtering + split ----------------------------------------------------------

def load_generated() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for f in GEN_DIR.glob("*.jsonl"):
        bucket = f.stem
        out[bucket] = []
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out[bucket].append(json.loads(line))
    return out


def filter_variants(records: list[dict]) -> list[dict]:
    """Return records with only quality-passing, de-duplicated variants."""
    seen: set[str] = set()
    cleaned: list[dict] = []
    for rec in records:
        good: list[str] = []
        for v in rec["variants"]:
            if not quality_ok(v):
                continue
            if v in seen:
                continue
            seen.add(v)
            good.append(v)
        if good:
            cleaned.append({**rec, "variants": good})
    return cleaned


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=["gen", "filter"], default="gen")
    args = parser.parse_args()

    if args.stage == "gen":
        if not API_KEY or not BASE_URL:
            print("BAILIAN_API_KEY / BAILIAN_BASE_URL missing", file=sys.stderr)
            return 1
        run_generation()
        return 0

    # filter + assemble
    gen = load_generated()
    cleaned = {k: filter_variants(v) for k, v in gen.items()}

    faq_titles = load_faq_titles()
    rng = random.Random(SEED)
    rng.shuffle(faq_titles)

    # --- eval set: 5 x 100 ---
    eval_records: list[dict] = []
    # faq: 50 official process titles + 50 rewritten colloquial
    faq_process = [t for t in faq_titles if is_process_faq(t)]
    faq_eval_official = faq_process[:50]
    faq_rewrites = cleaned.get("faq_rewrite", [])
    faq_rewrite_texts = [v for rec in faq_rewrites for v in rec["variants"]]
    faq_eval_colloquial = [t for t in faq_rewrite_texts if is_process_faq(t)][:50]
    for t in faq_eval_official:
        eval_records.append({"text": t, "expected_intent": "faq", "source": "seed_faq"})
    for t in faq_eval_colloquial:
        eval_records.append({"text": t, "expected_intent": "faq", "source": "rewrite"})

    # support: 30 handwritten + 70 migrated (from records NOT used in train)
    for t in HANDWRITTEN_SUPPORT:
        eval_records.append({"text": t, "expected_intent": "support", "source": "handwritten"})
    support_migrated = [v for rec in cleaned.get("support", []) for v in rec["variants"]]
    eval_records += [
        {"text": t, "expected_intent": "support", "source": "migrate"}
        for t in support_migrated[:70]
    ]

    # progress: 40 handwritten + 60 migrated
    for t in HANDWRITTEN_PROGRESS:
        eval_records.append({"text": t, "expected_intent": "progress_query", "source": "handwritten"})
    progress_migrated = [v for rec in cleaned.get("progress_query", []) for v in rec["variants"]]
    eval_records += [
        {"text": t, "expected_intent": "progress_query", "source": "migrate"}
        for t in progress_migrated[:60]
    ]

    # chitchat / other: 100 each from generated (business-word cleaned —
    # "谢谢，顺便问下加班调休怎么算" is a mixed intent, never chitchat)
    chitchat = [
        v for rec in cleaned.get("chitchat", []) for v in rec["variants"]
        if not business_hit(v)
    ]
    other = [
        v for rec in cleaned.get("other", []) for v in rec["variants"]
        if not business_hit(v)
    ]
    eval_records += [{"text": t, "expected_intent": "chitchat", "source": "gen"} for t in chitchat[:100]]
    eval_records += [{"text": t, "expected_intent": "other", "source": "gen"} for t in other[:100]]

    # --- train set: balanced slices (disjoint from eval where possible) ---
    train_records: list[dict] = []
    faq_train_official = faq_process[50:170]
    faq_train_colloquial = [t for t in faq_rewrite_texts if is_process_faq(t)][50:150]
    for t in faq_train_official:
        train_records.append({"text": t, "expected_intent": "faq", "source": "seed_faq"})
    for t in faq_train_colloquial[:100]:
        train_records.append({"text": t, "expected_intent": "faq", "source": "rewrite"})
    # support/progress: use variants beyond eval slice
    train_records += [
        {"text": t, "expected_intent": "support", "source": "migrate"}
        for t in support_migrated[70:190]
    ]
    train_records += [
        {"text": t, "expected_intent": "progress_query", "source": "migrate"}
        for t in progress_migrated[60:160]
    ]
    train_records += [
        {"text": t, "expected_intent": "chitchat", "source": "gen"}
        for t in chitchat[100:250]
    ]
    train_records += [
        {"text": t, "expected_intent": "other", "source": "gen"}
        for t in other[100:250]
    ]

    # --- write out ---
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUT_DIR / "eval_500.jsonl", "w", encoding="utf-8") as f:
        for i, r in enumerate(eval_records):
            f.write(json.dumps({"id": f"office-eval-{i:04d}", **r}, ensure_ascii=False) + "\n")
    with open(OUT_DIR / "train_650.jsonl", "w", encoding="utf-8") as f:
        for i, r in enumerate(train_records):
            f.write(json.dumps({"id": f"office-train-{i:04d}", **r}, ensure_ascii=False) + "\n")

    from collections import Counter
    print("\n=== eval distribution ===")
    print(dict(Counter(r["expected_intent"] for r in eval_records)))
    print("=== train distribution ===")
    print(dict(Counter(r["expected_intent"] for r in train_records)))
    print(f"eval={len(eval_records)} train={len(train_records)}")
    print(f"outputs: {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())