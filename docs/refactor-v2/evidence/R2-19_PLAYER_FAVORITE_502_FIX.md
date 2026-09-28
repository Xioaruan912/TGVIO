# R2-19 Player 取消收藏 502 修复与部署

日期：2026-09-28（UTC）
来源：`docs/refactor-v2/evidence/R2-19_PLAYER_USER_JOURNEY_AUDIT.md` 的缺陷 F1
范围：Player 独立服务。Bot 未重建、未重启、未改配置。

## 1. 发布身份

| 项 | 值 |
|---|---|
| Release id | `unfavorite-fix-20260928-752e604` |
| Full Git commit | `752e60474ed2ea15e384696ef85abe5f2f954a73`（clean `origin/main`） |
| 源码归档 | 702,561 B 级 `git archive`，SHA-256 `8ec887b1410cdd535986ce80936e0a867987c59142c3fbe14504fbcd92b77678` |
| 镜像归档（save+gzip） | SHA-256 `a449b66b699c84de488c226bff204dc3897c85461e49795beeee42999f3c8a3b` |
| 控制端镜像 id | `sha256:b5254e67c0e07e161e1c2f139c6e7f90e89b16708d89a2d01821a52693efe2f4` |
| 生产加载后镜像 id | `sha256:830b882e921bbefc99e98c4289dd9c15155b055e54aca4347859054bea6fa3fb` |
| 容器内内容 manifest | `d704d2f43092c909911f6a1b2869d38583bd99d99e1c65fca91c4fdc9738985b`（控制端与生产**逐字节相同**） |
| Player 容器 | `37a589c6cd73ec9aeacb0d267d59fd965b426a1c12d718713b377c70d6c0a881`，起于 `2026-09-28T15:12:02Z` |
| Bot 容器 | `9482d376adaa054e0758ea2dad031c12884f00f3faeac4408aedb77e6694c063`，起于 `06:29:11Z`（与 preflight 相同，未重启） |
| 前端资产 | `assets/index-gDIySyLg.js`（含客户端改动） |

跨主机镜像 id 差异与上次同因：`docker save`（OCI）到 classic-store `docker load` 会重写 image config JSON，层摘要与容器内内容保持一致，故以内容 manifest 为身份证据。

## 2. 根因（来自用户视角审计）

`unfavorite` 在**请求内同步**执行整份远端状态快照导出（`player_recovery.export_state()` → `config_store.save_atomic` + `manifest_store.save_atomic` 两次远端写），远端慢即抛错，经 `server.py:683` 变成 `HTTPBadGateway("favorite removal is pending")`。对称的 `favorite()` 只入队，所以加入只要毫秒级。

本次实测该远端写在此归档后端约需 **80 秒**（t+80s 才见到 upload 转 `synced`），因此请求内做这件事必然超时。后果链：502 → 客户端回滚星标（UI 与服务端分叉）→ delete 任务留下 `intent_persisted=0`，被 `claim_favorite_sync` 的 `operation='upload' OR intent_persisted=1` 永久拒领 → 远端副本滞留。

## 3. 改动

`src/tgvio_player/application/favorite_backup.py`
- `unfavorite()`：只做本地取消 + 入队 delete，返回 `pending`；**请求路径不再有任何远端 I/O**，也不再因远端失败抛错。
- 新增 `_persist_pending_delete_intents()`：在 `sync_pending()` 领取任务**之前**，对存在「未持久化 delete 意图」的任务执行一次快照导出并显式补写 `intent_persisted`。
- 顺序不变式保留：**远端快照先持久化，才会发生远端删除**（由 `claim_favorite_sync` 的既有条件继续强制）。
- 失败不再影响任何请求：仅告警并让任务留待下一轮；新增 `tombstone_retry_seconds`（默认 60s）节流，避免坏归档被每 5s 轮询反复冲击。
- 删除已无引用的 `tombstone_backup_failed` 错误类别。

`player/web/src/main.ts`
- 取消收藏的提示改为 `已取消收藏 · 待同步/同步中/已同步/同步失败`，不再默示远端清理已完成。

## 4. 测试

