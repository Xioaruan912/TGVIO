# 2026-10-04 Player 本地封面镜像发布

用户此前授权"部署允许"，本轮按 `docs/superpowers/plans/2026-10-04-player-cover-mirror.md`
交付并上线。**只切换 Player**，Bot 全程未动。

## 为什么做

每张首次封面都是一次到 WebDAV 的往返（6 条车道、服务端 6 个封面槽位、无任何本地副本），
这是"封面一直在转"的根因。归档已经用**自身字节的 sha256** 命名封面
（`cover/backfill/<sha256>.jpg`），所以本地副本不需要任何失效逻辑。实测全量仅
**956 个文件 / 12.5 MiB**（相对 17GB 播放缓存是千分之一）。

## 发布事实

| 项目 | 值 |
|---|---|
| 应用提交 | `357142a8f37c43d538a730269464e57c59affc42` |
| Release id / tag | `cover-mirror-357142a` / `tgvio-player:cover-mirror-357142a` |
| VPS 导入镜像 ID | `sha256:38e2a4ece705e2f574d50861cd8145d6ed9fdb9112ce47189625917082650bc0` |
| 传输包 SHA-256 | `742571a89724a8d5626c0dfbb8d116652ce7253b331ee731802b4694bfc78416`（两端一致） |
| 新 Player 容器 | `a8f8e7fbb02634791aaa19d665a302a7a1d166e6e79a63956506adb5d077fbac`（healthy / restarts 0） |
| 回滚点 | `/root/tgvio-player/rollback-20261003T203314Z`（原镜像 `sha256:cc2979c2…`、原 env、切换前库） |
| Bot | `408fd4e6…` restarts 0，未变 |
| 迁移 | 无（账本仍为 `[1..8,10,11,12]`） |

## 实现要点（与 spec 逐条对齐）

- **存储**：`${TGVIO_PLAYER_DATA_DIR}/covers/<sha256>.jpg`，落在**已有 bind mount 内** ——
  不改 compose、不加卷、不加容器。key 只来自目录元数据 `remote_relpath` 的 basename，
  必须匹配 `^[0-9a-f]{64}\.jpg$`，否则不镜像。
- **读路径**：鉴权、`active_cover()`、`?v=` 校验**全部照旧**；命中本地直接发（**不读上游、
  不占封面预算**）；未命中读上游并**边发边写**（临时文件 + `os.replace`，半张图永不可服务）；
  上游失败但本地有副本 → 发本地；无副本 → 与今天一致。
- **自动预热**：进程内有界循环，单轮 64、并发 ≤ 封面预算一半、**每张之间 200ms 间隔**、
  预算满即停；瞬时失败重试一次后跳过；**上游 404 记入进程内存并按内容寻址 key 记忆**，
  后续轮次不再探（重新发布的封面哈希不同，绝不会被误判）；有 backlog 按
  `TGVIO_PLAYER_COVER_MIRROR_INTERVAL_SECONDS`（默认 30s）追赶，无 backlog 退避 900s。
- **开关与观测**：`TGVIO_PLAYER_COVER_MIRROR=off` 时不建目录、不计数、不起任务（等于今天）；
  `/healthz` 的 `stream_capacity.cover_mirror` 报告 enabled/files/bytes/hits/misses/warm_pending/warm_failed。

## 上线后验

| 检查 | 结果 |
|---|---|
| 公网 `/` `/healthz` / 未登录 feed | 200 / 200 / 401 |
| 容器 | running / **healthy** / restarts 0 / revision 匹配提交 |
| Bot | 容器 ID 与重启次数未变 |
| **镜像已启用并自动预热** | `{"enabled": true, "files": 15, "bytes": 195960, "warm_pending": 976, "warm_failed": 0}` |
| 预热推进（实测三刻） | t+0 37 文件/464KB → t+2m 123/1.5MB → t+4m **233/3.1MB**，`warm_failed=0` |
| 门禁 | `scripts/check.sh --browser` → `project_checks=passed`（Python 1166/1166、浏览器 682 checks、布局 6 视口） |

预计约 20 分钟补齐（976 个待办、约 50 个/分钟）。补齐后每张封面只在本地读；
浏览器侧仍是一小时私有缓存，两者叠加。

## 复审与修复

整支复审（fresh reviewer，`opencode-go/deepseek-v4-pro`，high thinking）结论 "With fixes"：
0 Critical / 4 Important / 10 Minor / Declined 为空。四条 Important 在**一次**修复轮内解决，
每条都先写失败测试：

1. 被归档判定"已消失"的封面每轮都被重探、`warm_pending` 永不归零 → 加进程内按内容寻址的记忆；
2. 单轮异常会整条杀掉预热任务 → `run()` 把失败的一轮当一轮，记日志继续；
3. spec §3.3 的两处节奏（每张 200ms 间隔、`INTERVAL_SECONDS` 是**追赶**节奏）在计划里丢失/反了 → 按 spec 修正；
4. `mirror_candidates` 的 `active=1` 过滤没有测试钉住 → 用真实仓储补一条"退役封面不是候选"。

10 条 Minor 全部记账未修（详见该计划 ledger 的 `Final: minor (deferred)` 行）。
