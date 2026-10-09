# 2026-10-08 播放卡顿排查与 115 直连调研

只读排查；未改 OpenList、未调整容器资源。输出均为耗时，未记录网盘地址、路径或凭据。

## 结论

卡顿主要来自后端取数链路，前端不是主因。

| 环节 | 实测 |
|---|---|
| Player → OpenList WebDAV，未缓存分段首字节 | 1.5–4.9 s；同一文件重复请求仍 1.5–3 s（每段都重新取链） |
| OpenList 取 115 直链（`/api/fs/get`） | 0.34–0.93 s |
| VPS（荷兰）→ 115 CDN | TCP 往返约 0.21 s，偶发丢包（一次握手 1.24 s）；TLS 完成 0.65–3.2 s |
| 开始传输后速度 | 约 1.8–2.7 MB/s |
| 已缓存分段 | 约 23 MB/s，几乎即时 |
| Player 自身 | CPU 0.2%，不是瓶颈 |
| 副本维护 ffmpeg | 占约 2/4 核，CPU 压力 some avg10≈65%；并共用 OpenList 的 115 调用配额 |

OpenList 115 存储：驱动“115 Cloud”（Cookie），`limit_rate=2`（每秒 2 次 115 接口调用），WebDAV 策略为默认代理。
Player：分块 4 MB、预读窗口 32 MB、并发 4、开头预热 16 MB（WARM_ALL），17 GB 缓存已满（约 1084 个视频）。

前端：4 倍 CPU 降速模拟中端手机，新旧界面播放均稳定 60 fps、无掉帧。片库滚动曾为旧版约 2 倍主线程耗时，定位为每张加载中封面的无限动画，已移除（TGVIO-Player `7c442ee`），现低于旧版。

## 115 直连调研

开放平台以 pick_code 调 `/open/ufile/downurl` 取直链（Bearer access_token，需 refresh 续期）；直链有时效并绑定请求 UA，宜按 (pick_code, UA) 缓存至到期前。
直连无法消除荷兰到 115 CDN 的物理延迟；能省掉的是“每段重新取链 + 新建 TLS 连接”：同一文件只取一次链接并复用 keep-alive 连接，后续拖动/续读预计可从约 2 s 降到约 0.2–0.5 s（首次打开仍约 1–2 s）。
代价：Player 需持有 115 账号凭据（Cookie 或开放平台 token，权限远大于现有只读 WebDAV 账号）、处理 UA 绑定与风控、实现新的读取适配器并保留 WebDAV 回退。

## 可选方案（按投入排序）

1. 缓存与资源：固定缓存预算并停止开头预热的反复互挤；降低维护容器 CPU 权重；视风控评估适度提高 OpenList `limit_rate`。（上游本已按 32 MB 窗口一次请求，加大 4 MB 分块不会减少请求数，此前该建议作废。）
2. Player 新增 115 读取适配器：链接缓存 + 连接复用，WebDAV 作回退。
3. 基础设施：Player 迁到靠近 115 的机房（香港/日本优化线路），往返可降到数十毫秒，所有环节同步变快。

## 方案 1 实施结果（同日）

发现预热本身是主要的负担：旧版为每个视频预热开头，但每次拉整个 32 MB 窗口；17 GB 上限满后新开头挤掉旧开头，重启又重拉。切换前一份运行约 1.5 h 的统计：上游 8.0 GB、404 次窗口请求、磁盘命中 0，常驻 4 个并发占用 OpenList 配额。

改动（TGVIO `ff76336`，Player release `player-ff76336-20261008T124556Z`，回滚点 `rollback-20261008T124643Z`，Bot 不变）：
- 缓存固定预算 4 GiB，开头区最多一半，两区互不淘汰；开头只取 4 MB 一个分块、一次请求；预热最新优先、开头区满即停。
- VPS `player.env` 仅改 `CACHE_BYTES=4294967296`、`WARM_HEAD_MB=4`（备份 `player.env.bak-cache-budget-20261008`）。
- 副本与封面容器 `docker update --cpu-shares 256`（不重启，ID 与 restarts 不变），部署脚本同步。

结果：
| 指标 | 之前 | 之后 |
|---|---|---|
| 缓存磁盘 | 17 GB | 4.1 GB（开头区约 2 GB / 512 个） |
| 根分区可用 | 28 GB | 40 GB |
| 后台预热上游流量 | 约 8 GB / 1.5 h，常驻 4 并发 | 0（启动即达开头预算并停止） |
| 已缓存段服务端首字节（VPS 本机经公网域名） | — | 0.05–0.07 s |
| 未缓存段服务端首字节 | 1.5–4.9 s | 约 1.3–3.4 s（本机测得值减去 1.2 s 客户端网络基线） |

