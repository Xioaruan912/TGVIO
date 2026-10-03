# 封面指纹（dHash）实施计划 —— 阶段 3

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让每个封面带一个 64-bit dHash（worker 顺手算、回填进既有索引、Player 承接并降级），阶段 4 才能做相似聚类。

**Architecture:** 指纹从 worker **已经取到的 32×32 灰度**用整数盒式平均算出（零新依赖、零额外进程）；以**新增可选键**写进 `covers.json`（`schema`/`algorithm` 串不变，否则 Player 拒收整包）；回填走既有续跑路径，但把"有封面无哈希"识别为待做而非已完成；Player 用 `0012` 加可空列，缺失/坏值一律降级为"无相似信息"。

**Tech Stack:** Python 3.11 + ffmpeg（worker 已有）+ SQLite 不可变 migration；测试一律 `PYTHONPATH=src .venv/bin/python -m unittest`。

**Spec:** `docs/superpowers/specs/2026-10-03-player-cover-similarity-phash-design.md`（本文的一切判断以它为准）

## Global Constraints

- 零新依赖：不得引入 Pillow / numpy / 任何图像库。
- 每个视频**不新增 ffmpeg 进程**：复用 `cover_frames.extract()` 里那次 `scale=32:32 -pix_fmt gray` 的 1024 字节。
- `covers.json` 的 `schema`（`tgvio.archive.covers/v2`）与 `algorithm`（`bounded-frame-v2`）**不得改动**；只允许新增可选键 `phash`（16 位小写 hex）。
- 迁移不可变 + checksum，编号紧接 `0011`：本阶段用 `0012`（`media_covers.phash`，可空 TEXT）；**`0013` 不得占用**。
- 降级合同：缺失 / `null` / 大写 / 长度不对 / 非 hex → 视为"无相似信息"；**绝不因为哈希坏而丢弃封面**，也绝不伪造（不写 0、不写全零哈希）。
- 所有新读取路径需登录；未登录必须 401。
- 不改已提交的 manifest / complete marker；不改封面字节路由的语义。
- 回填幂等：第二次运行应为 **0 次取帧**。

## Review Focus

- **回填把已验证的封面重写**：同帧同编码下新 payload 的 sha256 应与索引一致 → **只更新索引**，封面写入次数必须为 0。测试放 Task 3。
- **索引 `schema`/`algorithm` 被顺手升级**：Player 精确比较这两个串，升版会拒收整包封面。测试放 Task 2。
- **坏 `phash` 让整条封面被丢弃**：封面是主体、哈希是附加信息。测试放 Task 4。
- **没有封面的视频凭空多出 `phash` 字段**：DTO 必须逐字节与今天一致。测试放 Task 5。
- **同一视频多条封面行中只有 active 那条算数**：旧包（`active=0`）的哈希不得泄漏到 DTO。测试放 Task 5。

---

### Task 1: 纯函数 dHash（32×32 灰度 → 16 位 hex）

**Files:**
- Modify: `src/tgvio/infrastructure/cover_frames.py`（与 `flat_extreme_frame` 同处：worker 既有的帧级纯函数都住这里）
- Test: `tests/test_cover_phash.py`

**Interfaces:**
- Produces: `dhash_gray32(gray: bytes) -> str` —— 输入必须恰好 1024 字节（32×32 行主序灰度），返回 16 位小写 hex。

- [ ] **Step 1: 写失败测试**

```python
def test_a_left_dark_right_light_frame_hashes_to_the_pinned_value(self):
    # 左半 0、右半 255：每一行 9 个采样列的前 4 个为 0、后 5 个为 255
    gray = bytes(0 if (index % 32) < 16 else 255 for index in range(1024))
    self.assertEqual(dhash_gray32(gray), "0f0f0f0f0f0f0f0f")

def test_one_pixel_of_contrast_sets_exactly_one_bit(self):
    # 让 grid[0][0] > grid[0][1] 成立、其余相邻列相等
    gray = bytearray(128 for _ in range(1024))
    for row in range(0, 4):          # 只有第一行的前 4 行像素更亮
        for column in range(0, 4):
            gray[row * 32 + column] = 200
    self.assertEqual(dhash_gray32(bytes(gray)), "8000000000000000")

def test_the_same_frame_hashes_the_same_way_and_the_length_is_checked(self):
    gray = bytes(range(256)) * 4
    self.assertEqual(dhash_gray32(gray), dhash_gray32(gray))
    with self.assertRaises(ValueError):
        dhash_gray32(gray[:1023])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_cover_phash -v`
