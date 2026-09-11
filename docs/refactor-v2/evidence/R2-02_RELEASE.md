# R2-02 Release 交付记录

> 状态：DELIVERED
> 交付日期：2026-09-11
> 范围：可复现 test/runtime build、fail-closed HostDZire 交付链、版本化 release、证据与显式回滚；无业务源码或 schema 变更。

## 1. Release 身份

| 项目 | 值 |
|---|---|
| Release ID | `r2-02-569926b-20260911T063133Z` |
| Full Git commit | `569926b53af19539b118daa93f95c58da2001637` |
| Git archive SHA-256 | `da429d64fc28d88cb9d02bee29fe3b578fdf4bee3389894f2c9f2e47ad0d5706` |
| Source manifest | `3274cc070dc4ef76519e4e2efc478385872e36c92f377b3946b70bac2183bddf` |
| Requirements lock SHA-256 | `dd48955c6e23775cd25b779052d8cc4a99bba6bcd4a0179cab39010540c91514` |
| Dockerfile SHA-256 | `2d390422a3de8e562d63091ade8c3faecd8a3f803abf48f9c3ebf18af8ad2fa8` |
| Test image ID | `sha256:67fda3292a0bbd46d0ab342fb9e0a93477ae3f682e21a6f426412eb6df2d4865` |
| Runtime image ID | `sha256:59571703bf505b02fe19db6ba73835c905445ba6b3e23b6e2fa57a5fa87f34b7` |
| Platform/base | `linux/amd64`; `python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534` |

Full non-secret machine manifest and deployment ledger remain on HostDZire at:

```text
/root/TGVIO-releases/r2-02-569926b-20260911T063133Z/evidence/
```

## 2. Git 与构建门禁

- `main`/`origin/main`/live private GitHub ref 都解析为 full commit `569926b53af19539b118daa93f95c58da2001637`。
- Release source 只由该 commit 的 `git archive` 生成；控制端与 VPS 两次 archive/secret/path 检查通过。
- 私有 origin 用隐藏式一次性 askpass 验证；token 未进入 argv、Git config、release source 或日志。
- Exact formal test image 在 `--network none`、无生产 env/session/data/downloads/logs 下运行，156 tests 在 9.569 秒内全部通过；Bot 未启动。
- `compileall`、composition check、domain/application AST 边界、Compose placeholder、dirty/unpushed/source mismatch 等 fail-closed 门禁通过。
- Runtime 离线检查为 46 个项目文件（44 个 `src/tgvio` Python 文件与 2 个运行脚本）、7 个精确锁定 Python 包；无 tests、bytecode、秘密/运行目录或 `gcc/g++/make`。
- Runtime OCI revision/release/source/lock labels、`linux/amd64` 平台和 immutable local repository digest 均与 manifest 一致。

正式 release 之前产生的本地/VPS diagnostic images 没有 release 身份，也没有部署。诊断曾发现递归 `.dockerignore` 未排除 bytecode，随后门禁被加固并在正式 commit 中验证。

## 3. Preflight、备份与切换

切换前生产事实：

- Commit/image：`40a8cde65bc196d880336995dbea61fbe3388b2f` / `sha256:e570bf3b6b8be4977bf3406b4f4ded42d70bf6ec9b3286425fa5cbd218892421`。
- 容器 `7a5e50354bdeffe4e7ed31023ee038f807262415dc6987d344318f69c393cbc5`，healthy、restart=0、单实例。
- SQLite 13 个 Job；active Job/progress/publish/Archive/claim、partial/uncertain 与 uncommitted visible effects 全部为 0。
- `quick_check=ok`，`user_version=0`，schema SHA-256 为 `d3ee6adf7d956c5b8e6b672727258aea5d8da28514cf9c5b5ee9f672548d7d7d`。

Cutover 前建立并验证三重回滚点：

| 回滚点 | 事实 |
|---|---|
| SQLite backup API | `state-pre.sqlite3`; SHA-256 `216582e31c014d28333fc6659852e2132240c80e7f376194877ebc9ff1888a13` |
| 脱敏源码归档 | `source-pre.tar.gz`; SHA-256 `fbe8cbaa03b789943d32800ca0b5209eb0733a446456c14c82d452f740b4c107` |
| Previous image | `tgvio-rollback-pre:r2-02-569926b-20260911T063133Z` 指向精确 R2-01 image ID |
| 远端 `.env` 副本 | 只在 release rollback 目录，mode=`0600`；未下载、未读取、未写入证据 |

Compose 使用 `/root/TGVIO/{data,downloads,session,logs}` 作为显式共享 bind mounts，只对唯一 service `tgvio` 做一次 recreate；没有平行 Bot 实例。

## 4. Postflight

- `/root/TGVIO-current` 指向 `/root/TGVIO-releases/r2-02-569926b-20260911T063133Z/source`。
- 新容器 `57d5c05cacfcb32333cf18b651404ad57e02050e72ec0829ebbcb03f36445583`，started-at `2026-09-11T06:28:39.441287843Z`。
- 状态 `running`、health=`healthy`、restart=0、实例数=1。
- `.release-commit`、container `APP_COMMIT` 和 Git 都是 full commit `569926b53af19539b118daa93f95c58da2001637`。
- Host source、container source 和 Git source manifest 都是 `3274cc070dc4ef76519e4e2efc478385872e36c92f377b3946b70bac2183bddf`。
- Container image ID 与 release manifest 精确一致；独立无挂载、无网络 image inspection 再次通过。
- Bootstrap marker=1、Telegram-ready marker=1、最近 fatal/traceback/unhandled/uncaught marker=0。
- SQLite `quick_check=ok`，仍为 13 个 Job、`user_version=0`、相同 schema hash；所有 deployment blocker 为 0。
- `scripts/rollback_hostdzire.sh --check r2-02-569926b-20260911T063133Z` 已验证 DB/source hash、`.env` 副本权限、previous source manifest、previous image 与 rollback tag，结果 passed；没有执行真实回滚。

首次按文档用 `sh` 调用 Bash rollback wrapper 时在本地、SSH 前失败；生产未改变。示例已经修正为直接执行脚本，随后同一 `--check` 通过。

## 5. 功能与 schema 边界

- 本阶段没有修改 `src/tgvio`，source manifest 与 R2-01 完全相同，因此没有引入业务语义分叉。
- 本阶段 migration=`none`；schema/user_version 前后完全一致。
- 没有执行会产生频道消息或 WebDAV 对象的 smoke。既有 137 项业务 characterization 加 19 项发布工具测试全部通过，生产 Telegram runtime 已连接并健康。
- DP-02 已改为 `VERIFIED`。旧功能缺口仍按 `FEATURE_CONTRACT.md` 保持 `REQUIRED`，下一阶段是 R2-03 migration ledger 与 repository 拆分。

## 6. 凭据后续动作

专用 SSH key 与 pinned host key 已接管自动交付。用户此前在会话中暴露过的 SSH 密码和 GitHub tokens 不在 Git、镜像或 release evidence 中，但仍应在确认新登录链稳定后轮换/撤销。