经用户授权，用其口令登录做了上述验证；会话已登出，含私人画面的截图已删除。未缓存段仍受“每段重新取链 + 新建跨洲 TLS 连接”限制，对应方案 2。

## 115 直连实测（只读，使用 OpenList 现有登录；未输出凭据、ID、名称或路径）

定位归档：
- OpenList 的 115 存储配置了 5 个根文件夹 ID（逗号分隔）；归档位于第 2 个根文件夹下，路径第一段即该根文件夹自身名称。
- `webapi.115.com/files/getid?path=` 用完整路径一次即可解析到包目录 ID（约 0.9 s）；逐级列目录也可行（每级 1–2.7 s）。
- 包目录列表（`webapi.115.com/files?cid=`，需分页）每个文件含提取码 `pc`、大小 `s`、`sha`，可用 manifest 的大小核对。

取下载地址：
- `webapi.115.com/files/download?pickcode=` 返回的链接带 `is_115chrome`，各种请求头均 403，不可用。
- OpenList（App 端接口，链接与取链时的 User-Agent 绑定）取链 0.35–0.82 s；用相同 UA 下载：

| 读取 | 首字节 | 速度 |
|---|---|---|
| 新建连接第一段 | 1.7–2.8 s | — |
| 同连接下一段 | 0.28–0.38 s | 1.7–2.3 MB/s |
| 同连接跳到文件中部 | 0.28–0.31 s | 2.2–2.5 MB/s |

结论：链接缓存 + 连接复用可把拖动/续读从约 2–3.5 s 降到约 0.3 s。
未完成：OpenList Cookie 的设备类型未核实（需用户在 115 App 登录设备管理中确认），Player 扫码登录必须选不同类型，否则会把 OpenList 挤下线。

## 方案 2 发布（2026-10-09 UTC）

代码：TGVIO `8d2492f`（OpenList 直链读取 + 可切换读取方式，迁移 `0013_player_read_mode`）、`6451da7`（锁定前端 TGVIO-Player `8fde4cc`，设置 > 清晰度与网络 > 读取方式）。

- VPS `player.env` 新增 `TGVIO_PLAYER_OPENLIST_API_URL=http://host.docker.internal:5244`（备份 `player.env.bak-direct-read-20261009`）；发布前确认容器经 Docker 网关可达 OpenList `/ping`，WebDAV 地址路径为 `/dav`，与 OpenList API 路径一致。
- 用户本人执行 `deploy_hostdzire.py --target player --phase R2-49 --migration 0013_player_read_mode`：release `player-6451da7-20261009T010333Z`，镜像 `sha256:b387de65d36f…`，容器 `e8ba1d25077b`，healthy，restarts 0；Bot `4bf74f9f3abf`（`388c4ac`）未变；回滚点 `/root/tgvio-player/rollback-20261009T010422Z`。
- 启动日志：`read mode: webdav (direct available: True)`；公网 `/healthz` 200，未登录 `/api/v1/settings/read-mode` 401。
- 默认仍为网盘；切到“115 直连”须用户在设置中操作。直连任何失败都回落 WebDAV。
- 未验证：生产直连实际取链与拖动耗时（未用生产凭据探测），需用户切换后看缓存统计中的 direct_reads / direct_fallbacks / link_hits。

## 真实环境测速（2026-10-09，经用户授权）

本机 WSL 上的临时 headless Chrome（独立 profile，测完删除），用户口令登录 csdn.im，测完登出并恢复原读取方式（网盘）。每轮随机挑不同视频（两种方式不共用缓存），网盘/直连交替先后，共 4 轮；只记录耗时，无截图。耗时包含本机到 VPS 的网络。

| 中位数 | 网盘 | 115 直连 |
|---|---|---|
| 短视频首帧 | 0.78 s | 1.14 s |
| 长片首帧 | 2.8 s | 1.2 s |
| 长片拖到 50% | 5.4 s | 8.9 s |
| 长片拖到 85% | 5.5 s | 4.1 s |
| 超时（30 s） | 0 | 1（长片首帧） |

- 直连确实生效：`direct_reads=118`、`direct_fallbacks=0`、`link_fetches=106`、`link_hits=12`（取链多来自同期后台预热）。
- 服务端 34 个流请求全部 206，无错误；客户端 reset 为浏览器拖动时取消开放区间，属正常。
- 结论：样本少、波动大，直连没有表现出稳定优势；拖动在两种方式下都要 4–9 s，远高于此前单段首字节估计（约 0.3 s）。拖动需要连续取数 MB，受约 2 MB/s 的上游带宽与客户端网络限制，不只是取链/握手开销。
- 未定位：直连那次 30 s 首帧超时，服务端日志无对应慢请求或错误；需要服务端记录流请求首字节耗时再判断。

