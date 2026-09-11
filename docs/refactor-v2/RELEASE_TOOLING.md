# R2-02 构建与 HostDZire 交付工具

> 状态：R2-02 实现中；本文描述本阶段唯一允许的构建、发布和回滚入口。
> 安全边界：任何命令都不得接收密码、Bot token、Telegram session、WebDAV/代理凭据或完整生产 `.env` 内容。

## 1. 构建分类

TGVIO 明确区分三类 Docker 构建：

| 类型 | 入口 | 是否有生产资格 | 是否传到 HostDZire |
|---|---|---:|---:|
| 离线测试 | `sh scripts/check_foundation.sh` 或 Docker `test` target | 否 | 否 |
| 诊断构建 | `sh scripts/build_check.sh` | 否 | 否 |
| 正式 release | `python3 scripts/deploy_hostdzire.py --phase R2-02` | 全部门禁通过后是 | 是，同一命令完成 |

测试/诊断构建用于发现问题，不会得到 release 标记，也不会启动 Bot。只有来自已推送 `origin/main` full commit 的构建才能成为 release；正式入口不提供“只构建、不部署”开关，因此不会留下已批准但未交付 VPS 的 release image。

## 2. 可复现输入

- Python 基底使用不可变 multi-platform digest；HostDZire release 固定为 `linux/amd64`。
- `requirements.txt` 只声明精确的直接依赖；生产安装使用 `requirements.lock` 的完整直接/传递依赖集合及 artifact SHA-256。
- pip 使用 `--require-hashes --no-deps --no-build-isolation`，避免隐式解析或未锁定的 build environment。
- `test` 与 `runtime` 共享同一 dependency/runtime base。`test` target 含 tests 和门禁；`runtime` 只含 `src/`、healthcheck 与镜像检查器。
- release manifest 固化 Git archive、Dockerfile、lock、源码、base image、test image 和 runtime image 的身份。操作系统包的最终事实由不可变 runtime image ID 固化。

源码 manifest 的权威算法是：

```text
find src -type f -name '*.py' -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1
```

## 3. 离线门禁

`test` target 的默认命令执行：

1. 发布树 forbidden-path 与高置信凭据扫描。
2. domain/application AST 依赖边界检查。
3. `src`、`tests`、`scripts` 的 `compileall`。
4. 临时 SQLite、fake/disabled adapters 下的 composition-root check。
5. 完整 unittest discovery。

候选容器总是用 `--network none` 运行，不挂载 `/root/TGVIO/.env`、`session/`、`data/`、`downloads/` 或 `logs/`。镜像检查还要求 runtime：

- 不含 tests、Git 元数据、秘密文件和运行卷；
- Python 源码 manifest 与 release commit 一致；
- 只包含 lock 声明的应用 Python 包；
- 不含 `gcc`、`g++` 或 `make`；
- OCI revision、release、source 和 lock labels 全部匹配。

## 4. 唯一正式发布入口

```bash
python3 scripts/deploy_hostdzire.py --phase R2-02
```

控制端必须满足：

- 当前分支是跟踪 `origin/main` 的 `main`；
- worktree 完全 clean，HEAD 已推送且 live `origin/main` 仍指向同一 full commit；
- `/root/.ssh/tgvio_hostdzire_ed25519` 是权限不宽于 `0600` 的专用私钥；
- `deploy/hostdzire_known_hosts` 只有经审计的 `HostDZire` ED25519 key；
- 当前生产 commit 是候选 commit 的祖先，生产 source 与其 Git object 一致；
- 控制端和 VPS 时钟偏差不超过五分钟。

私有 GitHub origin 的 live ref 校验会先使用现有 credential helper；无可用凭据时在 TTY 隐藏提示中读取一次 token。受控 CI 也可注入 protected `TGVIO_GITHUB_TOKEN`。该值只进入短生命周期 Git 子进程环境；askpass 文件本身不含秘密，结束后自动删除，且不会修改 Git credential 配置。

