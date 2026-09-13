# 目标技术架构

## 1. 架构形态

保留 Python 3.11、asyncio、Telethon/MTProto、SQLite、FFmpeg/ffprobe、yt-dlp 和标准库 WebDAV。运行时仍优先是一个进程、一个 Bot client；通过清晰的 port 和 durable lease 保留未来拆 worker 的可能，但不提前引入分布式组件。

```text
Telegram / URL / Bot UI
          │
          ▼
 interface adapters ── command/query DTO
          │
          ▼
 application use-cases ── durable scheduler ── planners
          │                         │
          ▼                         ▼
 domain model                 publish/archive plans
          │                         │
          └──────── ports ──────────┘
                    │
       ┌────────────┼───────────────┐
       ▼            ▼               ▼
 SQLite repos   Telegram I/O   Files/FFmpeg/WebDAV
```

核心变化不是增加目录，而是强制以下控制模型：

- command 改状态，query 只读投影。
- Job、PublishPlan、ArchivePlan 都在外部副作用前落库。
- worker 从数据库 claim，不从 handler 私有 dict 领取任务。
- UI 只渲染 view model，不决定发布策略。
- transport 只执行一个明确 step，不推断下一个业务动作。

## 2. 目标目录

```text
src/tgvio/
  domain/
    jobs.py                 # Job/MediaItem/状态与不变量
    collections.py          # collection session/inbox 纯模型
    publication.py          # PublishPlan/Step/Effect/Receipt
    archive.py              # ArchivePackage/Object/Event
    controls.py             # pause/cancel/retry/operation token
    policies.py             # spoiler/caption/固定 destination/Archive 策略快照
    errors.py               # 稳定错误码与安全摘要

  application/
    commands/
      intake.py             # accept/update dedupe/collection finalize
      job_control.py        # pause/resume/cancel/retry/undo request
      publication.py        # plan、claim、execute、reconcile
      archive.py            # plan、claim、execute、retry/delete
      settings.py           # runtime preferences/单 Archive profile/静态代理状态
    queries/
      jobs.py               # page/detail/failure center
      health.py
      statistics.py
      diagnostics.py
    planning/
      publish_planner.py
      archive_planner.py
    scheduling/
      download_dispatcher.py
      publish_dispatcher.py
      archive_dispatcher.py
    ports/
      repositories.py
      telegram.py
      media.py
      archive.py
      clock.py

  infrastructure/
    sqlite/
      connection.py         # lifecycle、transaction、backup、自检
      migrations.py         # checksum ledger、forward-only runner
      repositories/
        jobs.py
        publication.py
        archive.py
        controls.py
        preferences.py
        operations.py
        observability.py
      migrations/NNNN_*.sql
    filesystem/
      cache.py
      hashing.py
      paths.py
    media/
      inspector.py
      transformer.py
      splitter.py
    webdav/
      client.py
      verifier.py

  adapters/
    telegram/
      gateway.py
      intake.py
      download.py
      publish/
        transport.py
        uploads.py
        albums.py
        discussion.py
        large_files.py
        references.py
      ui/
        router.py
        commands.py
        callbacks.py
        presenters.py
        views/
    url/
      downloader.py
      security.py
    web/
      dashboard.py           # 后置，只读 query adapter

  runtime/
    container.py             # 对象装配
    supervisor.py            # start/stop/readiness/signal
    singleton.py             # process lease
  config.py                  # 仅应用运行配置
  main.py                    # 解析参数后调用 supervisor
```

迁移时允许原文件暂时 re-export；但兼容层只能委托新实现，不能继续保存第二份业务状态。

### 2.1 已实现布局（2026-09-13）

上面的树是目标方向；当前生产源码的实际热点边界如下（`MAX_SOURCE_FILE_LINES=1000` 由架构门禁强制）：

