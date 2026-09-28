# R2-19 Player 用户视角全量验收（browser-act 实机走查）

日期：2026-09-28（UTC）
被测对象：`https://<public host>` 上的 Player，release `public-baseline-20260928-ac0ec51`（commit `ac0ec518fa9a706e50bdd9016a626fa6550be0f0`）
方式：真实浏览器（系统 Chrome 153）+ browser-act `chrome-direct` 接管，**全程静音**，移动端竖屏视口 500×845
性质：只读取证 + 一次可逆的收藏往返（见「遗留状态」）

## 1. 执行环境与工具链

- browser-act CLI v1.4.2（WSL Debian），`chrome-direct` 连接本地 Chrome。
- 静音三重保障：① Chrome 启动参数 `--mute-audio`（浏览器级硬静音，已从进程命令行核对）；② 每个 `<video>` 断言 `muted===true`；③ WSL headless 无音频设备。全程**未点击**「取消静音 / 声音」。
- 上游工具缺陷（记录备查）：browser-act 的 `chrome` 类型在本机**无法启动内核**——打过补丁的 Chromium 需要 `--store_data_path` 下加密的 `init.json`，缺失时 `Decrypt init.json path failed!`（rc=17）；managed-`chrome` 启动路径未提供，`logs/kernel-launch/` 对该类型零记录（`stealth-extract` 内部路径可正常 provision）。`chrome-direct` 亦须走 CDP 直连才可用，且 Chrome 只在 `--remote-debugging-port=0` 时写 `DevToolsActivePort`（默认 profile 路径不可用 TCP CDP）。

## 2. 用户旅程结果

| # | 用户步骤 | 结果 | 证据 |
|---|---|---|---|
| 1 | 打开站点 → 登录页渲染 | ✅ | 标题「TGVIO 私享视频 你的私人视频空间」、口令输入、「进入」；`01-login.png` |
| 2 | 输入错误口令 | ✅ | 提示「访问口令不正确，请重新输入」；`02-wrong-passphrase.png` |
| 3 | 正确口令登录 | ✅ | 进入 Feed，底部导航 首页/长视频/收藏/片库 出现 |
| 4 | 首次打开的视频保护 | ✅ 设计如此 | `data-playback-state=privacy-locked`，需点「播放并显示视频」；`06-after-reload.png` |
| 5 | 点击后播放 | ✅ | `playing`，`readyState=4`，`currentTime` 从 0 推进（实测 5.83→7.69s），`playing_count=1`；`04-playing.png` |
| 6 | **全程静音** | ✅ | `video_elements=3`、`audio_elements=0`、`every_video_muted=true`、声音控件显示「取消静音」（即当前为静音）；设置页「声音 **已关闭**」；浏览器 `--mute-audio` |
| 7 | 连续快速滑动 12 次 | ✅ | 0 个 429 / 0 个 5xx；feed 正常推进（15 页上 / 4 页下） |
| 8 | 真实 Range 流播 | ✅ | 服务端日志 37×`stream 206`、8×`prepare 202`；`Content-Range: bytes 0-1023/45626007` |
| 9 | 收藏（加入） | ✅ | 服务端 `PUT …/favorite → 200`（**3.1 ms**），按钮转「取消收藏」 |
| 10 | 收藏（取消） | ❌ **502** | 见缺陷 F1 |
| 11 | 收藏页 / 片库 / 长视频 | ✅ | 片库「已加载 20 / 共 931 条」+ 全部/短视频/长视频筛选；`09-library.png` |
| 12 | 同组视频 | ✅ | 面板「同组视频 · 2026-09-19」+ 组内条目（如 0:24 · 3840×2160）；`13-group.png` |
| 13 | 设置页（只读） | ✅ | 播放设置/清晰度（当前 720p，可选 480p/原画）/收藏与 WebDAV/退出；**未点任何保存**；`11-settings.png` |
| 14 | 删除危险操作 | ✅ 安全 | `role=alertdialog` 警告面板出现，点「取消」关闭；服务端 `media_delete` 计数 **0**；`12-delete-warning.png` |
| 15 | 全屏 | ✅ | 点击后 `document.fullscreenElement` 非空 |
| 16 | 页面报错 | ✅ 干净 | 整个会话 console `errors/unhandled/rejections/warnings` 全为 **0** |
| 17 | PWA | ✅ | `navigator.serviceWorker` 存在，manifest / apple-touch-icon 200 |

## 3. 缺陷

### F1（高）取消收藏返回 502，客户端与服务端状态分叉，远端副本永久遗留

**现象链（全部实测）**

1. 服务端日志：`DELETE /api/v1/media/{media_id}/favorite` → `status:502`、`outcome:server_error`、`error:HTTPBadGateway`、**`duration_ms: 32511`**（而对称的 `PUT` 仅 3.1 ms）。
2. 代码路径：`adapters/http/server.py:683` 把 `FavoriteBackupError` 转成 `web.HTTPBadGateway(text="favorite removal is pending")`；触发点在 `application/favorite_backup.py:120-138` 的 `unfavorite()`——它在**请求内同步**调用 `self._snapshot_exporter.export_state()`（`application/player_recovery.py:211` 起，内含两次远端原子写 `config_store.save_atomic` / `manifest_store.save_atomic`）来持久化 delete 意图。远端慢/异常（本部署归档后端约 2 MB/s）即 → `tombstone_backup_failed` → 502。
   - 对比：`favorite()`（加入）**只入队**后立即返回，所以 PUT 3 ms；**取消收藏却同步做整份远端快照** → 不对称。
