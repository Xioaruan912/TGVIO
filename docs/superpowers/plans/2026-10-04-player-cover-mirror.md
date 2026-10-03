# Player 本地封面镜像实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让每张封面在首次读取后只从 VPS 本地盘提供，从而消除"封面一直在转"的根因（每张首次封面都是一次到 WebDAV 的往返）。

**Architecture:** 在封面字节路由前面加一层内容寻址的本地镜像（`<sha256>.jpg`），并在 Player 进程内跑一个有界的后台预热循环，把目录里"有封面但本地没有副本"的行自动补齐；镜像命中不占用封面并发预算，预热并发不超过该预算的一半；镜像不可用时完全退回今天的行为。

**Tech Stack:** Python 3.11 + aiohttp + SQLite（无新迁移）；测试 `PYTHONPATH=src .venv/bin/python -m unittest`。

**Spec:** `docs/superpowers/specs/2026-10-03-player-cover-mirror-design.md`

## Global Constraints

- 镜像目录：`${TGVIO_PLAYER_DATA_DIR}/covers/`，文件名 `<64位小写hex>.jpg`；**落在已有 bind mount 内**，不改 `docker-compose.player.yml`、不加卷、不加容器。
- key 只来自目录元数据 `media_covers.remote_relpath` 的 basename；不匹配 `^[0-9a-f]{64}\.jpg$` 即**不镜像**（仍按今天的方式直读上游），绝不接受调用方提供的路径。
- 写入必须**临时文件 + `os.replace`**：半张图永远不以正式名出现。
- 鉴权（未登录 401）、`?v=` 版本语义、`private, max-age=3600` + `Vary: Cookie`、无 `v=` 时的 `no-store`、响应体/`Content-Type`/`Content-Length`/`Content-Disposition`、封面并发预算 `_acquire_cover` 及其 fail-fast 语义**全部不变**；镜像命中**不**占用该预算。
- 默认值：磁盘预算 `268435456`（256 MiB）、单轮 `64`、并发 `2`（且 ≤ `_max_cover // 2`）、空闲间隔 `900` 秒、backlog 追赶间隔 `30` 秒。
- `TGVIO_PLAYER_COVER_MIRROR=off` → 读路径与预热循环都不启动，行为与今天逐字一致。
- 私密内容不出本机：镜像只存在于 VPS 本地盘，不经第三方、不外发。
- 单文件 ≤1000 行（`release_guard architecture`）；`scripts/check.sh` 必须通过。

## Review Focus

1. **退役封面行不得被服务**：预热只取 `active=1` 的行，读路径先经 `active_cover()`；退役行的字节不得被镜像服务。
2. **半张图不得被服务**：rename 失败或中断后，正式名不存在；读路径仍走上游。
3. **不得新增匿名读路径**：镜像只通过已鉴权的 `/cover` 处理器对外。
4. **预热不得与播放抢上游**：并发 ≤ 封面预算一半、有单轮上限、预算满即停。
5. **`off` 必须与今天逐字一致**：同一组断言在关闭态下重跑。

---

### Task 1: 镜像存储（key、原子写、预算清理）

**Files:**
- Create: `src/tgvio_player/infrastructure/cover_mirror.py`
- Test: `tests/test_player_cover_mirror.py`

**Interfaces:**
- Produces:
  - `CoverMirror(root: Path, budget_bytes: int)`
  - `key_for(remote_relpath: str) -> str | None`（返回 64 位小写 hex，或 `None`）
  - `path_for(key: str) -> Path`（`root/<key>.jpg`）
  - `exists(key: str) -> bool`
  - `has(key: str, size: int) -> bool`（存在且**大小等于目录声明值**）
  - `read(key: str) -> bytes | None`
  - `write(key: str, payload: bytes) -> None`
  - `stats() -> tuple[int, int]`（文件数、字节数）
  - `sweep(keep: Collection[str]) -> int`（返回删除数）
  - `CoverMirrorCounters`（`hits`、`misses`、`write_failed`、`warm_pending`、`warm_failed` 五个整数，供路由与 `/healthz` 共用）

- [ ] **Step 1: 写失败测试**