Expected: FAIL（`ImportError: cannot import name 'dhash_gray32'`）

- [ ] **Step 3: 实现**

在 `cover_frames.py` 里加常量 `GRAY_EDGE = 32`、`HASH_COLUMNS = 9`、`HASH_ROWS = 8`，并实现：

```python
def _box_mean(gray: bytes, row: int, column: int) -> int:
    rows = range(row * GRAY_EDGE // HASH_ROWS, (row + 1) * GRAY_EDGE // HASH_ROWS)
    columns = range(column * GRAY_EDGE // HASH_COLUMNS, (column + 1) * GRAY_EDGE // HASH_COLUMNS)
    total = sum(gray[r * GRAY_EDGE + c] for r in rows for c in columns)
    return total // (len(rows) * len(columns))
```

`dhash_gray32(gray)`：长度不是 1024 抛 `ValueError`；对 `row∈[0,8)`、`column∈[0,9)` 求 `_box_mean` 得 `grid`；`value = Σ_{row,column<8} (grid[row][column] > grid[row][column+1]) << (63 - (row*8 + column))`；返回 `f"{value:016x}"`。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_cover_phash -v`
Expected: PASS（3/3）

- [ ] **Step 5: 提交**

```bash
git add src/tgvio/infrastructure/cover_frames.py tests/test_cover_phash.py
git commit -m "feat(covers): a fingerprint from the frame the worker already sampled"
```

### Task 2: worker 把帧与哈希一起返回，并写进索引

**Files:**
- Modify: `src/tgvio/infrastructure/cover_frames.py`（`extract` / `sample`）
- Modify: `src/tgvio/adapters/cover_archive.py`（`sample`）
- Modify: `src/tgvio/application/cover_backfill_runner.py`（端口 Protocol + 条目）
- Test: `tests/test_cover_frame_detail.py`、`tests/test_committed_cover_backfill.py`

**Interfaces:**
- Consumes: Task 1 的 `dhash_gray32`。
- Produces: `SampledFrame(payload: bytes, phash: str)`（`cover_frames.py` 内的 frozen dataclass）；`cover_frames.extract(...) -> SampledFrame | None`、`cover_frames.sample(...) -> SampledFrame | None`、`CommittedCoverPort.sample(...) -> SampledFrame | None`；索引条目新增键 `"phash"`（值为 16 位小写 hex）。

- [ ] **Step 1: 写失败测试**（在 `tests/test_committed_cover_backfill.py` 的假 port 上）

```python
async def test_the_index_entry_carries_the_frame_fingerprint(self):
    ...
    self.assertEqual(entry["phash"], "0123456789abcdef")   # 假 port 返回的已知哈希
    self.assertEqual(index["schema"], "tgvio.archive.covers/v2")
    self.assertEqual(index["algorithm"], "bounded-frame-v2")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_committed_cover_backfill -v`
Expected: FAIL（条目里没有 `phash`）

- [ ] **Step 3: 实现**

- `cover_frames.extract()`：灰度拿到后先算 `phash = dhash_gray32(gray)`（`flat_extreme_frame(gray)` 判定不变），返回 `SampledFrame(payload, phash)`。
- `cover_frames.sample()` / `cover_archive.sample()` / 端口 Protocol 同步改成返回 `SampledFrame | None`。
- `CommittedCoverBackfill.run()`：`payload = frame.payload`，条目字典加 `"phash": frame.phash`。
- **不动** `cover_backfill.py` 的 v1 路径（`BackfillCover`/`backfill-head-frame-v1`）：它写的索引 Player 本来就不接受，本阶段不给它加字段。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_cover_frame_detail tests.test_committed_cover_backfill -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/tgvio/infrastructure/cover_frames.py src/tgvio/adapters/cover_archive.py src/tgvio/application/cover_backfill_runner.py tests/test_cover_frame_detail.py tests/test_committed_cover_backfill.py
git commit -m "feat(covers): the backfill writes the fingerprint it just computed"
```

