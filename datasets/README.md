# Datasets(原始数据,不入版本库)

> 下载于 2026-08-25,来源 HuggingFace(经 hf-mirror.com)。
> 用途对应《升级计划.md》任务:B2 意图路由 / C2 向量 RAG / C3 指标看板。

## A. customer-support-tickets/(Tobi-Bueck,HF)

| 文件 | 行数 | 说明 |
|---|---|---|
| `aa_dataset-tickets-multi-lang-5-2-50-version.csv` | 28,587 | 主数据集(en+de) |
| `dataset-tickets-multi-lang-4-20k.csv` | 20,000 | 4 语言版 |
| `dataset-tickets-german_normalized_50_5_2.csv` | 13,178 | 德语规范化版 |

字段:`subject, body, answer, type, queue(10类), priority(low/medium/high), language, tag_1..8`
用途:**body→answer 对 → RAG 知识库种子(C2);queue/priority 标签 → 意图/分类评测集(B2);type 分布 → 看板(C3)**。

## B. clinc150/(DeepPavlov 格式,UCI 原版)

| 文件 | 行数 |
|---|---|
| `train/validation/test-*.parquet` | 15,200 / 3,100 / 5,500(`utterance`,`label`) |
| `intents-*.parquet` | 150 个意图名 |

用途:**意图路由评测标准方法**(含 Out-of-Scope 检测)——把"不属于任何已知意图"
变成一等公民,治 other 分支误入。B2 的回归基准。

## C. it-support-tickets/(ameau01 合成,IT 服务台)

745 条 IT 事件记录:`record_id, record_type, ticket, status,
correspondence, diagnostics, root_cause, resolution` + PII 脱敏标注
(`pii.json`/`retention.json`/`users_directory.json`)。

用途:**领域最贴(IT 报修)的 RAG 小样本**;ticket→root_cause/resolution
即天然"问题→诊断→解决"三元组,可做诊断 Agent(§#5)的 few-shot 示例;
PII 字段可演示数据合规。