```python
import os, unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tgvio_player.infrastructure.cover_mirror import CoverMirror, CoverMirrorCounters

DIGEST = "a" * 64
RELPATH = f"cover/backfill/{DIGEST}.jpg"


class CoverMirrorStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "covers"

    def test_only_a_content_addressed_name_is_a_key(self):
        mirror = CoverMirror(self.root, budget_bytes=1024)
        self.assertEqual(mirror.key_for(RELPATH), DIGEST)
        for bad in ["cover/backfill/cover.jpg", f"cover/backfill/{DIGEST.upper()}.jpg",
                    f"cover/backfill/{'a' * 63}.jpg", "cover/backfill/../../etc/passwd", ""]:
            self.assertIsNone(mirror.key_for(bad), bad)

    def test_a_hit_needs_the_declared_size(self):
        mirror = CoverMirror(self.root, budget_bytes=1024)
        mirror.write(DIGEST, b"jpeg-bytes")
        self.assertTrue(mirror.has(DIGEST, 10))
        self.assertFalse(mirror.has(DIGEST, 11), "a size the catalog does not declare is not a hit")
        self.assertFalse(mirror.has("b" * 64, 10), "a missing key is never a hit")

    def test_a_failed_rename_leaves_no_servable_file(self):
        mirror = CoverMirror(self.root, budget_bytes=1024)
        with patch("tgvio_player.infrastructure.cover_mirror.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                mirror.write(DIGEST, b"jpeg-bytes")
        self.assertFalse(mirror.exists(DIGEST), "a half-written cover must never be servable")
        self.assertEqual([p.name for p in self.root.iterdir() if p.suffix == ".jpg"], [])

    def test_the_sweep_drops_orphans_then_the_oldest_over_budget(self):
        mirror = CoverMirror(self.root, budget_bytes=30)
        keep, orphan, fresh = "a" * 64, "b" * 64, "c" * 64
        mirror.write(keep, b"x" * 10); mirror.write(orphan, b"y" * 10)
        os.utime(mirror.path_for(keep), (1000, 1000))
        self.assertEqual(mirror.sweep({keep}), 1, "an orphan goes first")
        self.assertTrue(mirror.exists(keep))
        mirror.write(fresh, b"z" * 30)
        self.assertEqual(mirror.sweep({keep, fresh}), 1, "over budget, the oldest goes")
        self.assertFalse(mirror.exists(keep))
        self.assertTrue(mirror.exists(fresh))

    def test_stats_counts_only_mirror_files(self):
        mirror = CoverMirror(self.root, budget_bytes=1024)
        mirror.write(DIGEST, b"x" * 7)
        (self.root / "notes.txt").write_text("not a cover")
        self.assertEqual(mirror.stats(), (1, 7))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_player_cover_mirror -v`
Expected: FAIL（`ModuleNotFoundError: tgvio_player.infrastructure.cover_mirror`）

- [ ] **Step 3: 实现 `cover_mirror.py`**