### Task 3: 续跑把"有封面无哈希"当成待做（幂等回填）

**Files:**
- Modify: `src/tgvio/application/cover_backfill_runner.py`（`run()` 的提前返回分支）
- Test: `tests/test_committed_cover_backfill.py`

**Interfaces:**
- Consumes: Task 2 的 `SampledFrame` 与条目 `phash`。
- Produces: 无新名字；行为合同：条目**有封面且 `phash` 合法**才 `return 0`。

- [ ] **Step 1: 写失败测试**

```python
async def test_a_cover_without_a_fingerprint_is_resampled_and_not_rewritten(self):
    # 索引里已有条目（path/sha256/size/mime 全对、文件存在），但没有 phash
    ...
    self.assertEqual(runner.samples, 1)        # 取了一次帧
    self.assertEqual(runner.writes, [])        # 封面一次都没重写（新帧 sha 与索引一致）
    self.assertEqual(entry["phash"], "...")    # 索引补上了哈希
    self.assertEqual(entry["path"], "cover/backfill/<原 sha>.jpg")

async def test_a_cover_that_already_has_a_fingerprint_is_left_alone(self):
    ...
    self.assertEqual(runner.samples, 0)
    self.assertEqual(runner.writes, [])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_committed_cover_backfill -v`
Expected: FAIL（无 phash 的条目被当成已完成，`samples == 0`）

- [ ] **Step 3: 实现**

提前返回的条件从「条目验证通过」改为「条目验证通过 **且** `existing.get("phash")` 是 16 位小写 hex」；补哈希的路径：取帧 → 若新 `payload` 的 sha256 等于 `existing["sha256"]` → **只写索引**（沿用原 `path`/`size_bytes`/`sha256`/`mime_type`，加 `phash`）；否则按既有「源被替换」路径写新封面并更新条目。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_committed_cover_backfill -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/tgvio/application/cover_backfill_runner.py tests/test_committed_cover_backfill.py
git commit -m "feat(covers): backfilling a fingerprint never rewrites a verified cover"
```

### Task 4: Player 迁移 `0012` 与摄入

**Files:**
- Create: `src/tgvio_player/infrastructure/migrations/0012_media_covers_phash.sql`
- Modify: `src/tgvio_player/domain/catalog.py`（`CatalogCover`）、`src/tgvio_player/application/cover_sidecar.py`（`parse_covers`）、`src/tgvio_player/infrastructure/sqlite.py`（`apply_package` 的 cover INSERT）
- Modify: `tests/test_player_migration.py`（硬编码的迁移尖端）
- Test: `tests/test_player_cover_sidecars.py`、`tests/test_player_migration.py`

**Interfaces:**
- Produces: `CatalogCover.phash: str | None = None`；`media_covers.phash TEXT`（可空）；`PHASH_RE = ^[0-9a-f]{16}$`。

- [ ] **Step 1: 写失败测试**

```python
async def test_a_cover_keeps_its_fingerprint_and_survives_a_broken_one(self):
    # 合法 phash → 落库；缺失 / null / 大写 / 15 位 / 含 "g" → NULL，且封面仍在
    ...

def test_fresh_database_uses_the_public_baseline_only(self):
    ...  # 尖端断言加入 (12, "media_covers_phash")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_player_cover_sidecars tests.test_player_migration -v`
Expected: FAIL（列不存在 / 尖端仍是 11）

- [ ] **Step 3: 实现**