- `tests/test_player_favorites_backup.py`：改写 2 例、新增 2 例，共 10 例通过。
  - `test_unfavorite_never_touches_the_archive_and_sync_persists_the_tombstone_first`：断言取消收藏期间 writer 调用与 `export_state` 次数**均不增加**，随后一个同步周期内先出现 tombstone 再出现 delete。
  - `test_unfavorite_succeeds_for_the_user_while_the_tombstone_is_unavailable`：快照导出失败时取消收藏**仍返回 pending 成功**，远端删除被扣住，恢复后自动完成删除并清理 location。
  - `test_repeated_tombstone_failure_is_throttled_between_sync_cycles`：连续失败只尝试一次导出。
- 全量离线：**899 tests OK**；前端 `npm test` **24/24**、`npm run build` 通过；`release_guard verify-tree`（478 文件）/`architecture` 通过；`git diff --check` clean。

## 5. 备份、演练与回滚资产

- 在线备份（SQLite backup API，root-only）：`/root/tgvio-player/backups/player-pre-unfavorite-fix-20260928-752e604.sqlite3`，39,596,032 B，mode `600`，SHA-256 `61df420f994ea25b6a92e5e83c6de6ef5efb95e5b69281135e6d4ec2e3051579`。
- 演练（新构建对生产副本）：账本 1–8 与公开白名单一致，schema/对象数/16 张表行数**全部不变** → 未应用任何迁移。
- 回滚镜像标签：`tgvio-player:rollback-unfavorite-fix-20260928-752e604` = `sha256:6c3bb2fc…`（切换前运行镜像，已验证一致）。
- 环境备份：`/root/tgvio-player/player.env.bak-unfavorite-fix-20260928-752e604`（`600`）。env 仅改 `TGVIO_PLAYER_IMAGE` 一行，22 行数不变，无 Bot 变量。
- 上一版源码目录 `/root/tgvio-player/releases/public-baseline-20260928-ac0ec51` 保留。

无 schema/数据迁移，回滚不需要恢复数据库。

## 6. 验收：正是原先失败的那条操作

| 指标 | 修复前（ac0ec51） | 修复后（752e604） |
|---|---|---|
| `DELETE /api/v1/media/{id}/favorite` | **502** `HTTPBadGateway` | **200** |
| 请求耗时 | **32,511 ms** | **3.5 ms** |
| 响应体 | 错误文本 | `{"favorite": false, "sync_status": "pending"}` |
| 对称操作 `PUT` | 3.1 ms | 7.6 ms |
| 日志中 `"status":502` | 1 | **0** |
| 未持久化 delete 任务残留 | 1（永不领取） | **0** |

后台收敛（同一操作继续观察）：

```text
t+ 80s  upload=synced   delete=running(attempts=1, intent_persisted=1)
t+ 95s  upload=synced   delete=synced
Player favorite backup batch completed: processed=2 synced=2 retried=0 failed=0
favorite_locations 回到 9（基线），该 media 的 location 行 = 0，favorites=0，player_global_favorites=13
```

其他后验：Player `running healthy` restarts=0 单实例；Bot 容器 id 与启动时间不变；账本仍 1–8、checksum 不变、schema 不变；`PRAGMA quick_check=ok`；`/healthz` 200、`/` 200、认证 `/api/v1/feed` 200、Range **206**（1024 字节）；启动至今 0 traceback / 0 ERROR / 0 CRITICAL、0 条 tombstone 告警。

## 7. 残留与未验证

- 远端快照导出本身仍是慢操作（此归档后端约 80 秒），只是移出了请求路径；它现在与上传/删除一起在后台完成，用户不再等待。若要在前台可见进度，需要后续把同步状态呈现成任务视图（本次仅做了提示文案）。
- 未做手机真机复测；本次改动为服务端行为 + 一句提示文案，浏览器侧已由 `index-gDIySyLg.js` 资产名确认上线，且 Range/认证/feed 均在浏览器同源路径上复核过。
- `_unfavorite` 路由对畸形 media_id 仍会走到 `FavoriteBackupError → 502`（既有的小语义瑕疵，UI 不会产生该输入）。本次未改动，避免扩大范围。
