# R2-19 Player：HEAD 短路、suffix range 与长视频尾部预热

日期：2026-09-29（UTC）
来源：与两个同类项目的流式实现对比（`qianlong520/Telegram_MistRelay` 内嵌的 WebStreamer `stream_routes.py`、`lm317379829/TGFileBot` 的 `stream.go`），本轮为**源码级**对比，非文档级。
范围：Player 独立服务。Bot 未参与本次切换（Bot 的 r2-47 是另一笔发布）。

## 1. 发布身份

| 项 | 值 |
|---|---|
| Release id | `probe-range-tail-20260928-03a91b4` |
| Full Git commit | `03a91b4d04a9f9808fb078c9ed6e8d8e8eaadd02` |
| 控制端镜像 id | `sha256:ed6ee754c2700febbe2da0e151d8c248349c93e495f9348dc05477c49016e445` |
| 容器内内容 manifest | `d038e4f9fe30b490d1d9d261b9c46155ef2a248ad05592660fd7a32c4646319f`（两端一致） |
| Player 容器 | `e4200d626c0dc8fc28576baa96153efde0f6a4ab739f0590bef5d0017a6524b6`，起于 `2026-09-28T23:33:20Z` |
| 回滚镜像标签 | `tgvio-player:rollback-probe-range-tail-20260928-03a91b4` = 上一版 `sha256:ca22338149b4…` |
| DB 备份 | `player-pre-probe-range-tail-20260928-03a91b4.sqlite3`，39,596,032 B，mode `600`，sha256 `223e0fba07a5e78e4f2eb67f9d772a528c1075355c5ef7d3d3c19522aae09eef` |
| 前端资产 | `index-Ci84OQzu.js`（本轮无前端改动） |

## 2. 三处改动与依据

### 2.1 HEAD 曾经不是"免费"的

实测（2,088,573,997 B 的片，修复前）：`HEAD` 会走完整流式路径 —— `prime()` 一个对齐窗口、占用一个播放流槽，并把整片长度当"已发送字节"记日志（`response_bytes: 2088573997`），且被分类成 `client_disconnected`。

现在**先于**任何归档读/流槽获取而返回：

```
HEAD + Range: bytes=314572800-315621375   → 206, body 0 字节, 2 ms, 归档窗口读取 = 0
HEAD（无 Range）                          → 200, 2 ms, Content-Length=2088573997, 日志 outcome=stream_ok
```

三处响应出口（普通流 / 虚拟 faststart / 启动缓存命中）与 HEAD 现在共用同一个 header 构造器（`_media_response_headers`），避免"探测结果与真实响应不一致"。对照实现：MistRelay 的 `if request.method == "HEAD": … release_bot_slot(index)`，TGFileBot 同理。

### 2.2 suffix range 从 416 变成 206

我们此前**刻意**不支持 `bytes=-N`（`domain/ranges.py` 注释原文 "Suffix and multipart ranges are deliberately unsupported"），而这正是播放器读取"moov 在文件尾"的片子的方式，也是 RFC 7233 要求支持的形式。

```
GET + Range: bytes=-65536 → 206, 65536 字节, Content-Range: bytes 2088508461-2088573996/2088573997
```

multipart（`bytes=0-1,2-3`）、`bytes=-0`、`bytes=-`、`bytes=10-`（越界）、`bytes=9-8`（倒序）仍然 416。

### 2.3 长视频进入列表时就预热尾窗

播放器拖动到 70% 才预热尾部是"用户先等一次"；现在**列表阶段**就会为长视频（`duration_seconds >= LARGE_VIDEO_SECONDS`）预热尾窗，短片不动（短片本来就会把尾部播完，不值得付代价）。

实测：对已知的 2740 s / 2 GB 长片，**只发 4 次 `/api/v1/feed`、完全没有任何播放请求**，日志即出现该片的 `window_complete win=62`（2,088,573,997 的最后一个 32 MB 窗口）。

## 3. 测试与门禁

- 新增/改写：`tests/test_player_http.py`（HEAD 不碰归档且不占流槽、suffix 四种取值、`?tail=1`、长片列表预热触发与短片不触发）、`tests/test_player_backend.py`（suffix 解析与 416 矩阵）、`tests/test_player_range_cache.py`。
- 全量离线 **909 tests OK**（原 906）。前端未改动（`npm test` 24/24、`npm run build` 亦随全量门禁跑过）。
- `release_guard verify-tree`（480 文件）/`architecture` 通过；`git diff --check` clean。
- 附带修复：`server.py` 因新增逻辑一度到 1004 行，超出仓库 1000 行硬预算；预热调度器移入 `streaming.py` 的 mixin（同名方法，实例属性仍可遮蔽，既有测试的 mock 不受影响），`server.py` 回到 969 行。

## 4. 部署与后验

- 只读 preflight → 构建（镜像内离线断言 4 个符号）→ 传输（镜像/源码 SHA-256 两端一致 + 内容 manifest 相同）→ root-only DB 备份 + 谱系演练（账本 1–8 与公开白名单一致、schema 与 16 张表计数**零变化**）→ 回滚资产 → env 单行切换（mode 600）→ `player_deploy.sh --execute`（`bot_container_unchanged=true`）。
- 后验：Player `running healthy`、restarts 0、单实例；账本/schema 与切换前备份一致；收藏四表 `0/13/9/15` 与 preflight 相同；`/healthz`、`/`、认证 `/api/v1/feed` 均 200；Range 206；0 traceback / 0 ERROR / 0 CRITICAL / 0 个 5xx；公网 `/healthz` 200。

## 5. 残留

- **未做真机浏览器复测**：三项都是服务端行为，已由 909 项离线测试与生产 curl 级验收覆盖；但"某些播放器用 suffix range 之后首帧是否更快"仍需真机观察。
- HEAD 的日志 `response_bytes` 仍等于 `Content-Length`（日志字段语义如此），这是显示口径而非真实发送量；如需区分可另立字段。
- 长片列表预热每个"被看过的长视频"会付一个对齐窗口（本部署 32 MB）的后台流量；已按 `LARGE_VIDEO_SECONDS` 与 `WARM_TAIL_MB` 双重设限。