`write` 用同目录临时文件（`tempfile.mkstemp(dir=root, prefix=".tmp-")`）写全、`flush` + `os.fsync`、再 `os.replace` 到正式名；`sweep` 先删不在 `keep` 里的 `*.jpg`，再按 `st_mtime` 从旧到新删到不超过 `budget_bytes`。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_player_cover_mirror -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/tgvio_player/infrastructure/cover_mirror.py tests/test_player_cover_mirror.py
git commit -m "feat(player): a content-addressed cover mirror on local disk"
```

---

### Task 2: 封面路由先读本地镜像

**Files:**
- Modify: `src/tgvio_player/adapters/http/streaming.py`（`_cover`）
- Modify: `src/tgvio_player/adapters/http/server.py`（接受可选 `cover_mirror` 与 `cover_mirror_counters`）
- Test: `tests/test_player_http.py`（`PlayerCoverRouteTests`）

**Interfaces:**
- Consumes: `CoverMirror`、`CoverMirrorCounters`（Task 1）
- Produces: 路由行为——命中本地直接发（不读上游、不占封面预算）；未命中读上游并顺手写镜像；上游失败但有镜像 → 发镜像；无镜像 → 与今天相同的 404/502。

- [ ] **Step 1: 写失败测试**

```python
    async def test_a_mirrored_cover_is_served_without_touching_the_archive(self):
        mirror = CoverMirror(self.mirror_root, budget_bytes=1024 * 1024)
        mirror.write(self.cover_digest, self.JPEG)
        server = self.server_with_mirror(mirror)
        cookie = await self._login()
        response = await self.client.get(f"/api/v1/media/{self.media_id}/cover?v={self.version}",
                                         cookies={"tgvio_player_session": cookie})
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), self.JPEG)
        self.assertEqual(self.read_client.calls, [], "a mirror hit costs no upstream round trip")
        self.assertEqual(mirror.stats()[0], 1)

    async def test_a_miss_reads_upstream_and_fills_the_mirror(self):
        mirror = CoverMirror(self.mirror_root, budget_bytes=1024 * 1024)
        await self._request_cover()                       # 第一次：走上游
        self.assertEqual(len(self.read_client.calls), 1)
        self.assertTrue(mirror.has(self.cover_digest, len(self.JPEG)))
        await self._request_cover()                       # 第二次：本地
        self.assertEqual(len(self.read_client.calls), 1, "the second request never left the host")

    async def test_an_upstream_failure_still_serves_a_mirrored_cover(self):
        mirror = CoverMirror(self.mirror_root, budget_bytes=1024 * 1024)
        mirror.write(self.cover_digest, self.JPEG)        # 大小与目录声明不同 → 不是 has()，但 exists()
        self.read_client.fail = OSError("archive down")
        response = await self._request_cover()
        self.assertEqual(response.status, 200, "the local copy answers when the archive cannot")
        self.assertEqual(await response.read(), self.JPEG)

    async def test_an_unreadable_key_keeps_todays_behaviour(self):
        mirror = CoverMirror(self.mirror_root, budget_bytes=1024 * 1024)
        self.assertIsNone(mirror.key_for("cover/backfill/not-a-digest.jpg"))
        response = await self._request_cover()            # 与今天一致：一次上游读取
        self.assertEqual(response.status, 200)
        self.assertEqual(len(self.read_client.calls), 1)
        self.assertEqual(mirror.stats(), (0, 0), "an unkeyable cover is never mirrored")

    async def test_the_mirror_off_is_exactly_today(self):
        server = self.server_with_mirror(None)            # 关闭态
        self.assertEqual((await self._request_cover()).status, 200)
        self.assertEqual(len(self.read_client.calls), 1)
```

（`_request_cover`、`server_with_mirror`、`cover_digest`、`mirror_root` 是本类的新测试助手；`self.JPEG`、`self.read_client`、`self.version` 沿用本类既有夹具。）

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_player_http.PlayerCoverRouteTests -v -k mirror`
Expected: FAIL（镜像未接线：命中本地时仍会读到上游）

- [ ] **Step 3: 实现**

在 `_cover` 里 `active_cover()` 与 `?v=` 校验之后：取 `key = mirror.key_for(cover["remote_relpath"])`；`has(key, length)` 为真则直接发本地字节（相同的三个响应头），命中计数 +1 并**跳过** `_acquire_cover`。未命中则走今天的路径，并在字节完整读出后 `mirror.write(key, payload)`（写失败只记 `write_failed`，不影响本次响应）。上游抛错时若 `mirror.exists(key)` 为真，发本地字节。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_player_http -v`
Expected: PASS（既有封面测试全部保持绿）

- [ ] **Step 5: 提交**

```bash
git add src/tgvio_player/adapters/http/streaming.py src/tgvio_player/adapters/http/server.py tests/test_player_http.py
git commit -m "feat(player): the cover route answers from the local mirror first"
```

---

### Task 3: 有界自动预热

**Files:**
- Create: `src/tgvio_player/infrastructure/sqlite_cover_mirror.py`（仓储 mixin，`sqlite.py` 已 972/1000 行）
- Modify: `src/tgvio_player/infrastructure/sqlite.py`（把 mixin 组合进仓储类）
- Modify: `src/tgvio_player/application/ports.py`（声明 `mirror_candidates`）
- Create: `src/tgvio_player/application/cover_warm.py`
- Test: `tests/test_player_cover_warm.py`

**Interfaces:**
- Consumes: `CoverMirror`、`CoverMirrorCounters`（Task 1）；`PlayerMediaReader.open_range(path, relpath, byte_range)`
- Produces:
  - 仓储：`async def mirror_candidates(self, limit: int) -> Sequence[tuple[str, str, int]]`（`(media_id, remote_relpath, size_bytes)`，只取 `mc.active=1 AND media.active=1`，按 `media_id` 升序）
  - `CoverWarm(repository, reader, mirror, counters, *, batch: int, concurrency: int, idle_seconds: int, sleep=asyncio.sleep)`，方法 `async def run_once() -> CoverWarmRun` 与 `async def run(stop: asyncio.Event) -> None`
  - `CoverWarmRun`（`fetched`、`skipped`、`failed` 三个整数）

- [ ] **Step 1: 写失败测试**

```python
import asyncio, unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tgvio_player.application.cover_warm import CATCH_UP_SECONDS, CoverWarm
from tgvio_player.infrastructure.cover_mirror import CoverMirror, CoverMirrorCounters

