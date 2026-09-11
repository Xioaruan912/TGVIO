# R2-01 HostDZire 发布记录

> 阶段：R2-01；状态：`DELIVERED`；日期：2026-09-11。
> 本记录只含非敏感部署事实。release build manifest 见 [R2-01_RELEASE_MANIFEST.json](R2-01_RELEASE_MANIFEST.json)。

## 1. Release 身份

| 项目 | 值 |
|---|---|
| Release ID | `r2-01-40a8cde-20260911T004756Z` |
| Git commit | `40a8cde65bc196d880336995dbea61fbe3388b2f` |
| Git archive SHA-256 | `a6e2e1b415b261e51a51774ffe91be9a0be555ff2fb87efdd425c3be5fbffdf3` |
| Source manifest | `3274cc070dc4ef76519e4e2efc478385872e36c92f377b3946b70bac2183bddf` |
| Requirements SHA-256 | `a2c95ca97f91ef742ebcd7e64b86de74fae94f0191caa62adc945d6cd6b3aae4` |
| Dependency-set SHA-256 | `f46c4131b71f187c8e1c2850cc3bcaaf9f95f0a6e98c41d93c0ece5342fa4f6d` |
| Release image ID | `sha256:e570bf3b6b8be4977bf3406b4f4ded42d70bf6ec9b3286425fa5cbd218892421` |
| Release manifest SHA-256 | `5dca8a8f9f0f71e6cc1096fc2d3b8f4e11e1ca78007adc61de676c3575574346` |

提交和 annotated legacy tag `legacy-telegram-video-forwarder-750b3c1` 均已推送 GitHub。归档只来自已推送提交，未包含 `.env`、session、data、downloads、logs、SQLite 或 Git 元数据。

## 2. 构建与门禁

标准 Dockerfile 的首次无网络尝试在 apt 层缓存未命中后退出，没有生成 image。为避免浮动依赖重新解析改变行为，正式 release 固定继承部署前已验证 image `sha256:690e5ff3a62b54e54b701b203884904c5e9e622845c893b081926f93950401a9`，仅覆盖该提交的 `src/` 和 `scripts/`，并写入 full `APP_COMMIT`。正式 image 的依赖集合与基底哈希完全一致。

- 精确 release image、`--network none`、未加载生产配置/session：137 tests，8.634 秒，全部通过。
- Bot-disabled foundation check、`compileall`、AST 依赖边界、镜像 forbidden-path 与 secret-pattern 门禁均通过。
- 镜像不含 tests、`.env`、`.git` 或运行卷；容器内源码 manifest 与 Git/宿主相同。
- 本阶段无 migration；`user_version 0 -> 0`，schema SQL SHA-256 保持 `d3ee6adf7d956c5b8e6b672727258aea5d8da28514cf9c5b5ee9f672548d7d7d`。
- 多阶段标准构建、依赖锁和自动交付仍属于 R2-02；本阶段的 production-base overlay 不是长期发布方案。

## 3. 回滚点

| 类型 | 位置/标识 | 校验 |
|---|---|---|
| Source | `/root/TGVIO-releases/r2-01-40a8cde-20260911T004756Z/rollback/source-pre.tar.gz` | SHA-256 `76d450912534275016d68e2120eb2f2b1064eae3f3e9eb6717706bd26b45ff4a`，mode 0600 |
| SQLite | `/root/TGVIO-releases/r2-01-40a8cde-20260911T004756Z/rollback/state-pre.sqlite3` | SHA-256 `a95b61342347feba6d95727cdb7f523d4a0c4e9de6746e4f4334e0259936ea05`，593,920 bytes，`quick_check=ok`，mode 0600 |
| Environment | `/root/TGVIO-releases/r2-01-40a8cde-20260911T004756Z/rollback/env-pre.bak` | mode 0600；内容和 hash 未输出 |
| Image | `tgvio-rollback:pre-r2-01-40a8cde-20260911T004756Z` | `sha256:690e5ff3a62b54e54b701b203884904c5e9e622845c893b081926f93950401a9` |

SQLite 备份使用 backup API，且在旧容器停止后创建。源码和配置备份均只留在 VPS。

## 4. Cutover 与后验

- Final preflight：`quick_check=ok`，active jobs/progress/archives 均为 0。
- 发布时观测到 VPS UTC 时钟比控制端约慢 1～2 分钟，所以 image created-at 可能早于控制端生成的 release ID；所有身份判断均使用 commit/hash/image ID，不依赖文件时间。R2-02 preflight 应加入时钟同步检查。
- 旧容器停止：`2026-09-11T00:50:31Z`；新容器 started-at：`2026-09-11T00:51:02.596876006Z`。
- 后验：`2026-09-11T00:52:25Z`；容器 ID `7a5e50354bdeffe4e7ed31023ee038f807262415dc6987d344318f69c393cbc5`。
- 只有 1 个 Compose `tgvio` 实例；状态 `running`、health=`healthy`，旧容器历史 restart=1，新容器 restart=0。
- `APP_COMMIT` 与 `.release-commit` 都是 full `40a8cde65bc196d880336995dbea61fbe3388b2f`。
- 宿主与容器源码 manifest 均为 `3274cc070dc4ef76519e4e2efc478385872e36c92f377b3946b70bac2183bddf`。
- SQLite 仍为 8 succeeded、4 cancelled、1 failed；PublishStep 23 succeeded、1 failed、1 pending；ArchivePackage 10 committed、2 failed；active jobs/progress/archives 均为 0。
- 启动日志各出现 1 次 bootstrap/Telegram-ready，traceback/fatal/unhandled/exception marker 为 0。
- 没有执行真实媒体发布 smoke：本阶段 runtime 字节与原生产一致，选择避免制造 Telegram/WebDAV 外部副作用；连接 readiness、历史持久副作用证据及精确 image 的离线全量测试共同作为本阶段核心流程验收。

## 5. 下一步

R2-02 建立依赖锁、多阶段 test/runtime image、专用 SSH key、pinned known_hosts 和 fail-closed 自动部署。R2-02 完成前，导入的 `deploy_preview.sh` 与 `vps_check.sh` 仍是已知过时脚本，不得直接用于生产发布。