### 更正与服务端测量（2026-10-09）

- 更正：生产 `window_complete` 日志显示上游单窗口吞吐约 9–14 MB/s，并非上文估计的约 2 MB/s。
- 新假设（待测量验证）：范围缓存按 32 MB 对齐窗口、从窗口内第一个缺失块开始取数。拖到窗口中部时，必须先等目标位置之前的数据到达，最多约 32 MB，按上面的吞吐约 2–4 s。这可能是拖动 4–9 s 的主要部分。
- 测量方法：流请求的 `http_request` 日志新增以下字段，只有数字和固定标签：

  | 字段 | 含义 |
  |---|---|
  | `slot_wait_ms` | 等待播放槽位 |
  | `faststart` / `faststart_ms` | 是否使用 faststart 叠加，以及判定完成的时刻 |
  | `prime_ms` | 等首段数据的时间 |
  | `prime_from` | 首段来源：disk / inflight / fetched |
  | `lead_bytes` | 上游必须先送达、位于目标之前的字节数 |
  | `fetch_joined` | 是否加入了已在进行的窗口 |
  | `upstream_open_ms` / `upstream_via` / `upstream_status` / `upstream_failures` | 本请求触发的前台取数：打开耗时、读取路线（webdav / direct / direct_fallback）、状态码、失败次数 |
  | `headers_ms` | 响应头发出的时刻 |
  | `first_byte_ms` | 第一个字节发出的时刻 |
  | `sent_bytes` | 实际发出的字节数 |

  `*_ms` 字段中，`slot_wait_ms`、`prime_ms` 和 `upstream_open_ms` 是各自步骤的耗时；`faststart_ms`、`headers_ms` 和 `first_byte_ms` 是从请求开始算起的时刻。`window_complete` 日志另外新增 `start_chunk`、`open_ms` 和 `background`。
- 判读：
  - `first_byte_ms` 小而浏览器等待长：慢在客户端到 VPS 这一段。
  - `lead_bytes` 大且 `prime_ms` 高：慢在窗口对齐，下一步应改为从目标块开始取数。
  - `upstream_open_ms` 高：慢在网盘或直链建立连接。
- 发布：用户执行 `deploy_hostdzire.py --target player --phase R2-51`（无迁移）：release `player-2f7b0b4-20261009T030832Z`，镜像 `sha256:9586d7f9c9bd…`，容器 `73b93aecdce3`，healthy，restarts 0；Bot `4bf74f9f3abf` 未变；回滚点 `/root/tgvio-player/rollback-20261009T030920Z`。启动 read mode webdav；公网 `/healthz` 200。待用户实际播放后读取日志分析。

### 服务端测量结果（2026-10-09 03:17–03:20 UTC，R2-51）

用户授权，本机临时 Chrome 测 4 轮；每个长片拖到 20%、50%、85%。按媒体指纹把每个请求和服务端日志逐一对应。

**结论：拖动慢主要在服务端，原因是窗口对齐**
- 17 次需要从网盘取数的拖动中，目标位置之前要先下载的数据（`lead_bytes`）中位数是 8.5 MB，最多 33 MB。
  - 前面要下载的超过 20 MB 时：7 次，等首段数据（`prime_ms`）中位数 4.1 s。
  - 不超过 20 MB 时：10 次，中位数 1.8 s。
- 等待期间上游吞吐多数在 8–15 MB/s，与先前确认的窗口吞吐一致。
- 打开上游连接（`upstream_open_ms`）的中位数：
  - 直连 0.64 s（0.26–1.36 s）
  - 网盘 1.40 s（0.94–2.33 s）

  每次拖动直连约少等 0.8 s，这部分确实有效。
- 命中本地缓存时，服务端发出首字节的中位数是 18 ms。浏览器另外还要 1–2 s 才能播放，这部分属于客户端网络和解码。
- 网盘那次 85% 拖动 30 s 超时：服务端每次都在约 20 ms 内从缓存回应，是浏览器反复在 34/60/73/124 MB 几个位置之间重新请求。原因在客户端，不在服务端。

**修复（本地，待发布）**
- 范围缓存的取数改为从读者需要的块开始，一直取到窗口末尾，或者取到同一窗口里一段正在下载的数据之前为止。
- 同一窗口可以同时有多段下载，各自负责自己的块范围；往回拖时只补缺失的部分。
- 不带目标块的预热（`prefetch_tail`）保持原样。
- 预期效果：拖动前要先下载的数据从平均 8.5 MB（最多 33 MB）降到不超过一个块（4 MB）。拖动等首段数据的时间约为打开连接的耗时再加 0.3 s 左右。