DIGESTS = [f"{n:064x}" for n in range(1, 9)]
ROWS = [(f"media-{n}", f"cover/backfill/{d}.jpg", 8) for n, d in enumerate(DIGESTS)]


class CoverWarmTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.mirror = CoverMirror(Path(self.tmp.name) / "covers", budget_bytes=1024)
        self.counters = CoverMirrorCounters()
        self.reads: list[str] = []
        self.fail: dict[str, int] = {}
        self.sleeps: list[float] = []
        self.peak = 0
        self.active = 0

    async def test_only_covers_missing_locally_are_fetched(self):
        self.mirror.write(DIGESTS[0], b"12345678")           # 已有本地副本
        run = await self.warm().run_once()
        self.assertEqual(run.fetched, len(ROWS) - 1, "the mirrored row is not fetched again")
        self.assertNotIn(ROWS[0][0], self.reads)

    async def test_concurrency_never_exceeds_the_lane_limit(self):
        run = await self.warm(concurrency=2).run_once()
        self.assertEqual(run.fetched, len(ROWS))
        self.assertLessEqual(self.peak, 2, "the warm loop never opens more lanes than allowed")

    async def test_a_transient_failure_is_retried_then_skipped(self):
        self.fail[DIGESTS[1]] = 1                              # 第一次失败，第二次成功
        self.fail[DIGESTS[2]] = 99                             # 一直失败 → 本轮跳过
        run = await self.warm().run_once()
        self.assertEqual(run.fetched, len(ROWS) - 1)
        self.assertEqual(run.failed, 1)
        self.assertTrue(self.mirror.exists(DIGESTS[1]))

    async def test_a_missing_source_is_tried_once(self):
        self.fail[DIGESTS[3]] = 99
        self.reader.missing.add(DIGESTS[3])                     # 永久 404
        await self.warm().run_once()
        self.assertEqual(self.reads.count(ROWS[3][0]), 1, "a permanent failure is not hammered")

    async def test_the_run_stops_at_the_disk_budget(self):
        self.mirror = CoverMirror(Path(self.tmp.name) / "small", budget_bytes=16)
        run = await self.warm().run_once()
        self.assertLessEqual(run.fetched, 2, "two 8-byte covers fill a 16-byte budget")

    async def test_the_cadence_chases_a_backlog_and_backs_off_when_idle(self):
        stop = asyncio.Event()
        warm = self.warm(idle_seconds=900)
        task = asyncio.create_task(warm.run(stop))
        await asyncio.sleep(0)
        for _ in range(3):
            await asyncio.sleep(0)
        stop.set(); await task
        self.assertIn(CATCH_UP_SECONDS, self.sleeps, "a backlog is chased")
        self.assertIn(900, self.sleeps, "an empty pass backs off")
