# R2-01 生产源码回收基线

> 状态：回收与离线门禁完成，等待 Git 推送、release build 和 HostDZire cutover。
> 证据时间：2026-09-11（UTC）；所有测试容器均未启动 Bot。

## 1. 回收来源

- 生产项目：`/root/TGVIO`；Compose service / container：`tgvio`。
- 回收前 `.release-commit` 与容器 `APP_COMMIT`：`03c84cd`，本地 Git 不存在该对象。
- 回收前容器：`running`、Docker health=`healthy`、历史 restart count=`1`。
- 回收前 image ID：`sha256:690e5ff3a62b54e54b701b203884904c5e9e622845c893b081926f93950401a9`。
- 源码快照时间：`2026-09-11T00:38:35Z`。
- 白名单归档共 82 个文件；传输归档 SHA-256 为 `76d450912534275016d68e2120eb2f2b1064eae3f3e9eb6717706bd26b45ff4a`。该值只证明本次传输包完整性；gzip 元数据使它不作为源码身份。
- 权威逐文件内容清单见 [R2-01_PRODUCTION_SOURCE.sha256](R2-01_PRODUCTION_SOURCE.sha256)。

快照只包含 `src/`、`tests/`、`scripts/`、Docker/Compose 构建文件、示例配置和源码文档。明确排除了 `.env`、`.git`、`.release-commit`、`session/`、`data/`、`downloads/`、`logs/`、`__pycache__/` 和 `*.pyc`。归档成员已验证为相对路径普通文件/目录，无符号链接、设备文件或路径穿越。

## 2. 可复现源码身份

R2-01 起，Python runtime 源码 manifest 的唯一算法为：

```sh
find src -type f -name "*.py" -print0 \
  | LC_ALL=C sort -z \
  | xargs -0 sha256sum \
  | sha256sum \
  | cut -d" " -f1
```

在生产宿主、运行容器、回收快照和本地导入树上执行，结果均为：

```text
3274cc070dc4ef76519e4e2efc478385872e36c92f377b3946b70bac2183bddf
```

R2-00 记录的 `1da1d3d0…` 没有同时记录生成算法，无法由当前文件重现，因此由上述明确算法取代，不再作为 release identity。

## 3. 导入边界

- `src/`、`tests/`、`scripts/` 与快照逐文件完全一致；分别为 44、23、6 个文件。
- `.dockerignore`、`Dockerfile`、`docker-compose.yml`、`requirements.txt` 与生产快照完全一致。
- 保留本仓库的 V2 `AGENTS.md` 和 `docs/refactor-v2/`，没有用生产的过时说明覆盖权威规则。
- `.env.example`、`.gitignore`、`README.md`、`docs/ARCHITECTURE.md` 仅做脱敏边界或生产开关事实纠偏；`src/tgvio` 没有业务修改。
- 退役旧 runtime 由 annotated tag `legacy-telegram-video-forwarder-750b3c1` 指向提交 `750b3c1`，不在当前树中保留第二个可启动入口。

## 4. 数据库与运行前提

`2026-09-11T00:36:36Z` 的只读复核结果：

- SQLite `quick_check=ok`，`PRAGMA user_version=0`。
- Job：8 succeeded、4 cancelled、1 failed；非终态 Job 为 0。
- PublishStep：23 succeeded、1 failed、1 pending；ArchivePackage：10 committed、2 failed。
- schema SQL 使用 `SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL ORDER BY type, name`，以换行连接并追加末尾换行后计算 SHA-256，结果为 `d3ee6adf7d956c5b8e6b672727258aea5d8da28514cf9c5b5ee9f672548d7d7d`。
- 本阶段不修改 schema，也不启动 migration。

## 5. 离线门禁

- 本机 Python 3.13.5：`compileall` 通过。
- AST 依赖边界：domain 未引入 Telethon/SQLite/HTTP/adapters/infrastructure；application 未引入具体 Telethon/aiosqlite/HTTP client/adapters/infrastructure。
- 当前生产依赖镜像 + 只读挂载回收源码/tests/scripts + `--network none`：137 tests，8.471 秒，全部通过。
- 同一无网络容器使用占位配置运行 `python -m tgvio.main --check`：通过；`TGVIO_RUN_BOT=false`，未连接 Telegram。
- staged-tree `git diff --check` 在关闭 `blank-at-eof` 规则后通过；该单项例外用于保留生产源码既有的末尾空行，未顺手格式化 runtime，其它 whitespace 规则仍启用。
- 工作树和生产快照的脱敏扫描只命中历史文档占位 host 与 `example.test` 的凭据 URL 拒绝测试；未发现 GitHub token、Telegram token、私钥或真实 secret assignment。
- 工作树未发现 `.env`、session、SQLite 或其它运行数据；回收交付树未发现非普通文件。

## 6. 待完成交付

本文件只证明回收内容和离线行为基线。R2-01 只有在以下事项全部完成后才能标为 `DELIVERED`：

1. 回收提交与 legacy tag 推送 GitHub。
2. 从已推送 full commit 生成唯一 release，建立 source/image/schema manifest。
3. 创建 DB/source/image 三重回滚点并单实例切换 HostDZire。
4. 核对 full `APP_COMMIT`、宿主/容器源码 manifest、health、restart delta、SQLite 和日志。