3. 客户端 `player/web/src/main.ts:462 toggleFavorite()` 把非 2xx 当失败，**回滚** UI 到「已收藏」。
4. 实测分叉：

| 层 | 观测值 |
|---|---|
| UI 按钮 | `取消收藏`、`selected:true` → 显示"已收藏" |
| `player_global_favorites`（服务端真值） | **0** → 已取消 |
| `favorite_locations` | **1** → 远端副本仍被登记 |
| `favorite_sync` delete 任务 | `status=pending, attempts=0, intent_persisted=0` → **45 秒后仍无变化** |

5. 卡死是**设计使然**：`infrastructure/sqlite_favorites.py:115 claim_favorite_sync()` 的 WHERE 含 `AND (operation='upload' OR intent_persisted=1)`，而 `main.py:261` 的轮询每 5 s 调一次 → 没有 tombstone 的 delete 任务**永远不会被领取**。补 tombstone 的唯一入口仍是 `export_state()`（失败的那条路径），或用户去「收藏与 WebDAV」保存设置走迁移/导出路径。

**用户影响**：取消收藏会卡 ~32 秒后报错；星标回弹成"已收藏"而服务端已取消；远端 WebDAV 上 45 MB 的收藏副本（`player/Favorites/`）长期滞留且无人回收；下次点击会重新加入并再次上传。

**证据**：服务端 `player_event`（PUT 200 / DELETE 502）、`favorite_sync` 两行记录、`favorite_locations` 行、`docker logs` 中 `Player favorite backup batch completed: processed=1 synced=1`（那是 upload 任务）。

### F2（低）`/favicon.ico` 404
首次加载出现 2 次 `GET /favicon.ico → 404`。站点只提供 `apple-touch-icon.png` 与 `player-icon-*.png`，没有 `favicon.ico`。无功能影响，仅请求噪音与浏览器控制台/网络面板的一条 404。

### F3（说明）`<video>` 元素数量为 3–4，而设计文档写「最多 3 个」
实测：Feed 槽位（）`#feed .media-slot`）滚动后为 3（previous/current/next），另有 1 个 2×2 px 的 `.scrub-video` 辅助元素不参与播放；稳定态计 `total 3–4`、`playing` 恒为 1、全部 `muted`。设计文档表述为「最多 previous/current/next 3 个真实 `<video>`」——按 Feed 槽位口径成立，按 DOM 元素口径不成立，建议文档明确排除 scrub 辅助元素。

### F4（说明，非缺陷）未登录时 `GET /api/v1/feed → 401`
属预期：客户端启动先探测会话，未认证即 401 再渲染登录页。已确认无副作用。

## 4. 遗留状态（本次审计造成，已修复并复核）

审计产生的可逆副作用已全部清除，且清除过程同时验证了第 5 节的兜底建议：

1. 把那条卡死 delete 任务的 `intent_persisted` 置 1（单行 UPDATE，走应用自己的语义，未走捷径删文件）。
2. 后台 worker 在 **10 秒内**领取（`delete → running`），并**立即移除** `favorite_locations` 行；约 50 秒后任务转 `delete → synced`，日志 `Player favorite backup batch completed: processed=1 synced=1`。
   - `_sync_delete` 只在远端 `receipt.deleted` 为真后才删 location 行，因此远端那份约 45 MB 的 `Favorites` 副本**已确认被应用删除**。
3. 复核终态：`favorites=0`、`player_global_favorites=13`、`favorite_locations=9`、`favorite_locations` 中被审计 media 的行 = 0、`PRAGMA quick_check=ok` —— 关键计数全部回到审计前基线。
   - 唯一不是基线的是 `favorite_sync` 由 11 变 13：新增的 2 行已终态（upload `synced`、delete `synced`）。该表是 append-only 审计轨迹，保留为历史，不影响状态。
   - 未执行任何视频删除、未保存任何设置、未改动 Bot。

## 5. 建议修复（按优先级）

1. **把 tombstone/快照导出移出请求路径**：`unfavorite()` 只做本地删除 + 入队，返回 `pending`；由后台 `sync_pending` 负责 `export_state()` 并置 `intent_persisted`。这样 502 与 32 s 阻塞一并消失，且与 `favorite()` 语义对称。
2. **失败不再伪装成功**：客户端对"本地已生效、远端待同步"应显示「已取消收藏 · 待同步」而非回滚为「已收藏」；或服务端改返回 202 + `sync_status=pending` 而非 502。
3. **兜底回收**：为 `intent_persisted=0` 的 delete 任务增加有界重试/补偿路径（例如启动时或每 N 次轮询尝试补写 tombstone），避免永久滞留与远端副本泄漏。**本次审计已实测该方案有效**：手工补写 tombstone 后 worker 10 秒内领取并完成远端删除与 location 清理，说明补偿路径可行、无需改动删除语义。
4. F2：补 `favicon.ico` 或显式 204。

## 6. 证据清单

- 截图（13 张，`/root/audit-shots/`，已复制到 `C:\Users\Administrator\Downloads\tgvio-player-audit-shots\`）：登录页、错误口令、Feed、播放中、滑动后、重载后、收藏、收藏页、片库、长视频、设置、删除警告、同组视频。
- 服务端 `player_event` 明细、`favorite_sync` / `favorite_locations` 行、docker logs 摘要（均不含凭据、远端路径或媒体内容）。
