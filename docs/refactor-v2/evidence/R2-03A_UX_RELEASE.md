# R2-03A 生产反馈修复交付记录

> 状态：DELIVERED
> 交付日期：2026-09-12
> 范围：生产日志诊断、移动端按钮闭环、普通用户错误说明、Telegram 下载单流回退、宿主日志 wrapper 修复；migration=`none`。

## 1. 生产反馈与根因

对 2026-09-11 的脱敏结构化日志和 durable Job/Archive 状态做了只读关联，确认用户看到的“错误”实际来自三类互不等价的问题：

1. 重复点击未变化的内联页面触发 `MessageNotModifiedError`，旧 handler 将其作为未处理异常记录；这不代表任务失败。
2. 两个 Telegram 来源任务在并发 range 下载中耗尽 Telethon 请求重试，其中一个 Job 已经历普通安全重试；两者都停在下载阶段，没有产生 Telegram 发布副作用。
3. WebDAV 的远端 PUT 响应曾长时间超时。Telegram 发布仍成功，Archive package 独立失败；后续重试能先核对并复用远端已确认对象，本地 canonical cache 在未提交期间继续受保护。

9 月 9 日的历史限流事件未混入本次 9 月 11 日根因。审计没有输出 caption、完整 URL、媒体路径、用户凭据、`.env` 或 session 内容。

## 2. 交付内容

- Telegram 命令菜单缩为 `/start`、`/jobs`、`/status`、`/help`；旧高级命令仍兼容，但不再要求普通用户记忆。
- `/start` 安装六个 persistent reply keyboard 入口：首页、我的任务、状态、归档、缓存、更多。
- 最近任务按编号提供直接详情按钮；状态消息、任务详情和归档页提供上下文相关的重试、取消、归档重传、缓存清理和连接检测按钮。
- 所有有副作用或网络探测的按钮都有独立确认页；Job callback 每次从 repository 重新校验 owner，且所有 callback payload 都经过 Telegram 64-byte 上限测试。
- 普通任务页把内部错误码映射为“发生了什么、是否已经发布、下一步做什么”；内部码和脱敏日志只留在主动打开的技术详情中。
- 重复刷新未变化页面被当作成功确认，不再形成未处理的 `MessageNotModifiedError`。
- 并发 Telegram shard 重试耗尽后删除 partial，再自动尝试一次 Telethon 单流下载；显式取消会直接传播，不启动回退。最终文件仍必须通过大小检查并原子 rename。
- `scripts/logs.sh` 改从当前 release 的宿主源码执行查询器并读取 shared log，不再调用 runtime image 中不存在的 `/app/scripts/logs.py`。

发布顺序、PublishPlan、effect journal、Archive commit、缓存保护、URL 策略和数据库 schema 均未改变。

## 3. Release 身份

| 项目 | 值 |
|---|---|
| Release ID | `r2-03-a02e31c-20260912T044959Z` |
| Full Git commit | `a02e31c1b35673cfb9b8be54121c769026d38a9e` |
| Git archive SHA-256 | `f18bed820c65e16bf6229f5768734c214f59ee24d53728c332ead6981b6cb790` |
| Source manifest | `e14aa7c75dec44f71a8f383f75d0bd533ef82cdd72507c78df9fe71fbf48c473` |
| Requirements lock SHA-256 | `dd48955c6e23775cd25b779052d8cc4a99bba6bcd4a0179cab39010540c91514` |
| Dockerfile SHA-256 | `2d390422a3de8e562d63091ade8c3faecd8a3f803abf48f9c3ebf18af8ad2fa8` |
| Formal test image ID | `sha256:a1828d07e6c8c799d754885fef873036925c1524f32fe5edc208942ebbbd27f2` |
| Runtime image ID | `sha256:ddc6c481231b4e27537012bcdb12b95de8979d2fc25ee51b2110b650d16da06a` |

完整非敏感 manifest、ledger 和 rollback artifacts 保留在：

```text
/root/TGVIO-releases/r2-03-a02e31c-20260912T044959Z/
```

## 4. 构建与测试