发布只从 `git archive <full-commit>` 生成源码包，不会打包 dirty worktree。控制端和 VPS 会分别检查 archive hash、危险成员、凭据模式、源码 manifest、lock 与 Dockerfile hash。

## 5. 远端状态机

正式入口在 HostDZire 依次执行：

```text
唯一 release 目录
  -> source/archive/Compose 门禁
  -> 生产只读 preflight
  -> test image build + 无网络完整测试
  -> runtime image build + 内容/身份检查
  -> 再次 preflight
  -> SQLite backup API + 源码/.env/旧 image 三重回滚点
  -> 最后一次 preflight
  -> 唯一 tgvio service 单次 recreate
  -> health/日志/commit/image/source/schema/SQLite 后验
  -> current symlink 与 full release metadata 更新
  -> 最终 manifest 与 deployment ledger
```

任何 preflight 发现以下事实都会 fail closed：容器不是健康单实例、restart count 非 0、近期 fatal marker、APP_COMMIT/source 漂移、未知 schema、SQLite 不完整、非终态 Job/progress/publish、活动 Archive/claim、partial/uncertain publish，或已有回执却没有 step commit marker。

R2-02 是 migration-free release，生产 schema 必须保持 R2-01 指纹；schema 变更必须等待 R2-03 的 migration ledger 与副本演练。

## 6. HostDZire 目录

```text
/root/TGVIO/
  .env                    # 远端独有，0600
  data/ downloads/ session/ logs/   # 永久共享运行卷
  .release-commit         # 当前 full commit
  .release-id             # 当前 release id

/root/TGVIO-current -> /root/TGVIO-releases/<release-id>/source

/root/TGVIO-releases/<release-id>/
  incoming/source.tar.gz
  source/                 # Git archive + root-only .release.env
  evidence/               # pre/postflight、manifest、ledger、构建日志
  rollback/               # SQLite/source/.env 回滚点，root-only
```

版本化 source 不拥有生产运行数据。Compose 始终把 `/root/TGVIO/{data,downloads,session,logs}` 显式 bind mount 到唯一容器，并用批准 image 的本地 immutable repository digest 启动。

## 7. 发布证据

`evidence/release-manifest.json` 和 `deployment-ledger.json` 至少记录：

- full Git commit、Git archive SHA-256、UTC 时间；
- source manifest、requirements lock 和 Dockerfile SHA-256；
- base image、test image ID、runtime image ID；
- 测试数量/耗时、无网络与 Bot-disabled 事实；
- migration before/after、schema hash、SQLite `quick_check`；
- preflight、三重回滚点、previous release 和 postflight。

证据可以记录路径、hash、计数和非敏感开关状态，但不得记录环境值、用户消息、媒体路径、Telegram peer、认证 URL 或任何凭据。

## 8. 只读检查与回滚

生产只读检查：

```bash
sh scripts/vps_check.sh
```

只验证某个已部署 release 的回滚资产，不切换生产：

```bash
sh scripts/rollback_hostdzire.sh --check <release-id>
```

真正回滚是显式 destructive operation：

```bash
sh scripts/rollback_hostdzire.sh --execute <release-id>
```

如果 manifest 表明 schema 发生变化，工具拒绝普通回滚，必须显式增加 `--restore-db`。R2-02 不改变 schema，正常回滚不会恢复数据库，避免丢失 cutover 后的合法状态。脚本不会在失败 trap 中擅自重启或重复启动 Bot；SSH 中断后先只读审计，不重复执行 release。

## 9. 凭据边界

- 专用私钥只存在控制端 `/root/.ssh/`，不进 Git、Docker context、release 包或 VPS source。
- VPS 只安装对应 public key，并禁用该 key 的 forwarding、PTY 和 user rc 能力。
- pinned host key 是服务器公开身份材料，可以进入 Git；修改它必须重新走独立认证通道核验 fingerprint。
- 生产 `.env` 只在 VPS 本机做 `0600` 回滚副本，从不下载、不 diff、不打印。
- 已在会话中暴露过的 SSH/GitHub 凭据必须在不影响专用 key 发布链后轮换。
