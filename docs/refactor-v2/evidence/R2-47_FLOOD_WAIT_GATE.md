# R2-47：Telegram FloodWait 全局发布闸门

日期：2026-09-29（UTC）
来源：对比 `qianlong520/Telegram_MistRelay` 内嵌的 `WebStreamer/bot/plugins/stream_modules/flood_control.py`（源自 TG-FileStreamBot）——它用"限流开始 → 暂停整队列 → 通知 owner"的方式处理 FloodWait。
范围：Bot。Player 在本次切换中未重建（见 §5）。

## 1. 发布身份

| 项 | 值 |
|---|---|
| Release id | `r2-47-31f0b03-20260928T234506Z` |
| Full Git commit | `31f0b03439223c7f8ee036d16216a04e72ac465d` |
| Runtime image | `sha256:4bd56da3a62b6101c4167b5d97800f3240c0335aaae82f9e83f05d4ec90edc8f` |
| Source manifest | `f5514291e66a0316732ddd056e2bd505a432b24d7154549cae1539e07e05c411`（宿主/容器/发布清单三方一致） |
| Bot 容器 | `efa5a50c9661440ede35a9d9f8e61a3e579215487642efb0e699639c88597154`，起于 `2026-09-28T23:46:57Z` |
| 测试门禁 | 发布镜像内 **919 tests** |
| 迁移 | **none**（`user_version 18 → 18`） |
| 证据 | `/root/TGVIO-releases/r2-47-31f0b03-20260928T234506Z/evidence/{release-manifest.json,deployment-ledger.json}` |

## 2. 问题与设计

问题：TGVIO 只有**手动**的 `set_queue_paused`，`FloodWaitError` 仅出现在来源读取器与撤销错误分类里。发布/上传路径遇到 FloodWait 时没有全局闸门 —— 硬着头皮继续会烧掉剩余配额，把一次延迟变成一批失败任务；而等 owner 醒来处理又太慢。

设计（**零 schema 变更**）：

- `src/tgvio/application/flood_wait.py`
  - `parse_flood_wait_seconds(exc)`：识别 Telethon 的 `FloodWaitError.seconds` / `.value`，以及被包装后的文本形式 `FLOOD_WAIT_42` 与 `A wait of 42 seconds is required`；只有标记没有数字时取默认 300 s，上限 24 h。
  - `FloodWaitGate.arm(exc)`：把 durable 队列暂停设为 `paused=1, pause_reason="flood_wait:<until_epoch>"`。**绝不缩短**已有窗口（已有更长窗口时返回生效值）；非 flood 异常一律不动。
  - `FloodWaitGate.tick()`：只在 `pause_reason` 以 `flood_wait:` 开头且已过期时恢复 —— **owner 的手动暂停不会被碰**。
- 挂钩点
  - `PublishExecutionEngine` 的发布失败分类单点 → `arm()`（这是配额消耗的主路径）。
  - `JobDownloader` 的单文件失败重试处 → `arm()`，避免"下载刚被限流、下一毫秒又开始上传"。
  - `AutoRecoveryRuntime._run()`（既有 2 s 轮询）→ `tick()`，无需新任务。
- 可观测性：`classify_download_error` 现在把限流识别为独立的 `telegram_flood_wait`，失败中心会显示"Telegram 要求等待（限流）；发布队列已自动暂停，恢复后继续"而不是笼统的下载失败；`queue_controls.pause_reason` 让 `/diag`、Dashboard、metrics 的既有 `paused` 信号带上原因。
- **架构约束**：application 层不得 import Telethon（仓库 AST 边界门禁强制），因此识别完全基于异常类型名与文本。

## 3. 测试

- 新增 `tests/test_flood_wait.py`（10 项）：解析（typed / 包装文本 / 仅标记 / 上限钳制 / 无关错误为 None）、闸门状态机（arm→暂停、tick 到期前保持、到期恢复、重复等待不缩短、更长等待延长、手动暂停不被清除、无关错误不暂停）、下载错误分类。
- 测试过程中抓到一个真实 bug：已有更长窗口时 `arm` 返回了**新的较短值**而非生效值（会误导调用方与日志），已修正为返回生效窗口。
- 全量离线 **919 tests OK**（原 909）。`release_guard verify-tree`（482 文件）/`architecture`（185 文件）通过；`git diff --check` clean。

## 4. 部署与后验

- 走仓库唯一 fail-closed 入口：`scripts/deploy_hostdzire.py --phase R2-47`（migration 默认 none）。该入口完成 test/runtime 双镜像构建、离线 919 测试、source archive + manifest、传输、远端 preflight、三重回滚点、单实例切换与后验。
- 独立复核（本次）：
  - 容器 `running healthy`、restarts 0、单实例；`APP_COMMIT` = `31f0b03…` ✅
  - **容器内探针**：`python -c "from tgvio.application.flood_wait import FloodWaitGate, parse_flood_wait_seconds …"` → `gate_ok 33 FloodWaitGate`（证明新代码确实在运行时镜像里，且解析正确）
  - 容器内 `src/**/*.py` manifest == 发布清单 manifest ✅
  - DB：`quick_check=ok`、`user_version=18`、ledger `1..18`、`queue_controls=(paused=0, reason=NULL, revision=0)`、`jobs: 54 succeeded / 1 failed`、**非终态任务 0**
  - 启动日志：`runtime.lease.acquired`（generation 82）、`runtime.telegram.ready`（`recovery_jobs: 0`）；0 traceback / 0 ERROR / 0 CRITICAL / 0 unhandled
  - 发布清单：`status=deployed`、`tests=919`、`migration 18→18`

## 5. Player 隔离验证

本次切换**没有**触碰 Player：其容器 id 仍为 `e4200d626c0dc8fc28576baa96153efde0f6a4ab739f0590bef5d0017a6524b6`、启动时间仍为 `2026-09-28T23:33:20Z`、`running healthy`、restarts 0。

## 6. 残留与未验证

- **闸门没有被真实 FloodWait 触发过**（它只在 Telegram 真的限流时才动作）。已由 10 项单元测试覆盖状态机与解析，并由容器内探针证明代码上线；真实触发条件不可人为制造。
- 闸门只暂停**新任务的 claim**（scheduler 既有语义）；已经在飞的可见发布不会被中断，这是刻意的（中断可见发送会造成未知副作用）。
- 通知面：本次只写结构化日志 + 复用既有 `paused` 信号；若希望 owner 收到一条私聊告警（`job.failed` 那样的 alert），需要另接 owner 告警通道，可作后续。