- `adapters/telegram/`：`bot_ui.py` 只保留组装与事件分派，表现为 `bot_ui_format.py`、动作在 `bot_ui_jobs.py` + `bot_ui_job_actions.py`，Archive 在 `bot_ui_archive.py`，fixture 在 `bot_ui_fixture.py`，共享常量在 `bot_ui_support.py`；intake 为 `intake_runtime.py` + `intake_status.py` + `intake_collection.py`；publish 为 `publish_transport.py` + `publish_albums.py` + `publish_references.py` + `publish_large_files.py`，上传在 `uploads.py`，讨论区在 `discussion_resolver.py`。
- `adapters/`：`webdav_archive.py` 为高层操作、`webdav_client.py` 为 HTTP/DAV 与路径策略、`webdav_archive_support.py` 共享异常；`web/dashboard.py` 为 loopback-only 只读 HTTP 适配器。
- `application/`：`archive_executor.py` + `archive_commit.py` + `archive_probe.py`；只读运维面为 `dashboard.py`、`metrics.py`、`notifications.py`；成功/终止事件通过 `domain/notifications.py` 白名单进入 outbox。
- `infrastructure/`：`sqlite_*` 按域拆分（新增 `sqlite_notifications.py`、`sqlite_archive_deletion.py`），`sqlite.py` 只做 mixin 组装。
- `application/commands` 与 `application/queries` 未做物理目录重排：command 由 application service 承担，query 由 `application/ports.py` 的只读方法 + repository 读模型承担。

## 3. 可自动检查的依赖规则

| 层 | 可以依赖 | 禁止依赖 |
|---|---|---|
| `domain` | 标准库、同层 | Telethon、SQLite、HTTP、文件系统、UI |
| `application` | domain、application ports | 具体 Telethon/SQLite/WebDAV、中文按钮 |
| `infrastructure` | domain、application ports | Telegram UI/handler |
| `adapters` | application commands/queries/ports、domain | 直接 SQL、修改 repository 内部字段 |
| `runtime` | 所有装配对象 | 业务判断和 UI 文案 |

新增 AST/import-linter 门禁：禁止 domain 外部依赖、禁止 application import `telethon|aiosqlite|http.client`、禁止 UI import SQLite implementation、禁止 repository import adapters。

## 4. Durable 数据模型

### 4.1 Migration 接管

生产现有 schema 不重建。第一条 migration runner 上线时：

1. 用 SQLite backup API 创建一致性副本。
2. 对现有表、列、索引和关键约束计算 schema fingerprint。
3. 只有 fingerprint 与已知 TGVIO baseline 兼容时，事务创建 `schema_migrations(version, name, checksum, applied_at)` 并登记 baseline。
4. 不匹配则 fail closed，保留原库并退出；禁止自动“修复”为新空库。
5. 以后每个 SQL migration 文件不可修改；已应用 checksum 不一致立即拒绝启动。

所有网络 I/O、FFmpeg 和大文件 hash 都必须发生在事务外。一个 command 的状态快照、revision 和 event 在同一短事务提交。

### 4.2 建议新增实体

- `runtime_leases`：生产单实例 lease、owner、heartbeat、expires_at。
- `job_claims`：job_id、phase、worker_id、generation、heartbeat、expires_at。
- `intake_events`：source type/chat/message/update key，唯一约束防重复 update。
- `collection_sessions` / `collection_entries`：显式合集和文字，按 ordinal 持久化。
- `user_preferences`：spoiler/progress/completion/collection 偏好。
- PublishPlan 内的固定 destination snapshot：保留目的地、讨论组、cover/caption 与 reference-cache 隔离身份；2026-09-13 已明确退役动态 destination profile CRUD/选择。
- ArchivePackage 内的单一 Archive profile/policy snapshot：只保存非秘密 identity、policy version、`required|best_effort` 与 capability 状态；endpoint credential 继续留在部署 secret。
- `operation_tokens`：危险操作的 owner/revision/过期/单次消费 payload。
- `status_messages` 或 Job 上的 status ref：稳定状态消息恢复。
- 静态代理只来自部署环境；业务数据库不新增 `proxy_profiles`。启动时有界检测结果只以脱敏枚举供 Diagnostic Snapshot 读取。

部署工具配置（VPS host/user/key/app dir/GitHub）不属于应用 Settings，不进入业务进程。

## 5. 状态与调度

### 5.1 Job 状态

保留当前主链：

```text
received -> downloading -> downloaded -> analyzing -> analyzed
         -> planned -> publishing -> succeeded
```

控制状态不强行塞入所有阶段：

- `job_controls` 保存 requested pause/cancel 和 retry counters。
- `paused` 可作为明确 state，或保存 `hold=true + resume_state`；最终选择需一次 ADR，但必须 durable。
- `failed` 保存 phase、稳定 error code、safe summary、retry disposition。
- `partial/uncertain` 是 publish failure code + step/effect 事实，不允许普通自动 retry。
- ArchivePackage 维持独立状态机，不用 `backing_up` 污染 Job 主状态。

### 5.2 并发下载、严格发布顺序

