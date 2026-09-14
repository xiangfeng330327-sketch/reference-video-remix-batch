# 随包脚本调用手册

需要创建确认包、维护状态、探测媒体能力或验收音频时读取本文件。所有命令均离线；它们不会调用 Seedance、上传素材、抓取网页或执行媒体合成。

## 目录

- [通用约定](#通用约定)
- [校验批次](#校验批次)
- [构建和登记确认包](#构建和登记确认包)
- [规划波次与两阶段提交](#规划波次与两阶段提交)
- [查询与媒体操作](#查询与媒体操作)
- [记录最终质检](#记录最终质检)
- [恢复与公开导出](#恢复与公开导出)
- [媒体能力与音频验证](#媒体能力与音频验证)
- [离线自测](#离线自测)
- [退出码与错误处理](#退出码与错误处理)

## 通用约定

- 可从 Skill 根目录调用脚本，先对目标脚本运行 `--help`；也可以使用脚本绝对路径从其他目录调用。
- 私有批次文件、确认收据、QC 和能力报告使用权限 `0600`；确认完成的 Markdown 包由状态脚本改为 `0400`。
- 清单、记录和报告文件的父目录必须已经存在。
- 以下 `/path/to/private-batch-work` 是必须由使用者替换的绝对路径占位符，且必须位于 Skill 安装目录之外。若路径仍指向 Skill 目录，停止执行并改用外部私有工作目录。
- 命令返回 JSON。先检查退出码与 `ok/status`，不能只看是否输出了一行文本。

## 校验批次

完整批量校验：

```bash
python3 scripts/validate_batch.py /path/to/private-batch-work/batch-state.private.json \
  --format json \
  --allowed-input-root /path/to/private-batch-work \
  --normalized-out /path/to/private-batch-work/validated.private.json \
  --require-batch \
  --json
```

常用参数：

| 参数 | 必填 | 说明 |
|---|---|---|
| `manifest` | 是 | JSON 或 CSV 清单 |
| `--format` | 否 | `auto`、`json` 或 `csv` |
| `--allowed-input-root` | 有本地素材时 | 可重复；符号链接解析后仍须位于其中 |
| `--normalized-out` | 否 | 原子写入规范 JSON |
| `--require-batch` | 正式 10–50 条时 | 少于 10 条按错误而非普通任务处理 |
| `--allow-missing-inputs` | 仅纯计划 | 不要求文件已经存在；不得用于真实提交门 |

51 条或更多返回 `needs_split` 与完整子批次方案，而不是静默截断。不要把 `--allow-missing-inputs` 的通过结果当成素材已验证。

## 构建和登记确认包

建立新修订：

```bash
python3 scripts/build_confirmation_bundle.py build \
  --state /path/to/private-batch-work/batch-state.private.json \
  --output-dir /path/to/private-batch-work/confirmations \
  --bundle-id rvb-bundle-001 \
  --json
```

把返回的完整 JSON 作为私有 `confirmation-receipt.private.json` 保存，不要粘贴到公开报告。用户确认 Markdown 中的完整集合后登记：

```bash
python3 scripts/batch_state.py record-confirmation \
  /path/to/private-batch-work/batch-state.private.json \
  --bundle-json /path/to/private-batch-work/confirmation-receipt.private.json \
  --confirmed-at 2026-09-07T12:00:00+08:00 \
  --json
```

独立校验确认文件：

```bash
python3 scripts/build_confirmation_bundle.py verify \
  --bundle /path/to/private-batch-work/confirmations/rvb-bundle-001-r1.private.md \
  --expected-file-sha256 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  --expected-collection-sha256 fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210 \
  --json
```

示例哈希只是格式示例。实际值必须来自 build 返回结果。登记时会从当前状态重新渲染确认文件并逐字节比对；只改收据中的哈希不能替换用户确认过的提示词。

## 规划波次与两阶段提交

API 容量明确时规划一波：

```bash
python3 scripts/batch_state.py plan-wave \
  /path/to/private-batch-work/batch-state.private.json \
  --adapter api \
  --reported-capacity 8 \
  --json
```

UI 路径使用 `--adapter ui`，调度器从 1 开始并只按验证结果升到 2。`--limit` 可进一步限制当前波次，但不能扩大安全上限。上一波出现限流时提供 `--previous-wave-outcome limit` 和 Tool 返回的 `--retry-after-seconds`。

调用真实 Tool 前先落盘：

```bash
python3 scripts/batch_state.py record-submit \
  /path/to/private-batch-work/batch-state.private.json \
  --job-id job-001 \
  --segment-id segment-001 \
  --client-submission-id rvb-client-001 \
  --status SUBMITTING \
  --json
```

Tool 明确接受并返回任务 ID 后再登记 `SUBMITTED`：

```bash
python3 scripts/batch_state.py record-submit \
  /path/to/private-batch-work/batch-state.private.json \
  --job-id job-001 \
  --segment-id segment-001 \
  --client-submission-id rvb-client-001 \
  --status SUBMITTED \
  --tool-task-id tool-task-returned-by-provider \
  --json
```

若接受结果不确定，记录 `NEEDS_RECONCILIATION`，不要再次提交。同哈希技术重试只允许一次；`--increment-retry` 只用于符合规则的重试，质量版本还须已有确认或带用户授权修订。

## 查询与媒体操作

记录 Tool 观察到的状态：

```bash
python3 scripts/batch_state.py record-query \
  /path/to/private-batch-work/batch-state.private.json \
  --job-id job-001 \
  --segment-id segment-001 \
  --status GENERATING \
  --tool-task-id tool-task-returned-by-provider \
  --json
```

记录成功时必须同时给出批次目录内的真实文件与 SHA-256：

```bash
python3 scripts/batch_state.py record-query \
  /path/to/private-batch-work/batch-state.private.json \
  --job-id job-001 \
  --segment-id segment-001 \
  --status SUCCEEDED \
  --tool-task-id tool-task-returned-by-provider \
  --output-path /path/to/private-batch-work/segments/job-001-segment-001.mp4 \
  --output-hash 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  --json
```

媒体操作使用独立私有 JSON 传入：

```bash
python3 scripts/batch_state.py record-media-operation \
  /path/to/private-batch-work/batch-state.private.json \
  --record-json /path/to/private-batch-work/operations/final-mux-job-001.private.json \
  --json
```

新操作记录须含操作 ID、任务 ID、类型、依赖 ID 数组、是否必需、输入哈希、参数哈希、执行种类和状态。异步 Cloud 操作还须有客户端提交 ID；成功时须有真实输出路径/哈希。依赖未完成、跨任务或成环时拒绝。

## 记录最终质检

只有任务已经进入 `QC_FAST` 或 `QC_DEEP`、所有片段与必需媒体操作成功后才能执行：

```bash
python3 scripts/batch_state.py record-qc \
  /path/to/private-batch-work/batch-state.private.json \
  --job-id job-001 \
  --qc-json /path/to/private-batch-work/qc/job-001.private.json \
  --output-path /path/to/private-batch-work/final/job-001.mp4 \
  --output-hash 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  --json
```

QC 文件须为私有权限，`overall_result` 为 `PASS`，并包含下列全部 `PASS` 检查：

- `media_integrity`
- `duration`
- `audio_timeline`
- `visual_integrity`
- `identity`
- `text_stability`
- `rights`

每项含非空 `method`、0–1 的 `coverage` 与 `confidence`。除身份检查按严格替换最低 0.95 覆盖外，其余必检覆盖为 1.0。任一 `FAIL` 或 `UNKNOWN` 都拒绝成功；最终文件必须与一个已验证的必需媒体操作结果一致。

## 恢复与公开导出

读取下一步，不修改状态：

```bash
python3 scripts/batch_state.py next-actions /path/to/private-batch-work/batch-state.private.json --json
python3 scripts/batch_state.py resume-summary /path/to/private-batch-work/batch-state.private.json --json
```

导出白名单清单：

```bash
python3 scripts/batch_state.py export-public \
  /path/to/private-batch-work/batch-state.private.json \
  --output /path/to/private-batch-work/public/batch-manifest.json \
  --json
```

公开 QC 只保留固定检查 ID、三态结果和数值比例；不公开 method 或错误文本，只用 `has_error` 布尔值。未知新字段、完整提示词、绝对路径、任务 ID 和临时 URL不会透传。

## 媒体能力与音频验证

本地能力和额外 Cloud 上传门探测：

```bash
python3 scripts/media_adapter.py \
  --output /path/to/private-batch-work/media-capabilities.json \
  --manual-visual-check
```

`MediaKit Cloud` 只是可选适配器示例，不随本 Skill 提供，也未在本地自测中验证。只有当前环境已另行完成适配、且用户已对该目的地单独确认时，才增加 `--cloud-provider mediakit_cloud`、`--cloud-confirmation` 与每个 `--cloud-asset-id`。脚本只返回是否允许，绝不上传。

连续音轨验证：

```bash
python3 scripts/verify_audio_timeline.py \
  --expected /path/to/private-batch-work/audio/expected-master.wav \
  --actual /path/to/private-batch-work/audio/final-decoded.wav \
  --timeline-map /path/to/private-batch-work/timeline-map.json \
  --fps 30 \
  --output /path/to/private-batch-work/qc/audio-report.private.json
```

只有确认包明确允许下混时使用 `--allow-downmix`。音频脚本以 `0/1/2` 分别表示 `PASS/FAIL/UNKNOWN`。

## 离线自测

运行全部测试：

```bash
python3 scripts/self_test.py --json
```

只运行指定组：

```bash
python3 scripts/self_test.py --only confirmation_integrity --only batch_state_contract --json
```

自测只使用临时目录、假提交器和合成 WAV，不联网、不上传、不生成真实视频。它验证实现契约，不能替代真实平台外验。

## 退出码与错误处理

- `0`：请求的本地操作成功；音频为 `PASS`。
- `1`：完整性或媒体质量检查得到确定 `FAIL`。
- `2`：输入、状态、权限或证据不足；音频为 `UNKNOWN`。
- `3`：部分脚本用于可规划但不可直接执行的结果，例如 51 条需要拆批或 Cloud 门被拒绝。

收到非零退出码时读取结构化 `error.code`、`errors` 或 `reasons`。不得通过改状态文件、放宽 QA 阈值、删除确认哈希或重复提交来绕过。
