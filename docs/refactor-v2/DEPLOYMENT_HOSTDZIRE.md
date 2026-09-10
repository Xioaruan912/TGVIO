# HostDZire 发布协议

> 目标：保证每个通过门禁的 release build 都在同一交付阶段安全传递到 HostDZire，同时只运行一个生产 Bot。
> 本文只记录非秘密连接元数据；密码、私钥、Bot token、Telegram session、WebDAV/代理凭据禁止进入 Git、脚本参数、shell history 和发布日志。

## 1. 生产标识

| 项目 | 值 |
|---|---|
| 逻辑名称 | `HostDZire` |
| SSH host | `199.47.242.40` |
| SSH port | `22` |
| SSH user | `root` |
| 当前项目目录 | `/root/TGVIO` |
| 当前 Compose service / container | `tgvio` |
| 当前 SQLite | `/root/TGVIO/data/state.sqlite3` |
| 建议发布归档目录 | `/root/TGVIO-releases` |

2026-09-10 审计时密码认证可用，但 BatchMode key 认证不可用。用户提供的密码不得固化；R2-02 应安装专用 SSH public key、固定 host key，并在确认 key 登录后轮换已经在会话中暴露的密码。旧脚本中的 `sshpass` 和 `StrictHostKeyChecking=no` 不得继续使用。

推荐本机 SSH 配置只引用私钥路径，不保存秘密：

```sshconfig
Host HostDZire
    HostName 199.47.242.40
    Port 22
    User root
    IdentityFile <dedicated-key-path>
    IdentitiesOnly yes
    StrictHostKeyChecking yes
```

## 2. “每次构建都传 VPS”的精确定义

- `test build`：为测试或诊断创建，不具备生产资格，不部署。
- `failed build`：任一门禁失败，不部署。
- `release build`：来自已推送 full Git commit，所有阶段门禁通过且被标记为交付候选。每个 release build 必须在同一工作包部署 HostDZire 并完成后验。
- docs-only change：不产生应用 build，不重建或重启容器；提交并推送即可。

这样既满足每次正式构建都交付生产，也避免每次试验性 Docker layer 都重启真实 Bot。

## 3. 必需发布物

每个 release 必须生成：

1. Git archive/source bundle：仅来自已推送 commit。
2. `release-manifest.json`：full commit、UTC build time、source manifest、依赖 lock hash、image digest、migration range、测试摘要。
3. runtime image：不含 tests、`.env`、`.git`、`session/`、`data/`、`downloads/`、`logs/`。
4. test evidence：测试 target、数量、用时、compile/architecture/secret scan 结果。
5. deployment ledger：preflight、回滚点、cutover 与后验结果。

发布包中出现符号链接逃逸、设备文件、绝对路径或运行数据时必须 fail closed。

## 4. 本地/CI 门禁

发布前至少通过：

```text
clean worktree
HEAD == pushed origin commit
full unit/integration tests
python compileall
dependency-boundary tests
migration checksum + production-fixture rehearsal
git diff --check
source manifest generation
secret-pattern and forbidden-path scan
runtime image content scan
offline candidate smoke (--network none, Bot disabled)
```

- 构建参数可写 full `APP_COMMIT`，但不得把 `.env` 复制进 build context。
- Compose 校验使用占位配置；真实生产 `.env` 只留在 HostDZire，权限至少 `0600`。
- 测试 adapters 必须 fake，或临时容器使用 `--network none`。不能复制生产 session 后联网。

## 5. 远端只读 preflight

每次发布先记录，不修改状态：

- 当前 container id、image digest、full `APP_COMMIT`、health、started-at、restart count。
- 宿主与容器 source manifest 是否一致。
- `docker compose ps`，以及最近启动日志是否有 traceback/fatal/unhandled。
- SQLite 文件位置、权限、`PRAGMA quick_check`、schema/migration ledger。
- 非终态 Job、active progress、claims/leases、Archive upload、uncertain/partial effect 数量。
- 数据盘和 Docker 盘可用空间，release 包/镜像/DB backup 所需容量。
- `.env`、`data/`、`session/`、`downloads/`、`logs/` 是挂载或保留目录，不读取其秘密内容。

以下任一情况默认停止发布：

- 生产源码与已知 baseline 不一致且差异未回收进 Git。
- 有活动 Telegram send、下载、Archive PUT/MOVE 或未过期 claim。
- SQLite check 失败、schema fingerprint 未知或 migration 演练失败。
- 备份空间不足、source manifest 不一致或候选 commit 未推送。
- 无法保证切换期间只有一个 Bot 实例。

## 6. 三重回滚点

cutover 前创建并验证：

1. **数据库**：使用 SQLite backup API 创建一致性 backup；不能在 WAL 活动时只复制单个 `.sqlite3` 文件。
2. **源码**：归档当前运行源码，排除所有运行卷与秘密。
3. **镜像**：把当前 image digest 标记为 `rollback-pre-<release-id>`，记录 digest 而不只记录可变 tag。