```

（`warm()` 组装被测对象；`reader` 是记录 `reads`、按 `fail` 抛错、把 `missing` 当 404 的假实现；`sleep` 收集间隔并让出事件循环。）

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_player_cover_warm -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

仓储 mixin 只加一条 SELECT（`mc.active=1 AND media.active=1 AND media.kind='video'`）。预热循环每轮：取 `batch` 行 → 跳过已 `has()` 的 → 以 `concurrency` 个并发拉取（每张走 `reader.open_range(remote_path, remote_relpath, ByteRange(0, size-1))`，写完 `mirror.write`）→ 单张最多 2 次尝试（退避 1s / 4s），404 只试一次 → 每轮结束调 `mirror.sweep(keep)`，预算满则停止本轮 → 本轮仍有缺失则睡 `CATCH_UP_SECONDS`，否则睡 `idle_seconds`。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_player_cover_warm -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/tgvio_player/application/cover_warm.py src/tgvio_player/infrastructure/sqlite_cover_mirror.py \
        src/tgvio_player/infrastructure/sqlite.py src/tgvio_player/application/ports.py tests/test_player_cover_warm.py
git commit -m "feat(player): the mirror warms itself, bounded and resumable"
```

---

### Task 4: 配置、计数与接线

**Files:**
- Modify: `src/tgvio_player/main.py`（`Settings` 五个字段 + 启动预热任务）
- Modify: `src/tgvio_player/adapters/http/server.py`（`/healthz` 增 `cover_mirror`）
- Test: `tests/test_player_backend.py`（配置解析）、`tests/test_player_http.py`（health 形状）、`tests/test_player_runtime.py`（任务随应用启动/停止）

**Interfaces:**
- Consumes: Task 1–3 的全部构件
- Produces: 环境变量 `TGVIO_PLAYER_COVER_MIRROR`（`on`/`off`，默认 `on`）、`TGVIO_PLAYER_COVER_MIRROR_BYTES`（默认 `268435456`）、`TGVIO_PLAYER_COVER_MIRROR_BATCH`（默认 `64`）、`TGVIO_PLAYER_COVER_MIRROR_CONCURRENCY`（默认 `2`）、`TGVIO_PLAYER_COVER_MIRROR_INTERVAL_SECONDS`（默认 `900`）；`/healthz` 的 `stream_capacity` 旁新增 `cover_mirror: {enabled, files, bytes, hits, misses, warm_pending, warm_failed}`。

- [ ] **Step 1: 写失败测试**

```python
    def test_the_mirror_settings_default_on_and_reject_junk(self):
        settings = Settings.from_env({"TGVIO_PLAYER_ENABLED": "true", ...})   # 沿用本文件既有构造方式
        self.assertTrue(settings.cover_mirror)
        self.assertEqual(settings.cover_mirror_bytes, 268435456)
        self.assertEqual(settings.cover_mirror_batch, 64)
        self.assertEqual(settings.cover_mirror_concurrency, 2)
        self.assertEqual(settings.cover_mirror_interval_seconds, 900)
        off = Settings.from_env({..., "TGVIO_PLAYER_COVER_MIRROR": "off"})
        self.assertFalse(off.cover_mirror)
        junk = Settings.from_env({..., "TGVIO_PLAYER_COVER_MIRROR_BYTES": "0",
                                  "TGVIO_PLAYER_COVER_MIRROR_BATCH": "abc"})
        self.assertEqual(junk.cover_mirror_bytes, 268435456, "a value off the list falls back")
        self.assertEqual(junk.cover_mirror_batch, 64)

    async def test_health_reports_the_mirror(self):
        body = await (await self.client.get("/healthz")).json()
        self.assertIn("cover_mirror", body["stream_capacity"])
        self.assertEqual(body["stream_capacity"]["cover_mirror"]["enabled"], True)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_player_backend tests.test_player_http -k mirror -v`
Expected: FAIL（无 `cover_mirror` 字段）

- [ ] **Step 3: 实现**

