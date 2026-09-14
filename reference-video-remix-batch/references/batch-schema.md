# 批次结构、确认与恢复

建立或恢复批次时读取本文件。私有状态 JSON 是唯一真实来源；聊天记录、CSV 和公开报告都不能替代它。

## 目录

- [批量映射](#批量映射)
- [规范状态](#规范状态)
- [素材与授权](#素材与授权)
- [任务和片段](#任务和片段)
- [不可变确认账本](#不可变确认账本)
- [媒体操作图](#媒体操作图)
- [状态机与恢复](#状态机与恢复)
- [导入与公开导出](#导入与公开导出)

## 批量映射

正式批次含 10–50 个 30 秒成片任务。少于 10 个可按普通复刻任务处理；超过 50 个时建立完整总计划，再拆成每批最多 50 个的子批次。每次只确认和启动一个子批次。

支持两种显式模式：

- `one_reference_many_subjects`：一个参考对应多个主体，每个主体生成一条成片。
- `paired`：每行明确参考与主体的对应关系。

`paired` 的单行含多个主体时必须声明：

- `subjects_in_same_output`：多人共同出现在同一条成片，该行计一个任务。
- `expand_per_subject`：每个主体各生成一条成片，该行按主体数展开。

只有一个主体时可默认 `subjects_in_same_output`。存在多个主体但没有映射方式时暂停澄清。不得因为有 M 个参考和 N 个主体就自动形成 M×N；只有用户明确要求“全部组合”等语义时才展开。确认前同时报告成片任务数与底层 Seedance 调用数。

## 规范状态

使用 `schema_version: "1.0"`。最小顶层结构为：

```json
{
  "schema_version": "1.0",
  "batch_id": "rvb-20260907-001",
  "created_at": "2026-09-07T12:00:00+08:00",
  "status": "DRAFT",
  "mode": "one_reference_many_subjects",
  "target_duration_seconds": 30,
  "rights_confirmation": {
    "confirmed": false,
    "scope": "current_batch",
    "confirmed_at": null,
    "assets": []
  },
  "reference_analyses": {},
  "execution_policy": {
    "quality_mode": "quality_first_adaptive",
    "max_jobs_per_wave": "auto",
    "pilot_policy": "risk_based",
    "automatic_technical_retries": 1,
    "automatic_quality_retries": "preconfirmed_only"
  },
  "capacity_snapshot": {},
  "scheduler_state": {},
  "confirmation_bundles": [],
  "media_operations": [],
  "jobs": []
}
```

将文件命名为 `batch-state.private.json`，创建后设为当前用户可读写。每次状态变更先写同目录临时文件、同步，再原子替换。不得把状态写入 Skill 安装目录。

## 素材与授权

`rights_confirmation.assets[]` 的每一项至少记录：

- `asset_id`、`asset_type`、`content_hash`。
- `rights_confirmed`、`confirmation_scope: "current_batch"`。
- `processing_destination`、`upload_required`、`processing_purpose`。
- `retention_status`、`upload_confirmed`。

素材类型至少区分参考视频、肖像、音乐、声音、字体、商标和其他。用户的授权声明是当前工作流的使用前提，不等同于法律权属证明。对额外 Cloud 处理的同意必须与素材使用授权分别记录。

进入生成或本地合成的文件须有内容哈希。仅作网页高层观察的链接可只有规范化 URL 指纹，但不能支撑精确复刻或源音轨复用。

## 任务和片段

每个 `job` 至少包含：

- 稳定唯一的 `job_id`、组合输入的 `input_hash`。
- `reference_id`、主体素材 ID 列表和可读 `subject_label`。
- 固定的 `target_duration_seconds: 30`、`aspect_ratio` 和输出规格。
- `preserve_constraints`、`variation_constraints`、`negative_constraints`。
- `timeline_map`、`segment_plan`、音频与文字策略。
- `segments`、任务 `status`、尝试次数、私有结果和 `qc_result`。

实际调用信息放在 `segment`，即使单次生成整条 30 秒也建立一个 segment。每项至少包含：

- `segment_id`、目标起止时间和对应时间线条目。
- 完整 `prompt_text`、`prompt_hash`。
- 完整 `invocation_spec`、`invocation_hash`。
- 参考素材 ID、`client_submission_id`。
- `confirmation_bundle_id`、`confirmation_revision`。
- `status`、`technical_retry_count`、`quality_revision`。
- 私有下游任务 ID、提交/对账时间、输出路径/哈希和错误。

`prompt_text` 只能包含创作内容。模型、任务类型、时长、比例、素材 ID 与职责、检索补充和分段目标放入 `invocation_spec`。两个对象分别规范序列化并计算 SHA-256；不要把路径或确认说明塞进提示词。

允许的画幅为 `16:9`、`4:3`、`1:1`、`3:4`、`9:16`、`21:9`。每个 Seedance 调用时长必须是 5–30 秒整数；最终任务仍为 30 秒。

## 不可变确认账本

`confirmation_bundles[]` 是只追加账本，每一项至少有：

- `bundle_id` 与递增 `revision`。
- 覆盖的 `job_id/segment_id` 列表。
- 每次调用的 `prompt_hash`、`invocation_hash`。
- 规范集合计算出的 `bundle_hash`。
- `created_at`、`status`、`confirmed_at`。

确认包必须完整展示每个调用的提示词原文、模型、类型、时长、比例、素材职责、检索补充、处理目的地、风险和可选的精确定义重试版本。用户确认的是这个封闭集合，而不是模糊的“接下来都同意”。

每次修订写入新的 `confirmations/<bundle_id>-r<revision>.private.md`。确认后文件改为当前用户只读，旧版本不得覆盖。只修改受影响的调用，未变化 segment 继续引用旧确认版本。

提交前同时验证：

1. segment 当前的两个哈希与其引用条目相同。
2. 引用账本状态为已确认。
3. `bundle_hash` 与磁盘确认包内容匹配。

任一不匹配就回到 `AWAITING_CONFIRMATION`，不能靠聊天中的“确认过”跳过。

## 媒体操作图

每次抽取、裁切、统一编码、拼接、文字叠加、混音、合流、解码和 QA 输入构造分别建立 `media_operations[]` 项：

- `operation_id`、`job_id`、可选 `segment_id`、操作类型。
- `depends_on_operation_ids` 与 `required_for_success`。
- 输入内容哈希、参数哈希、`client_submission_id`。
- `status`、技术重试次数、私有下游任务 ID。
- 输出路径/哈希、提交时间和最近对账时间。

依赖必须构成无环图。只有全部依赖成功后操作才可运行。相同输入哈希和参数哈希的本地确定性输出可复用；未完成的临时文件不是成功证据。

Cloud 媒体操作使用与生成调用相同的提交前落盘、任务查询和对账原则。必需操作失败会使任务失败；可选操作缺失若影响必检项，该项为 `UNKNOWN`，仍不能成功。

## 状态机与恢复

批次主路径：

`DRAFT → VALIDATED → AWAITING_CONFIRMATION → RUNNING → COMPLETED_WITH_SUMMARY`

任务主路径：

`DRAFT → VALIDATED → CONFIRMED → QUEUED → SUBMITTED → GENERATING → COMPOSING → QC_FAST → SUCCEEDED`

片段和异步媒体操作额外使用：

- `SUBMITTING`：客户端提交 ID 已落盘，下游 ID 尚未确认。
- `NEEDS_RECONCILIATION`：提交结果不确定，必须先查询或从可见任务列表对账。
- `AWAITING_REVISION_CONFIRMATION`：质量修复需要新的提示词、参数或素材。
- `FAILED_FINAL`：允许的重试已用尽，不会在恢复时自动入队。

提交动作顺序固定为：生成 `client_submission_id` → 原子写入 `SUBMITTING` → 调用工具 → 记录唯一任务 ID 并转为 `SUBMITTED`。中断发生在中间时先对账，绝不直接重复提交。

恢复时跳过 `SUCCEEDED` 和 `FAILED_FINAL`，校验已有输出哈希；继续未完成片段，再按媒体操作依赖图恢复合成与 QA。应用关闭后不声称后台仍执行，必须由用户再次调用本 Skill 触发恢复。

## 导入与公开导出

JSON/CSV 导入限制：

- 文件最大 5 MiB，JSON 最大嵌套 20 层，自由文本字段最大 64 KiB。
- 每批最多 50 个任务、500 条资产记录。
- ID 只能含小写字母、数字、连字符和下划线，长度 1–64，并在作用域内唯一。
- CSV 列表字段使用 JSON 数组字符串；不要猜分隔符。
- 拒绝绝对输出路径、`..`、控制字符、路径分隔符和解析后越界的符号链接。

公开 `batch-manifest.json` 必须按固定白名单生成，只允许不透明 ID、映射摘要、哈希、状态、相对成片文件名和 QA 摘要。默认禁止公开：

- 完整提示词和私密人物标签。
- 原始绝对路径、下游任务 ID。
- 临时或签名 URL、Cookie、令牌、认证信息。
- 私有错误载荷和未知新字段。

公开汇总不得直接序列化私有状态后“删几个键”。导出后执行敏感字段和绝对路径扫描；发现命中即失败。