```text
intake -> durable accepted_order
                 │
       download dispatcher (N claims)
                 │
           analyzed/planned
                 │
       publish dispatcher (single ordered lease)
                 │
         smallest publishable accepted_order
```

- 下载 worker 可并发 claim。
- publish dispatcher 每次只选择最小未越过的 `accepted_order`。
- 更早 Job 为 active download/analyze 时，晚到 Job 不发布；更早 Job cancelled/failed/held 时按显式规则跳过。
- 全局暂停只停止新 claim；正在执行的外部可见 send 到安全边界后停。
- 不创建按序永久等待的 Future；Condition/Event 只用于唤醒重新查库。
- `claim_owner/generation/heartbeat` 防重复 worker；过期 claim 经 recovery 转 interrupted/requeue 或人工检查。

是否继续“下载闸门：所有当前输入先下载完再上传”作为独立策略 flag，在 R2-04 用旧行为测试确定；无论选择如何，FIFO 不可破坏。

## 6. 外部副作用协议

### 6.1 Telegram

每个 PublishStep 的执行顺序：

1. 事务 claim step，记录 generation/idempotency key。
2. 事务外准备本地 artifact；准备失败尚无外部副作用，可安全 retry。
3. 发 Telegram 请求。
4. 收到 Message receipt 后，在一个事务写全部 effect + completion marker，再标 step succeeded。
5. 请求可能成功但无 receipt 时标 `publish_uncertain`；有部分 receipt 时标 `publish_partial`。
6. UI 最后刷新；编辑失败只记录 display error。

撤销是新的有审计 command：从 durable effects 解析 peer/message，生成短期 confirmation token，逐 peer 删除并逐项 checkpoint；不反向抹掉原始发布事实。

### 6.2 WebDAV Archive

- 只归档 canonical MediaItem；Telegram cover/thumbnail/remux/split artifact 默认不归档。
- 每个对象按 size + SHA-256 校验本地，再 PUT。
- lost response 先轮询 remote fact；大小/hash 可证实时不重复 PUT。
- `_COMPLETE.json` 是 commit boundary；MOVE 只是支持时的优化。
- Archive failure 不改变 Telegram succeeded；required policy 则在 Job completion projection 中显示未满足，而不是删除已发布消息。

## 7. Telegram 接口层

- router 只做 allowlist、私聊/owner 校验、解析和快速 callback answer。
- command handler 调 application command；query handler 调 read model。
- presenter 产出纯 `View(text, buttons)`，按钮 callback data 始终 <=64 bytes。
- 稳定 callback 编码只传 action、对象 id、revision；路径、URL、JSON、peer 不进入 callback。
- 一个 Job/collection session 维护一条主要状态消息；chat/message id 持久化，消息丢失最多补发一次。
- 进度内存采样，SQLite 每 5 秒或 32 MiB 落一次，Telegram 每任务约 2 秒节流，并有账号级 token bucket。

## 8. 配置、安全与可观测性

- runtime Settings 只含业务运行配置；deploy Settings 只存在于发布脚本的非秘密 CLI/env。
- required secret 校验只输出变量名，不输出值。
- URL 默认 `block` 私网；跟随 redirect 时每一跳重复验证。
- 所有 managed path 经过 resolve/commonpath，并拒绝 symlink escape。
- 日志字段使用 allowlist；caption、文件名、URL、chat/user/peer、绝对路径和凭据不做结构化字段。
- `/stats`、`/health`、`/diag` 和 Dashboard query 都无副作用。
- readiness 包含 migration、recovery、worker 和 singleton lease；liveness 不因 Telegram/WebDAV 短抖动自动重启到风暴。

## 9. 性能边界

- 大文件始终流式读取；hash 可在一次扫描同时产生 SHA-256/必要兼容摘要。
- Telegram download/upload concurrency 都有全局和单文件上限。
- FFmpeg/ffprobe 使用 semaphore、timeout、cancel 和完整 child reap。
- SQLite query 必须有分页/index；UI 不在 Python 中加载全部历史再切片。
- 目标门禁：100 Job/1000 Item query 和 recovery 不阻塞 event loop；历史增长不会线性增加常驻内存。

## 10. 兼容迁移策略

每次只替换一个 seam：先 characterization test，再新实现，再 adapter/re-export，最后删除旧路径。生产 rollout 使用同一 DB 和运行卷，不复制 session，不双启 Bot。只有新路径连续两个 release 无 fallback 命中且生产验证完成后，才允许删除兼容层。