`Settings` 加五个字段与校验（非法值回落默认，`bytes`/`batch`/`concurrency` 必须 > 0，`concurrency` 再被 `max(1, max_streams // 2)` 夹住）；`main.py` 在 `settings.cover_mirror` 为真时创建镜像与计数对象，把预热任务加进既有的 `tasks` 列表（与 `warm.run(stop)` 同处），并在 `stop` 时随其它任务一起取消；`/healthz` 从计数对象与 `mirror.stats()` 汇总。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_player_backend tests.test_player_http tests.test_player_runtime -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/tgvio_player/main.py src/tgvio_player/adapters/http/server.py tests/test_player_backend.py tests/test_player_http.py tests/test_player_runtime.py
git commit -m "feat(player): the mirror is a setting, and it reports what it did"
```

---

### Task 5: 发布（Player-only 通道）

**Files:**
- Create: `docs/operations/2026-10-04-player-cover-mirror-release.md`

**Interfaces:**
- Consumes: 前四个任务的提交
- Produces: 线上可观测的 `cover_mirror` 计数与本地镜像目录

- [ ] **Step 1: 全仓门禁**

Run: `bash scripts/check.sh --browser`
Expected: `project_checks=passed`（布局夹具 + 封面回归 + Python 全量）

- [ ] **Step 2: 构建并传输候选镜像**

Run: `bash scripts/player_release.sh --tag tgvio-player:cover-mirror-<short> --commit <full> --release-id cover-mirror-<short>`
然后 `docker save | gzip` → 核对两端 SHA-256 → VPS `docker load` → 核对 `org.opencontainers.image.revision`。

- [ ] **Step 3: 单次受控切换**

先记录回滚点（现容器 ID、现镜像 ID、`player.sqlite3` 副本、`player.env` 副本），再把 `TGVIO_PLAYER_IMAGE` 指向新镜像并运行
`bash scripts/player_deploy.sh --env-file /root/tgvio-player/player.env --execute`（脚本自带 Bot/Player 邻居不变校验）。

- [ ] **Step 4: 上线后验**

- `/healthz` 200 且 `stream_capacity.cover_mirror.enabled` 为真；
- 未登录 `/api/v1/feed` 仍 401；
- 线上资源与候选镜像逐字节一致；
- 容器 healthy / restarts 0，Bot 容器 ID 与重启次数不变；
- 预热推进：`cover_mirror.files` 随时间上涨，直到接近活跃封面总数（实测 956 个文件、12.5 MiB）；
- 抽查一次封面请求：第二次请求不再产生上游往返（`cover_mirror.hits` 增长、`cover_mirror.misses` 不变）。

- [ ] **Step 5: 记录并提交**

把发布事实（提交、release id、镜像 ID、传输包 SHA-256、回滚点、后验数字）写进上面的运维记录并提交。

```bash
git add docs/operations/2026-10-04-player-cover-mirror-release.md
git commit -m "docs(player): record the cover mirror release"
```

---

## Self-Review

- **Spec coverage**：§3.1 存储与 key → Task 1；§3.2 读路径（命中/未命中/回退/不变合同）→ Task 2；§3.3 自动预热（含"浏览即预热"由 Task 2 的写透承担、新视频由 Task 3 的下一轮发现）→ Task 3；§3.4 失败处理 → Task 3；§3.5 预算与清理 → Task 1 + Task 3；§3.6 可观测 → Task 4；§3.7 开关 → Task 4；§4 不变合同 → Task 2 的断言 + Global Constraints；§5 测试口径 11 条 → 分布在 Task 1–4；§6 运维与回滚 → Task 5。
- **Step scan**：每个任务都是"写失败测试 → 确认失败 → 最小实现 → 确认通过 → 提交"；未出现"处理边界情况"这类不决定任何事的步骤。
- **Type consistency**：`CoverMirror` 的 `key_for`/`has`/`exists`/`read`/`write`/`stats`/`sweep` 在 Task 2/3 中按同一签名使用；`CoverMirrorCounters` 的五个字段名在 Task 2/3/4 中一致；`mirror_candidates` 的三元组顺序在 Task 3 内自洽。
- **Review Focus**：五条各落在对应任务——① 退役行 → Task 2 的 `test_a_retired_cover_row_*`（沿用本类既有夹具）；② 半张图 → Task 1 的 `test_a_failed_rename_leaves_no_servable_file`；③ 匿名路径 → Task 2 沿用本类既有的 401 断言；④ 不抢上游 → Task 3 的并发与预算断言；⑤ 关闭即等价 → Task 2 的 `test_the_mirror_off_is_exactly_today` + Task 4 的 `enabled: false`。
- **Proportion**：计划短于 spec；代码块只出现在测试与必须固定的算法处，实现步骤给签名与取值，不转写函数体。