如果需要备份 `.env`，只在远端本机复制到 root-only 目录并保持 `0600`；不下载、不展示、不计算会泄漏内容的文本 diff。发布记录只写备份路径和权限检查结果。

## 7. 候选构建与验证

优先在开发机或 CI 构建；如果暂时只能在 VPS 构建，也必须先在独立 release 目录进行，且：

- 不挂载生产 `.env`、session 或 data 到 candidate test container。
- candidate 只跑 test target、`--network none`，不执行 Bot runtime entrypoint。
- 构建完成后记录 image digest 与容器内 source manifest。
- 同一 source commit 只保留一个批准的 release image；重建导致 digest 改变时要更新 manifest 并重新门禁。

候选测试通过不等于已经发布，不能修改 `.release-commit` 或 current symlink。

## 8. 单实例切换

当前 Compose 布局下，cutover 必须是一次受控 recreate，不能先启动平行 candidate Bot：

1. 再查一次 active Job/claim/Archive，确认 preflight 窗口仍成立。
2. 确认数据库/source/image 回滚点存在且可读。
3. 把经过验证的 source/image 放到最终 release 位置；运行卷不移动、不覆盖。
4. 停止并替换唯一 `tgvio` service，禁止 scale 到 2。
5. 只有 migration/recovery/singleton lease 成功后才连接 Telegram。
6. 将 `.release-commit` 更新为 full commit；短 hash 只用于显示，不能作为唯一证据。

R2-02 后建议用原子 `current` symlink 指向版本化 release，并让 Compose 显式引用批准的 image digest；在此之前不得假定这些机制已经存在。

## 9. 强制后验

cutover 后必须记录：

- container `running` 且 health=`healthy`，readiness 通过。
- `APP_COMMIT` 是期望 full commit，image digest 等于 manifest。
- 宿主、容器与 Git source manifest 一致。
- restart count 相对 preflight 没有异常增加；started-at 符合本次切换。
- migration ledger/schema 与 release manifest 一致；SQLite `quick_check=ok`。
- 没有遗留/重复 claim，没有 Job 被错误标成 running，partial/uncertain 数量未无解释增加。
- 启动日志包含 migration/recovery、Bot commands registered/started 等预期标记，且无 secret/error marker。
- 生产容器不含 tests 和秘密路径；完整 tests 已由同 digest 对应 test target 证明。
- 按本阶段风险执行受控 smoke；可能真正发消息的 smoke 需要显式测试 Job 和清理/审计记录。

发布只有在这些后验完成后才算成功。SSH 中断或监控窗口未完成时状态应写 `CUTOVER_UNKNOWN`，先只读确认，不重复 recreate。

## 10. 回滚规则

### 无 schema 变化

- 停止当前唯一 service。
- 恢复 previous source/current link 与 previous image digest。
- 启动一次并执行完整后验。
- 除非数据库已经损坏，不恢复 DB，以免丢失切换后合法状态。

### 有 schema 变化

- 停止 service，禁止旧代码接触新 schema。
- 恢复 pre-release SQLite backup、previous source 和 image。
- 启动一次并验证 Job/Archive/claims 计数。
- 明确记录 cutover 后到回滚前可能丢失的 intake 窗口；不得隐藏数据边界。

### 已产生 Telegram/WebDAV 副作用

- 不用代码回滚假装外部消息或对象不存在。
- 读取 effect/archive journal，标记 partial/uncertain 并人工 reconcile。
- 禁止自动重发或递归删除远端目录。

## 11. 发布自动化的安全边界

发布脚本应具备：

- `set -euo pipefail` 等价的 fail-fast 行为。
- 目标 host/app/service allowlist，拒绝空变量、`/`、`~` 或 workspace root 作为删除目标。
- pinned known_hosts；SSH key agent 或受限私钥文件，不接受 password CLI 参数。
- 不使用 `eval`，不输出环境，不运行 `docker compose config` 的未脱敏完整结果。
- 上传到唯一临时 release id；校验 archive/hash 后再原子切换。
- 所有 destructive cleanup 只针对明确的旧 release id，并保留至少一个已验证回滚版本。
- trap 不能在失败时自动重复启动 Bot；失败后保留现场并报告阶段。

## 12. 发布记录模板

```text
release id:
full Git commit:
source manifest:
test result:
image digest:
migration before/after:
preflight container / health / restart:
preflight active jobs / claims / archives:
DB backup:
source backup:
rollback image digest:
cutover time (UTC):
postflight container / health / restart:
postflight APP_COMMIT / source manifest:
postflight SQLite / claims:
smoke result:
rollback deadline / retained assets:
operator / notes:
```

任何字段都不得包含密码、token、Authorization header、Telegram session、完整 WebDAV/代理 URL、用户消息或媒体路径。
