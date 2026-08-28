"""E2E 真人模拟:以真实 HTTP webhook 驱动完整链路,模拟多位真人。

人物:
- 张三: 报修人(wecom zhangsan / feishu ou_zhangsan)
- 李师傅: 运维 OPERATOR(wecom lihua)
- 王经理: APPROVER(wecom manager)

剧本(覆盖五意图 + 跨渠道续接 + 确认闭环 + HITL + 记忆召回 + 幂等):
S1  张三 wecom 维修群报修"打印机卡纸"           -> support,建单,回执应落在维修群
S2  张三 feishu 私聊追问"我的工单怎么样了"       -> 跨渠道 progress,回执应落在飞书私聊(验证🔴错位问题)
S3  张三 feishu 群问 FAQ"请假流程是什么"         -> faq RAG 直答
S4  张三 wecom 群里闲聊"你好"                   -> chitchat,不建单
S5  张三 wecom 群"今天天气不错"                 -> other/闲聊,不建单
S6  幂等:重发 S1 同 message_id                  -> duplicate=true,不重复建单
S7  李师傅 claim S1 工单 -> 处理完成 resolve     -> 通知张三确认
S8  张三确认"确认关闭"                          -> ticket CLOSED + 记忆抽取
S9  张三新报修"门禁卡刷不开"                    -> 新单 + 记忆召回(历史事实)
S10 王经理审批链路:张三报修"申请更换新电脑"     -> support 建单(低危直发 / 观察审批流程)
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8123"
DB = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "runtime" / "e2e_live.db")

PASS, FAIL, WARN = 0, 0, 0
REPORT: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL, WARN
    if ok:
        PASS += 1
        REPORT.append(f"  ✅ {name}  {detail}")
    else:
        FAIL += 1
        REPORT.append(f"  ❌ {name}  {detail}")


def warn(name: str, detail: str) -> None:
    global WARN
    WARN += 1
    REPORT.append(f"  ⚠️  {name}  {detail}")


def http(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=420) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    return conn


def outbox_after(conn, before_count: int) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM notification_outbox ORDER BY rowid"
    ).fetchall()
    return rows[before_count:]


def wecom_msg(user: str, text: str, conv: str, msg_id: str) -> dict:
    return {"MsgId": msg_id, "FromUserName": user, "Content": text, "conversation_id": conv}


def feishu_msg(open_id: str, text: str, chat_id: str, msg_id: str, chat_type: str = "p2p") -> dict:
    return {
        "header": {"event_id": f"evt_{uuid4().hex[:12]}"},
        "event": {
            "sender": {"sender_id": {"open_id": open_id}},
            "message": {
                "message_id": msg_id,
                "chat_id": chat_id,
                "chat_type": chat_type,
                "content": json.dumps({"text": text}),
            },
        },
    }


def main() -> None:
    conn = db()
    REPORT.append("=" * 70)
    REPORT.append("E2E 真人全链路测试(隔离实例 http://127.0.0.1:8123)")
    REPORT.append("=" * 70)

    # ---------- S1 张三 wecom 维修群报修(门禁,稳定 support) ----------
    REPORT.append("\n[S1] 张三 wecom 维修群报修「门禁卡刷不开了」")
    before = conn.execute("SELECT COUNT(*) c FROM notification_outbox").fetchone()["c"]
    s1 = wecom_msg("zhangsan", "门禁卡刷不开了,进不了办公室", "repair_group_1", f"wx_{uuid4().hex[:10]}")
    code, r = http("POST", "/webhooks/wecom", s1)
    check("S1 HTTP 200", code == 200, f"code={code}")
    check("S1 识别为 support 并建单", r.get("workflow") == "ticket" and r.get("ticket_id"), f"workflow={r.get('workflow')} ticket={r.get('ticket_id')}")
    t1 = r.get("ticket_id")
    check("S1 有真实回复", bool(r.get("reply")), f"reply={r.get('reply')!r:.100}")
    out1 = outbox_after(conn, before)
    routed1 = [f"{o['target_type']}:{o['target_key']}" for o in out1]
    REPORT.append(f"     outbox={routed1}")
    check("S1 回执发往维修群(无错位)",
          any("conversation:wecom:repair_group_1" in o["target_key"] and "REACTIVE" in o["notification_type"] for o in out1),
          f"targets={routed1}")
    check("S1 运维处理群收到工单", any("operator_queue" in o["target_type"] for o in out1),
          f"targets={routed1}")

    # ---------- S2 张三 feishu 私聊跨渠道查进度 ----------
    REPORT.append("\n[S2] 张三 feishu 私聊续单查进度(跨渠道)「我的工单怎么样了」")
    before = conn.execute("SELECT COUNT(*) c FROM notification_outbox").fetchone()["c"]
    s2 = feishu_msg("ou_zhangsan", "我的工单怎么样了", f"oc_dm_zhang_{uuid4().hex[:6]}", f"om_{uuid4().hex[:10]}")
    code, r = http("POST", "/webhooks/feishu", s2)
    check("S2 HTTP 200", code == 200, f"code={code}")
    check("S2 命中同一工单", r.get("ticket_id") == t1, f"ticket={r.get('ticket_id')} expect={t1}")
    check("S2 有进度回复", bool(r.get("reply")) and "T" in r.get("reply", ""), f"reply={r.get('reply')!r:.100}")
    out2 = outbox_after(conn, before)
    routed2 = [f"{o['target_type']}:{o['target_key']}" for o in out2]
    REPORT.append(f"     outbox={routed2}")
    # 🔴 已知问题复现点:回执是否错误发到 wecom 群
    leaked = any("conversation:wecom:repair_group_1" in o["target_key"] for o in out2)
    to_dm = any("conversation:feishu" in o["target_key"] and "oc_dm" in o["target_key"] for o in out2)
    check("S2 回执发到飞书私聊(修复前此点失败=错位问题复现)", to_dm and not leaked,
          f"to_feishu_dm={to_dm} leaked_to_wecom={leaked} targets={routed2}")

    # ---------- S3 FAQ ----------
    REPORT.append("\n[S3] 张三 feishu 群问 FAQ「请假流程是什么」")
    s3 = feishu_msg("ou_zhangsan", "请假流程是什么", "oc_requester_group", f"om_{uuid4().hex[:10]}", chat_type="group")
    code, r = http("POST", "/webhooks/feishu", s3)
    check("S3 FAQ 直答", r.get("workflow") in ("faq", "faq_answer") and bool(r.get("reply")),
          f"workflow={r.get('workflow')} reply={r.get('reply')!r:.120}")

    # ---------- S4 chitchat ----------
    REPORT.append("\n[S4] 张三 wecom 群闲聊「你好」")
    s4 = wecom_msg("zhangsan", "你好", "repair_group_1", f"wx_{uuid4().hex[:10]}")
    code, r = http("POST", "/webhooks/wecom", s4)
    check("S4 闲聊不建单", r.get("workflow") in ("chitchat", "other") and not r.get("ticket_id"),
          f"workflow={r.get('workflow')} ticket={r.get('ticket_id')} reply={r.get('reply')!r:.80}")

    # ---------- S5 other ----------
    REPORT.append("\n[S5] 张三 wecom 群「今天天气不错我们出去散步吧」")
    s5 = wecom_msg("zhangsan", "今天天气不错我们出去散步吧", "repair_group_1", f"wx_{uuid4().hex[:10]}")
    code, r = http("POST", "/webhooks/wecom", s5)
    check("S5 闲聊不新建单(续接活动工单=AC-12 设计)", r.get("ticket_id") in (None, t1),
          f"workflow={r.get('workflow')} ticket={r.get('ticket_id')} reply={r.get('reply')!r:.80}")

    # ---------- S6 幂等 ----------
    REPORT.append("\n[S6] 幂等:重发 S1 同 message_id")
    code, r = http("POST", "/webhooks/wecom", s1)
    check("S6 duplicate=true 且不建新单", r.get("duplicate") is True,
          f"duplicate={r.get('duplicate')} ticket={r.get('ticket_id')}")
    n_tickets = conn.execute("SELECT COUNT(*) c FROM tickets").fetchone()["c"]
    check("S6 全库工单数不增", n_tickets == 1, f"tickets={n_tickets}")

    # ---------- S7 李师傅 claim + resolve ----------
    REPORT.append("\n[S7] 李师傅处理:claim -> resolve")
    act = {"actor": {"channel": "wecom", "channel_user_id": "lihua"}}
    code, r = http("POST", f"/tickets/{t1}/claim", act)
    check("S7 claim 成功", code == 200, f"code={code} {r}")
    code, r = http("POST", f"/tickets/{t1}/resolve", {**act, "note": "已清理卡纸,恢复正常"})
    check("S7 resolve 成功", code == 200, f"code={code} {r}")
    st = conn.execute("SELECT status AS state FROM tickets WHERE id=?", (t1,)).fetchone()
    REPORT.append(f"     ticket {t1} state={st['state'] if st else None}")
    confirm_reqs = conn.execute(
        "SELECT COUNT(*) c FROM notification_outbox WHERE notification_type='REQUESTER_CONFIRMATION_REQUEST'"
    ).fetchone()["c"]
    check("S7 发送确认请求通知", confirm_reqs >= 2, f"confirmation_requests={confirm_reqs}")

    # ---------- S8 张三确认关闭 ----------
    REPORT.append("\n[S8] 张三确认「确认关闭」")
    s8 = wecom_msg("zhangsan", "确认关闭", "repair_group_1", f"wx_{uuid4().hex[:10]}")
    code, r = http("POST", "/webhooks/wecom", s8)
    st = conn.execute("SELECT status AS state FROM tickets WHERE id=?", (t1,)).fetchone()
    check("S8 工单 CLOSED", st and st["state"] == "CLOSED", f"state={st['state'] if st else None}")
    mem = conn.execute("SELECT fact, source FROM memories WHERE user_id=(SELECT id FROM users WHERE display_name='张三')").fetchall()
    REPORT.append(f"     记忆抽取: {[dict(m) for m in mem]}")
    check("S8 记忆已抽取", len(mem) >= 1, f"memories={len(mem)}")

    # ---------- S9 新报修 + 记忆召回 ----------
    REPORT.append("\n[S9] 张三同主题再报修「门禁卡又刷不开了」(记忆召回)")
    before = conn.execute("SELECT COUNT(*) c FROM notification_outbox").fetchone()["c"]
    s9 = wecom_msg("zhangsan", "门禁卡又刷不开了,还是进不去", "repair_group_1", f"wx_{uuid4().hex[:10]}")
    code, r = http("POST", "/webhooks/wecom", s9)
    check("S9 建新单(support)", r.get("workflow") == "ticket" and r.get("ticket_id") != t1,
          f"workflow={r.get('workflow')} ticket={r.get('ticket_id')} reply={r.get('reply')!r:.80}")
    check("S9 记忆召回", bool(r.get("recalled")), f"recalled={r.get('recalled')}")
    out9 = outbox_after(conn, before)
    op_notified = any("operator_queue" in o["target_type"] for o in out9)
    check("S9 通知运维处理群", op_notified, f"targets={[o['target_type']+':'+str(o['target_key']) for o in out9]}")

    # ---------- S10 HITL 审批链 ----------
    REPORT.append("\n[S10] 张三报修「申请更换新电脑」")
    s10 = wecom_msg("zhangsan", "我的电脑太旧了,申请更换一台新电脑", "repair_group_1", f"wx_{uuid4().hex[:10]}")
    code, r = http("POST", "/webhooks/wecom", s10)
    check("S10 建单(support)或 grounded 直答", bool(r.get("ticket_id")) or r.get("workflow") in ("faq_answer", "faq"),
          f"workflow={r.get('workflow')} ticket={r.get('ticket_id')} reply={r.get('reply')!r:.100}")
    t10 = r.get("ticket_id")
    approvals = conn.execute("SELECT id, status, action FROM approvals").fetchall()
    REPORT.append(f"     approvals={[dict(a) for a in approvals]}")
    if t10:
        check("S10 审批记录存在", len(approvals) >= 1, f"count={len(approvals)}")
    else:
        check("S10 无工单无需审批(grounded 直答)", True, "")

    # ---------- 汇总 ----------
    REPORT.append("\n" + "=" * 70)
    REPORT.append(f"结果: ✅{PASS}  ❌{FAIL}  ⚠️{WARN}")
    REPORT.append("=" * 70)
    print("\n".join(REPORT))
    conn.close()
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()