- 迁移文件：`PRAGMA foreign_keys=ON;` + `ALTER TABLE media_covers ADD COLUMN phash TEXT;`（注释写明：可空、缺失即"无相似信息"、不可变）。
- `CatalogCover` 末尾加 `phash: str | None = None`。
- `parse_covers`：读 `entry.get("phash")`，**只有**匹配 16 位小写 hex 才带上，否则 `None` —— 任何情况下都**不** `continue` 丢弃该封面。
- `apply_package`：INSERT 与 ON CONFLICT 都带上 `phash`（`cover.phash`）。
- `tests/test_player_migration.py`：尖端与表断言同步到 0012（与 0011 那次同样的三处）。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_player_cover_sidecars tests.test_player_migration -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/tgvio_player/infrastructure/migrations/0012_media_covers_phash.sql src/tgvio_player/domain/catalog.py src/tgvio_player/application/cover_sidecar.py src/tgvio_player/infrastructure/sqlite.py tests/test_player_cover_sidecars.py tests/test_player_migration.py
git commit -m "feat(player): a cover may carry a fingerprint, and may not"
```

### Task 5: Player 读出（`active_cover` + DTO）

**Files:**
- Modify: `src/tgvio_player/infrastructure/sqlite.py`（`active_cover` 的 SELECT）、`src/tgvio_player/adapters/http/media.py`（`_media_dto`）
- Test: `tests/test_player_http.py`

**Interfaces:**
- Consumes: Task 4 的列。
- Produces: `active_cover()` 返回值新增键 `"phash"`（`str | None`）；媒体 DTO 可选字段 `phash`。

- [ ] **Step 1: 写失败测试**

```python
async def test_the_dto_carries_a_fingerprint_only_when_the_cover_has_one(self):
    # 有封面且有哈希 → item["phash"] == "0123456789abcdef"
    # 有封面无哈希 → "phash" not in item
    # 无封面 → "phash" not in item
async def test_a_retired_cover_row_never_leaks_its_fingerprint(self):
    # 同一 media 两个包：active=0 的那条即使有哈希，DTO 也只能反映 active 那条
async def def_the_cover_route_requires_a_session(self):  # 与既有 401 断言同处
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_player_http -v -k phash`
Expected: FAIL（DTO 里没有 `phash`）

- [ ] **Step 3: 实现**

- `active_cover` 的 SELECT 加 `mc.phash`，返回值加 `"phash": row[...]`（`str | None`）。
- `_media_dto`：封面存在且 `phash` 非空时才在返回字典里加 `"phash"`（用条件展开，缺省即不出现）。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_player_http`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/tgvio_player/infrastructure/sqlite.py src/tgvio_player/adapters/http/media.py tests/test_player_http.py
git commit -m "feat(player): serve the fingerprint with the cover it belongs to"
```

### Task 6: 文档与运维口径

**Files:**
- Modify: `docs/development/COVER_SUPPLY.md`
- Test: 无（文档；由 `tests/test_repository_hygiene.py` 的链接门禁间接校验）

**Interfaces:**
- Produces: 无代码接口。

- [ ] **Step 1: 写文档**：covers.json 契约一节补 `phash`：可选、16 位小写 hex、缺失含义（"无相似信息"）、**为什么不动 schema/algorithm 串**（Player 精确比较）、以及回填操作（重跑既有 covers 回填命令即可，幂等、第二次 0 次取帧）。
- [ ] **Step 2: 跑门禁**：`PYTHONPATH=src .venv/bin/python scripts/repository_hygiene.py` → Expected: passed
- [ ] **Step 3: 提交**：`git add docs/development/COVER_SUPPLY.md && git commit -m "docs(covers): the fingerprint is an optional field, not a new schema"`

---

## Self-Review

- **Spec coverage**：§3.1 → Task 1；§3.2 → Task 2；§3.3 → Task 3；§3.4 → Task 4/5；§3.5 → Task 6；§5 的测试口径逐条落在对应任务的 Step 1。
- **Step scan**：每个任务都是「失败测试 → 确认失败 → 最小实现 → 确认通过 → 提交」，实现步骤只给签名与必须固定的值。
- **Type consistency**：`SampledFrame(payload, phash)` 在 Task 2 定义、Task 3 消费；`dhash_gray32` 在 Task 1 定义、Task 2 消费；`CatalogCover.phash` 在 Task 4 定义、Task 5 消费；键名 `phash` 在 worker 索引、Player 列、DTO 三处同名。
- **Review Focus**：五条都已在对应任务的 Step 1 里排了测试。
- **Proportion**：计划与 spec 体量相当，无函数体转写（只有 Task 1 的 `_box_mean` 给出了边界公式，因为取整方式决定位级结果）。
