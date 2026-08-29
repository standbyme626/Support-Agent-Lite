# support-agent-platform 项目记忆（MEMORY）

> 本文件是项目的**长期记忆**：给 AI 的阅读上下文 + 主人的运维/进度备忘录。有新进展就往里加。
>
> 更新日期：2026-08-29
> 位置：/home/kkk/Project/support-agent-platform
> 状态：真实飞书群运行中（张三报修 / 于松泽处理），V2.2 语义意图路由已落地，**SetFit 已接入生产语义层（INTENT_EMBEDDING=setfit）**，E2E 真人测试 4 缺陷全部修复（24/24 绿）；**2026-08-29 P0 上线落地完成：e7e6106 已部署生产并冒烟通过（3/3），服务以 systemd 常驻 unit 运行**

---

## 目录
- [一、项目定位与架构](#一项目定位与架构)
- [二、意图识别体系（V2.2）](#二意图识别体系v22)
- [三、评测集资产](#三评测集资产)
- [四、RAG 检索](#四rag检索)
- [五、数据集清洗与重建（2026-08-27）](#五数据集清洗与重建2026-08-27)
- [六、bge 本地化实验（2026-08-28）](#六bge-本地化实验2026-08-28)
- [七、E2E 真人测试与修复（2026-08-28）](#七e2e-真人测试与修复2026-08-28)
- [八、已知问题与待办](#八已知问题与待办)
- [九、环境与命令](#九环境与命令)

---

## 一、项目定位与架构

- **Support Agent Lite**：跨渠道企业 IT 支持服务台（飞书/企微 → 意图路由 → FAQ 直答或建单 → 三面协作 → 人工认领 → 确认关闭 → 记忆抽取）。**不是**纯 IT 工作台，是"员工报修+流程咨询+进度查询"的服务台。
- 五意图体系：`support`(报修建单) / `faq`(知识问答→RAG) / `progress_query`(查进度) / `chitchat`(闲聊) / `other`(安全拒答)。
- 核心链路：`workflow.py:_prepare_requester` 按意图分支；只有 faq 走 RAG 检索，其余确定性处理。
- 治理层（Harness Engineering）：两阶段事务、HITL exactly-once、幂等、outbox 重试、崩溃恢复——简历差异化卖点。

## 二、意图识别体系（V2.2）

### 级联路由器 `app/application/semantic_intent_router.py`

```
规则层(IntentRouter 关键词,确定性主路径)
  → 任何关键词命中(conf>=0.58)即赢,不做语义竞争
  → 语义层(SemanticIntentRouter,锚点余弦)只接管"不含关键词的长尾"
  → other 兜底(never force-pick)
```

- **修复记录（2026-08-27）**：① 规则层信号（"怎么样了"0.65）曾被语义层高置信覆盖→已改为规则层优先；② 短文本（<6 字）不走语义层（"处理好了"曾被锚点误判 chitchat）→ `MIN_SEMANTIC_LEN=6` 门控。
- **生产语义层（2026-08-28 起）**：`INTENT_EMBEDDING=setfit`（.env）→ `CascadeIntentRouter` 语义层换 `app/application/setfit_intent.py`（SetFit 微调 bge-small-zh，确定性、无 API 抖动、1.6ms/条）。未启用/模型缺失自动降级 API 锚点。
- **非对称意图阈值（E2E 修复 1B，semantic_intent_router.py）**：support 0.55（宁错建单不漏单）/ progress 0.58 / faq 0.60 / chitchat 0.68。
- **业务保底护栏（E2E 修复 1A/1D，workflow.py + intent_router.py）**：`SUPPORT_GUARD_TERMS`（设备/故障词表）+ `has_support_guard_signal()`；语义层 low/异常降级时命中即保守走 support 建单——错建单有 HITL 兜底，漏单没有。
- 语义层锚点（API 路径备用）：`runtime/intent_anchors/anchors.json` + `vectors.json`（255 条向量，**中英混合**：CLINC150 英文 50 条/意图 + zh_golden 中文 15 条/意图）。

### 评测基线

| 评测集 | 指标 | 数值 |
|---|---|---|
| zh_golden 505 条 | 级联 overall | **78.0%**（GATES 全过，见 `tests/test_intent_eval_zh.py`） |
| office eval_500 | 级联 overall | **78.6%**（报告 `runtime/office_eval_report.md`） |
| **setfit 生产路径**（2026-08-28 修复后） | office / zh | **82.6% / 73.8%**（`train_setfit_intent.py --eval-only`；office support 75%↑、zh support 86.2%） |

## 三、评测集资产

| 数据集 | 路径 | 数量 | 来源 |
|---|---|---|---|
| zh_golden | `datasets/zh_golden/intent_eval_500.jsonl` | 505 | CLINC150 英文 → **MyMemory 免费直译**（`style=translated`，全部） |
| office eval | `datasets/office_golden/eval_500.jsonl` | 500（每桶 100） | 百炼改写/迁移/手写 |
| office train | `datasets/office_golden/train_650.jsonl` | **668**（8-28 重组后） | faq 151/support 120/progress 100/chitchat 150/other 147 |
| diagnosis fewshot | `datasets/zh_golden/diagnosis_fewshot_50.jsonl` | ❌ 未生成 | it-support-tickets 四元组本地化（可后用百炼补） |

- zh_golden 是**跨分布压力测试**（直译生硬+金融词），office 是**业务同分布**测试，两个都要过。
- 评测脚本：`scripts/eval_intent_router.py`（zh，复用缓存向量）、`scripts/eval_office_router.py`（office，`--re-embed` 重算；embedding 缓存 `runtime/eval_cache/`）。
- office eval 嵌入失败会 `ReadTimeout`（SiliconFlow 偶发），重跑即可。

## 四、RAG 检索

- `KB_VECTOR_ENABLED=true`（`.env`），`main.py:_maybe_hybrid` 装配 HybridRetriever：TF-IDF + 向量双路 → RRF(k=60) → Qwen3-Reranker 重排 → top3。
- **E2E 修复 3A（2026-08-28）**：① `candidate_k` 8→**30**（此前"请假流程"双路 top-8 都不含请假文档，rerank 救不回候选集外的文档——实测 rerank 年假 0.62 vs 差旅 0.03）；② **`answer()` 不再透传 keyword-only**，走 `search()` 全管线（此前 FAQ 答复从未经过向量+rerank，是请假错位的真根因）；③ `last_rerank` 可观测（rerank 是否生效/候选数/分数，workflow 写入 `stage=rerank` trace）。
- 向量索引：`runtime/vector_index/`（NumpyVectorStore 零依赖暴力余弦，**540 条** × 4096 维，8-28 重建）。Chroma 备选（`KB_VECTOR_BACKEND=chroma`）未启用，419 条规模没必要。
- embedding/rerank 均走 SiliconFlow API（Qwen3-Embedding-8B / Qwen3-Reranker）。
- 已知边界：E2E 实测 `Qwen3-Embedding` 同一文本两次调用向量有 L2 差 ~0.011（非确定性）——意图锚点层已换 SetFit 规避；检索 embedding 仍受此影响（rerank 兜底）。

## 五、数据集清洗与重建（2026-08-27）

### 补写 120 条流程类 QA 对（治本）
- 新文件 `seed/faq/kb_office_process.json`（doc_id `faq-proc-001~120`，16 类：假期/报销/门禁/账号/会议室/差旅/采购/网络/软件/电话/打印/考勤/设备/行政/安全/IT服务/HR/财务）。
- 知识库 419 → **539** 条，process 形态 84 → 162。标题已避开 TROUBLESHOOT_HINTS 词表（"怎么办/无法"等）。

### 百炼改写全量化
- `build_office_dataset.py`：`rewrite_targets = faq_titles`（原 `[:150]`）→ 539 标题全部改写（0 FAIL，`runtime/office_gen/faq_rewrite.jsonl` 539 行）。
- chitchat/other 生成扩到 **15 变体/kind**（`DIRECT_VARIANTS=15`，18 kinds → 270 原始变体/桶）。

### 清洗规则（关键改动）
- `PROFESSIONAL_HINTS`：技术框架/医疗/投资/数据分析词（Firebase/Kubernetes/macOS/患者/投资/量化…）→ `is_process_faq` 判非流程咨询（**原来 32% eval faq 是技术教程类污染**）。
- `BUSINESS_HINTS`：chitchat/other 桶剔除业务词（"谢谢，顺便问下加班调休怎么算"这类混合意图样本直接丢）。
- 重组后 train faq 污染 **0**、chitchat/other 业务词 **0**、eval/train 重叠 0。

### 清洗前后对比
| 项 | 清洗前 | 清洗后 |
|---|---|---|
| train faq 污染 | 53/212 | 0/151 |
| eval faq 污染 | 32/100 | 2/100（边缘残留"数字策略工具挂了"） |
| office eval overall | 79.0%（脏） | **78.6%（干净基线）** |

## 六、bge 本地化实验（2026-08-28）

**目标**：意图识别本地化（毫秒级、零 API 依赖、离线演示），替换 SiliconFlow API embedding 层。

### 实验结果链

| 方案 | office eval | zh_golden | 结论 |
|---|---|---|---|
| API 基线（Qwen3 锚点） | 78.6% | 78.0% | 当前生产 |
| 冻结 bge-small-zh + 原锚点（中英混合） | 60.0% | 54.1% | ❌ 英文锚点对中文模型失效 |
| 冻结 bge + 纯中文锚点 | 72.6%（thr=0.55） | 56.8% | 速度 100 倍但精度不够 |
| **SetFit 微调 v1**（503 条，epoch 8/16） | 78.0% | 51.3% | 过拟合，其他桶被 faq 绑架 |
| **SetFit 微调 v2（当前最优）** | **81.8%** | **73.8%** | ✅ 超 API；zh 排除 100 条训练样本后评测 |

### v2 关键调优（全部生效）
1. chitchat/other 训练数据翻倍（67→150 / 65→147）
2. epoch (8,16)→(4,8) 减过拟合
3. 掺入 zh_golden 每桶 20 条（共 100）作分布增强，训练样本 768 条；评测 zh 时排除（`runtime/setfit-intent/zh_train_ids.json`）

### 产物与命令
- 模型：`runtime/setfit-intent/`（SetFit 微调后 bge-small-zh-v1.5）
- 训练脚本：`scripts/train_setfit_intent.py`（`--eval-only` 只评测）
- 本地编码脚本：`scripts/eval_local_bge.py`（原锚点对比）、`scripts/eval_local_bge_zh.py`（纯中文锚点+阈值扫描）
- 推理：**500 条 0.8s（1.6ms/条）** vs API 分钟级

### 踩坑记录（重要）
- setfit 1.1.3 要求 **transformers <5**（`default_logdir` 被移除）→ 降级 4.57.6
- setfit 1.1.3 的 `SetFitTrainer` **不再收 `args`**，超参直接传（`num_epochs=(4,8)` 元组=body/head）
- `SetFitTrainer` 不收 `max_length` / `l2_weight` 等参数（1.1.3 签名收 `num_iterations/num_epochs/learning_rate/batch_size`）
- 评测脚本 bug：`pred_labels = [LABELS[i]...]` 应为 `LABELS[p]`（已修）
- 模型下载需 `HF_ENDPOINT=https://hf-mirror.com`；pip 用阿里云源（`-i https://mirrors.aliyun.com/pypi/simple/`）
- CPU 训练 768 条 ≈ 43 分钟（`torch.set_num_threads(8)`）

### 剩余短板
- office support 71%（"申请更换设备"类被 faq 抢 21 条）→ 百炼迁移池还有 168 条 support 变体未用，可扩训练（修复后 75%，"申请更换电脑"仍走 grounded faq 直答——已接受的边界）
- zh other 55.6%（OOS 拒答弱）→ 生产已加 `SETFIT_PROB_THRESHOLD`（默认 0.35）低置信→other，数据侧仍可扩

## 七、E2E 真人测试与修复（2026-08-28）

**方法**（可复用）：`scripts/e2e_seed.py`（隔离 DB 种子，不碰生产库）+ `scripts/e2e_human_sim.py`（真实 HTTP webhook 模拟 张三/李师傅/王经理 三真人，断言回复/建单/outbox 路由/状态机/记忆）。跑法：seed → 起 uvicorn（`SUPPORT_AGENT_DB=runtime/e2e_live.db`）→ 跑 sim → 查 `notification_outbox` 判定回执去向。

**发现 4 个使用级缺陷 → 全部修复（修复后 E2E 24/24 绿，全量 pytest 377 绿 + setfit 用例）**：

| # | 缺陷 | 根因（实测） | 修复 |
|---|---|---|---|
| 1 | 🔴 报修漏单（"打印机卡纸"偶发不建单） | Qwen3-Embedding 非确定性（L2 差 0.011），锚点分 0.58~0.65 卡阈值 0.62；embedding 异常时走 LLM 闲聊裁断 | 1A 业务保底护栏（`SUPPORT_GUARD_TERMS`，语义低分/降级时保守建单）；1B 非对称阈值（support 0.55）；1C 根治=SetFit 接入生产 |
| 2 | 🔴 查进度静默失败（用户收不到任何回复） | `_deterministic`（progress/clarify/确认路径）只写 messages+trace，从不投递 outbox | `_deterministic` 补 `conversation_reply`（与 agent 路径同款） |
| 3 | 🟡 FAQ 错位（请假流程→差旅报销） | `HybridRetriever.answer()` 透传 keyword-only，向量+rerank 从未生效；且候选集只有 8 条 | `answer()` 走全管线 + `candidate_k` 8→30 + rerank 入 trace；KB 补 `faq-proc-121 请假流程是什么` |
| 4 | 🟡 续单/催办漏判（"还没修好吗"→闲聊） | progress/support 规则词不全 | 规则词补充（还没修好/催一下/卡纸/用不了…） |

**修复后关键行为（E2E 实测）**：飞书私聊查进度回执**正确落在飞书私聊**（修复前此点=生产🔴错位问题，已由 e2e_human_sim.py S2 断言守护）；同主题再报修记忆召回命中；确定性回复（查进度/clarify）全部投递。

## 八、已知问题与待办

### 🟡 待办（按优先级）
1. **概率拒答数据侧**：zh other 55.6%（生产已有 prob 阈值，提升需扩 OOS 训练数据）
2. **support 扩训**：迁移池剩余 168 条 support 变体重训（office support 75%→更高）
3. **diagnosis_fewshot_50** 生成（it-support-tickets 四元组，百炼一次调用 50 条）
4. **性能**：agent 轮次 20~122s（free 模型）——换非 free 快模型或重提 C8（Ollama）
5. C4 编排（owner 暂停）、C8 Ollama（owner 取消）——维持原状
6. E2E 剧本可扩展：approval 审批通过链路（S10 当前为 grounded faq 直答，未触发审批）

### 🟢 运行注意事项
- 后台长任务用 `systemd-run --unit=xxx --working-directory=<项目目录> --setenv=HOME=/root .venv/bin/python xxx`，**不要 nohup+&**（会被 shell 回收）
- 百炼 gen/filter 可断点续跑（key 去重）；embedding API 偶发 ReadTimeout 重跑即可
- 飞书 ws bridge 进程：`.venv/bin/python scripts/feishu_ws_bridge.py`（PID 常驻）；转发 15s 超时会导致飞书重试 → duplicate 日志（idempotency 已兜住，属正常噪音）
- **SetFit 首载 ~2 分钟**（CPU）：生产进程启动时会打 `[intent] semantic layer: setfit (local)`；测试默认 `INTENT_EMBEDDING=api`（conftest 强制，防慢加载）

## 九、环境与命令

| 项 | 值 |
|---|---|
| Python | 项目 `.venv`（python3.12） |
| 测试 | `.venv/bin/python -m pytest tests/ -q -p no:cacheprovider --ignore=tests/test_agent_eval.py --ignore=tests/test_intent_eval_zh.py`（embedding 慢测试单独跑） |
| 百炼改写 | `.venv/bin/python scripts/build_office_dataset.py --stage gen\|filter` |
| 评测 | `.venv/bin/python scripts/eval_office_router.py [--re-embed]` / `eval_intent_router.py [--cached]` / `train_setfit_intent.py --eval-only` |
| SetFit | `.venv/bin/python scripts/train_setfit_intent.py [--eval-only]` |
| E2E 真人 | `.venv/bin/python scripts/e2e_seed.py runtime/e2e_live.db` → 起 uvicorn（`--setenv=INTENT_EMBEDDING=setfit`）→ `.venv/bin/python scripts/e2e_human_sim.py runtime/e2e_live.db` |
| 向量索引重建 | `.venv/bin/python scripts/build_vector_index.py`（KB 变更后必须重跑） |
| 数据库 | `runtime/support_agent.db`（conversations/notification_outbox/sessions/trace_events…） |
| 密钥 | 均在 `.env`（600 权限），勿外泄；百炼/SiliconFlow/OpenRouter 三套 |

### 已装的本地模型依赖（.venv）
- sentence-transformers 6.0.0 + torch 2.13.0（CPU）+ transformers **4.57.6**（勿升 5.x，setfit 不兼容）
- setfit 1.1.3

---
---

## 十、P0 上线落地与生产冒烟（2026-08-29）

### 服务常驻恢复（关键教训）
- **发现生产 API 自 8/28 晚起就没在跑**：ws_bridge（8/25 启动的 session 遗留进程）一直往 `127.0.0.1:8322` 死端口转发，最后一条真实用户消息停在 8/26 20:50。"生产跑旧代码"的真实版本是"生产根本没在跑"。
- 现两个进程均为 systemd transient unit（`systemctl status` 可查）：
  - `support-agent-api.service`：`.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8322`
  - `support-agent-feishu-bridge.service`：`.venv/bin/python scripts/feishu_ws_bridge.py`
  - ⚠️ transient unit **重启后即失**，需落正式 `.service` 文件（WantedBy=multi-user.target + Restart=on-failure）
- 部署即代码：本机=生产机，git push（origin 同步至 e7e6106）+ 重启进程即部署。向量索引 8/28 已重建（540=KB 540），无需重跑。

### 生产冒烟（3 条模拟 webhook，全过）
1. 张三上报群报修 → 命中**遗留单歧义 clarify**（张三名下 4 个 OPEN：T0003/T0004/T0005"/help"/T0010）未建新单；clarify 确定性回复正确投递上报群（修复 2 生产验证）
2. 于松泽上报群报修「B2 打印机卡纸」→ **T0013 建单 + 三面投递全对**：回执→上报群 oc_979f、详情→DM(open_id)、运维单→处理群 oc_54cd，全部 SENT_FEISHU，12.8s
3. 张三 DM 查「T0003 处理得怎么样了」→ progress 确定性回复**落在 DM**（修复 2/S2 生产验证），33.4s

### 群聊错位定案
- 仅 **8/13 V1 时代回放数据**存在互换（T0001 首轮：REACTIVE_REPLY→处理群、OPERATOR_WORK_ITEM→上报群）；8/24 起 40+ 条 outbox **零错位**；现行 `TargetResolver.requester_public` 按 purpose+queue+channel 解析，无互换路径。
- T0005「/help 建单」为旧代码缺陷，现行 `_chitchat_reply` 精确拦截（workflow.py:95），已不会复现。

### 遗留（需 owner 决定）
- 张三 4 个遗留 OPEN 工单会持续触发多单 clarify 挡新报修：T0005"/help"、T0010"你可以做什么"是纯噪音建议清掉；T0003（空调）/T0004（电脑）是 8/24-25 测试期旧单，确认后可走 force_close+审批链清理。
- workflow 懒构建：SetFit 在首个 webhook 请求才加载（冷缓存 ~2min/热 ~5s），建议 `create_app` 加 startup 预热钩子。
- 性能实测：agent 建单轮 12.8s（远好于记录的 20~122s，可能网络/模型波动），确定性轮 <1s。

### 2026-08-29 晚：六项修复全部落地（全量 384 绿，生产已切换新代码）
1. **测试封闭性破洞（重要发现）**：conftest 强制 `INTENT_EMBEDDING=api` 但没屏蔽 key → .env 的 SILICONFLOW_API_KEY 泄入测试 → 语义层真实调外网 API → `test_llm_flagged_request_still_creates_ticket` 随机红（1B 把 support 阈值 0.62→0.55，"查考勤补卡"锚点分落在两阈值之间+embedding 非确定性）。修复：conftest 置空 `SILICONFLOW_API_KEY`；1B 边界问题（非 IT 文本建单）并入 P1 扩训处理。
2. **startup 预热**：`create_app` 加 startup 钩子，SetFit/KB/意图层在端口绑定前加载完（日志 `[startup] runtime prebuilt`），首条消息不再背冷启动。
3. **进程看护**：正式 unit `deploy/*.service`（Restart=on-failure+开机自启）已安装并 enable --now（替代 transient unit）；`scripts/ops/health_probe.sh` cron 每分钟探活+自动重启（告警落 `runtime/ops/health_alerts.log`）；`scripts/ops/backup_db.sh` cron 每日 3:30 在线备份到 `runtime/backups/`（留 7 天）；.env 已 chmod 600。
4. **dispatch 后台化**：webhook/REST 不再被飞书出站 HTTP 阻塞（`IngressService(auto_dispatch=False)` + FastAPI BackgroundTasks）；`DispatchWorker`（`SUPPORT_AGENT_DISPATCH_WORKER=1`，写在 unit 的 Environment=，不进 .env 防泄入测试）每 30s 扫 outbox 重试，失败投递不再依赖下一条消息捎带。`test_outbox_survives_delivery_failure` 契约更新：断言 attempt 历史记录失败 + 后台重试治愈。
5. **no-LLM other 降级改澄清**：`_prepare_other` 不再为无法分类文本造 handoff 工单（T0005"/help"垃圾单根源），改澄清式回复；`test_workflow.py::test_other_intent_real_handoff` 契约同步更新为 `test_other_intent_clarifies_without_ticket`。
6. **确认/驳回词收紧**：CONFIRM 移除"好了/可以了/修好了"（子串误中"处理好了吗/修好了吗"进度问句会误关单），REJECT 移除"不好"（误中"心情不好"）；新增 `tests/test_confirmation_guard.py` 4 例。