- 本机补装缺失的 Docker Buildx `0.13.1`，未改项目依赖或生产主机包。
- Dirty source 诊断镜像与 pushed-commit 正式门禁均在 `--network none` 下通过 174 tests；测试容器没有生产配置，也没有启动 Bot。
- HostDZire 对正式 Git archive 再做 secret/path/architecture/compile/Compose 门禁，并在 `--network none` 下通过精确 174 tests（9.482 秒）。
- Runtime 离线检查为 47 个项目文件（45 个 Python 源文件与 2 个运行脚本）、7 个精确锁定包；无 tests、bytecode、构建工具、秘密或运行数据。
- 新增回归覆盖 persistent 键盘、四命令菜单、navigation stop-propagation、任务按钮与 owner 边界、二次确认、普通/技术错误视图、重复刷新、callback 长度、下载回退及取消不回退。

## 5. Preflight、切换与回滚点

切换前 release 为 `r2-02-569926b-20260911T063133Z`，容器 healthy、restart=0、单实例；23 个 Job 全部终态，所有 deployment blocker 为 0。SQLite `quick_check=ok`、`user_version=0`，schema SHA-256 为 `d3ee6adf7d956c5b8e6b672727258aea5d8da28514cf9c5b5ee9f672548d7d7d`。

Cutover 前创建并验证：

- SQLite backup API 副本 `state-pre.sqlite3`，SHA-256 `19caeb8cacd022e5cb62bb28a9edd6d0247e4986433cda19c08a909389ede0b2`。
- 脱敏 previous source archive，SHA-256 `d5c6d119fe1a676f90600cad3f9673c144fcddfe996bc6a58536419fd24eb3f0`。
- Previous runtime image 的 immutable rollback tag。
- 只保留在 VPS rollback 目录且 mode=`0600` 的 `.env` 副本；未读取、未下载、未写入证据。

Compose 只 recreate 唯一 service/container `tgvio`，共享 `/root/TGVIO/{data,downloads,session,logs}` 未替换，没有并行启动第二个 Bot。

## 6. 生产后验

- `/root/TGVIO-current` 指向本 release source；`.release-commit`、container `APP_COMMIT` 与 Git 都是 full commit `a02e31c1b35673cfb9b8be54121c769026d38a9e`。
- 容器 `c3dade402af3beffce6ca229d6c921d76fca4d7b25eb5003df1ace0b7cb785db` 为 `running`、health=`healthy`、restart=0、instances=1。
- Git、宿主与容器 source manifest 均为 `e14aa7c75dec44f71a8f383f75d0bd533ef82cdd72507c78df9fe71fbf48c473`；运行 image ID 与 manifest 一致。
- Bootstrap marker=1、Telegram-ready marker=1、startup error marker=0。菜单配置发生在 ready marker 之前，因此 ready 同时证明本 release 的 server command 配置没有抛错。
- 修复后的宿主 `scripts/logs.sh` 已成功查询 shared JSONL 中的 runtime 事件。
- SQLite 仍为 23 个 Job（16 succeeded、4 cancelled、3 failed），`quick_check=ok`、`user_version=0`、无 migration ledger；schema hash 前后完全一致，所有 deployment blocker 为 0。
- ArchivePackage 为 16 committed、4 failed；失败归档仍与成功 Telegram 发布分离，缓存保护规则不变。
- `scripts/rollback_hostdzire.sh --check r2-03-a02e31c-20260912T044959Z` 通过；没有执行真实回滚。

生产报告仍显示 `ntp_synchronized=no`，本次观察到约 94 秒主机时差，但未超过五分钟 fail-closed 门禁。时间同步应作为独立运维项处理，不影响本 release 身份与测试结论。

## 7. 用户入口

已有 Telegram 会话发送一次 `/start`，即可在输入框上方看到常驻六键菜单。之后日常查看、重试、取消、归档和缓存操作都可点按钮完成，无需复制 Job ID。普通错误页明确区分“尚未发布”“Telegram 结果不确定”和“仅 WebDAV 归档失败”。

## 8. 凭据后续动作

用户此前在会话中暴露过的 SSH 密码和 GitHub tokens 未进入 Git、镜像、测试或 release evidence；这些凭据仍应立即轮换/撤销。生产交付继续使用专用 SSH key 与 pinned host key。
