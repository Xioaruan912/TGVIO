# Player 封面优先找片与集合实施计划（阶段 1+2）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 不记得日期也能靠封面找到某个视频，并能用自己的多对多集合组织它们。

**Architecture:** 筛选/排序条件是一个可序列化的纯值对象，先在后端校验并落到 `_videos` 查询，阶段 2 的智能集合直接复用同一结构。集合是新表（`collections` + `collection_items`），`favorites` 不动、作为内置只读集合参与展示。前端新增两个独立模块（筛选结构、集合控制器），UI 落在现有片库/收藏页的工具栏与 `sheet` 上，帧墙复用已上线的密度三档与 6 通道封面预算。

**Tech Stack:** Python 3.11 + aiohttp + SQLite（不可变 migration）；Vite + 严格 TypeScript（无框架）；Node `--test` + 现有 CDP 浏览器 fixture。

**Spec:** `docs/superpowers/specs/2026-10-03-player-visual-retrieval-and-collections-design.md`

## Global Constraints

- 列表阶段不创建 `<video>`；浏览界面自身不发起媒体请求。
- 封面三态（loading/missing/failed+可重试）必须继续可区分；沿用 `cover-image` 的观察者、20 秒期限、400/1200ms 退避与 **6 通道预算**；**不得**为帧墙开第二条封面并发通路。
- 每个分页请求都有预算与取消；上下文切换后旧结果不得写入新页面（沿用 generation/AbortController）。
- 智能集合求值、集合列表、帧墙分页都必须有明确上限；禁止无界全表扫描。
- 隐私合同：闲置 60 秒会**结束会话**回到口令页；筛选与集合面板不得被写成闲置豁免。
- 无障碍：面板走 `sheet` 的焦点陷阱 / Escape / 原焦点恢复；隐藏控件必须同时退出指针命中与键盘焦点。
- 行数预算：新前端模块 ≤600 行；`main.ts` ≤1710；`large.ts` ≤682（只许缩小）。
- migration 不可变 + checksum；阶段 2 用 `0011`（`0012` 留给阶段 3 的 `phash`）。
- 条件里的日期区间存**绝对时间戳**（秒），不得存相对区间。
- 所有新 API 需登录；未登录必须 401。

## Review Focus

- **分页期间切换筛选/集合**：旧请求返回后不得写入新上下文（generation/abort）。测试放在 Task 6。
- **视频被永久删除后的集合成员与计数**：依赖外键级联，必须实测删除路径而不是假定。测试放在 Task 3。
- **非法或空的智能集合条件**（坏 JSON、未知键、空对象）：必须视为空集合，不得 500，也不得退化成"全部视频"。测试放在 Task 1。
- **筛选面板打开时闲置 60 秒**：会话结束后面板不得留下悬空 sheet。测试放在 Task 7。
- **缺封面条目**进入帧墙与集合时必须显示 `missing` 三态之一，不得伪造图片。测试放在 Task 8。

---

### Task 1: 筛选与排序值对象（domain）

**Files:**
- Create: `src/tgvio_player/domain/library_filters.py`
- Test: `tests/test_player_library_filters.py`

**Interfaces:**
- Produces: `parse_filters(query: Mapping[str, str]) -> LibraryFilters`、`LibraryFilters`（字段 `categories: frozenset[str]`、`date_from: int | None`、`date_to: int | None`、`min_seconds: float | None`、`max_seconds: float | None`、`min_bytes: int | None`、`max_bytes: int | None`、`has_cover: bool | None`、`favorite: bool | None`、`resumable: bool | None`、`unwatched: bool | None`、`sort: str`、`seed: int | None`）、`InvalidFilters(ValueError)`。`sort` 取值 `newest|longest|largest|random|resume`。

- [ ] **Step 1: 写失败测试**

```python
def test_unknown_filter_key_is_rejected():
    with pytest.raises(InvalidFilters):
        parse_filters({"colour": "red"})

def test_empty_and_malformed_smart_rules_are_an_empty_selection():
    assert parse_rules(None) == LibraryFilters.empty()
    assert parse_rules("{") == LibraryFilters.empty()
    assert parse_rules('{"nope": 1}') == LibraryFilters.empty()

def test_date_range_is_absolute_seconds():
    filters = parse_filters({"date_from": "1790000000", "date_to": "1799999999"})
    assert (filters.date_from, filters.date_to) == (1790000000, 1799999999)

def test_random_sort_requires_a_seed():
    with pytest.raises(InvalidFilters):
        parse_filters({"sort": "random"})
    assert parse_filters({"sort": "random", "seed": "7"}).seed == 7
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_player_library_filters -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 `domain/library_filters.py`**

允许键白名单即 `LibraryFilters` 字段名，未知键抛 `InvalidFilters`；数字解析失败同样抛错。`sort="random"` 缺 `seed` 抛错。`parse_rules` 用于智能集合的 `rules_json`：坏 JSON / 未知键 / 非对象一律返回 `LibraryFilters.empty()`，不抛错（展示层要的是"空集合"，不是 500）。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_player_library_filters -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/tgvio_player/domain/library_filters.py tests/test_player_library_filters.py
git commit -m "feat(player): a filter and sort value object that cannot be malformed"
```

### Task 2: `_videos` 支持筛选与排序

**Files:**
- Modify: `src/tgvio_player/adapters/http/server.py`（`_videos` 约 495-540 行）
- Modify: `src/tgvio_player/infrastructure/sqlite_library.py`（视频查询）
- Test: `tests/test_player_http.py`

**Interfaces:**
- Consumes: Task 1 的 `parse_filters` / `LibraryFilters`。
- Produces: `GET /api/v1/videos?category=&limit=&offset=&sort=newest|longest|largest|random|resume&seed=&date_from=&date_to=&min_seconds=&max_seconds=&min_bytes=&max_bytes=&has_cover=&favorite=&resumable=&unwatched=`；返回体仍是 `{items, total, has_more}`，另加 `next_cursor`（随机排序时用 `(seed, offset)`，不引入新字段语义）。

- [ ] **Step 1: 写失败测试**（追加到 `PlayerHttpTests`）

```python
async def test_videos_reject_an_unknown_filter(self) -> None:
    response = await self.client.get("/api/v1/videos?colour=red", cookies={"tgvio_player_session": await self._login()})
    self.assertEqual(response.status, 400)

async def test_videos_filter_by_duration_and_sort_by_size(self) -> None:
    cookie = await self._login()
    body = await (await self.client.get("/api/v1/videos?min_seconds=10&sort=largest", cookies={"tgvio_player_session": cookie})).json()
    durations = [item["duration_seconds"] for item in body["items"]]
    self.assertTrue(all(value >= 10 for value in durations))
    sizes = [item["size_bytes"] for item in body["items"]]
    self.assertEqual(sizes, sorted(sizes, reverse=True))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_player_http -v -k videos`
Expected: FAIL（400/排序未实现）

- [ ] **Step 3: 实现**

在 `_videos` 里 `parse_filters(request.query)` 并把 `InvalidFilters` 映射成 400；查询按需拼 `WHERE` 片段与 `ORDER BY`（`random` 用 `ORDER BY abs(media_id_hash ^ seed)` 之类的确定性表达式，保证同一 seed 分页不重复）；所有片段都用绑定参数，不拼字符串。

- [ ] **Step 4: 跑测试**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_player_http`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/tgvio_player/adapters/http/server.py src/tgvio_player/infrastructure/sqlite_library.py tests/test_player_http.py
git commit -m "feat(player): filter and sort the library without any text search"
```

### Task 3: `0011` 迁移与集合仓储

**Files:**
- Create: `src/tgvio_player/infrastructure/migrations/0011_player_collections.sql`
- Create: `src/tgvio_player/infrastructure/sqlite_collections.py`
- Test: `tests/test_player_collections.py`

**Interfaces:**
- Produces: `SqliteCollectionRepository`，方法 `list() -> tuple[Collection, ...]`、`create(name: str, kind: str, rules_json: str | None) -> Collection`、`rename(collection_id, name)`、`set_rules(collection_id, rules_json)`、`delete(collection_id)`、`add_item(collection_id, media_id) -> bool`、`remove_item(collection_id, media_id) -> bool`、`items(collection_id, limit, offset)`、`counts() -> dict[str, int]`。名称规则：去空白后 1..60 字符，控制字符拒绝。

- [ ] **Step 1: 写失败测试**

```python
async def test_deleting_a_media_row_cascades_out_of_collections(self) -> None:
    collection = await self.repo.create("旅行", "manual", None)
    await self.repo.add_item(collection.collection_id, self.media_id)
    await self.delete_media_row(self.media_id)
    self.assertEqual(await self.repo.items(collection.collection_id, 50, 0), ())

async def test_blank_and_overlong_names_are_rejected(self) -> None:
    for bad in ("", "   ", "x" * 61, "bad\nname"):
        with self.assertRaises(ValueError):
            await self.repo.create(bad, "manual", None)

async def test_add_item_is_idempotent(self) -> None:
    collection = await self.repo.create("想重看", "manual", None)
    self.assertTrue(await self.repo.add_item(collection.collection_id, self.media_id))
    self.assertFalse(await self.repo.add_item(collection.collection_id, self.media_id))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_player_collections -v`
Expected: FAIL（表与仓储不存在）

- [ ] **Step 3: 实现**

按 spec §3.3 建表（含 `ON DELETE CASCADE`）。仓储走现有 SQLite 访问模式；`PRAGMA foreign_keys=ON` 必须确认已开启，否则级联不生效 —— 这是本任务的核心风险点。

- [ ] **Step 4: 跑测试**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_player_collections -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/tgvio_player/infrastructure/migrations/0011_player_collections.sql src/tgvio_player/infrastructure/sqlite_collections.py tests/test_player_collections.py
git commit -m "feat(player): collections that cascade with their media"
```

### Task 4: 集合 HTTP 路由与内置收藏

**Files:**
- Modify: `src/tgvio_player/adapters/http/library.py`
- Test: `tests/test_player_http.py`

**Interfaces:**
- Consumes: Task 3 的仓储。
- Produces: spec §3.5 的八个端点；`GET /api/v1/collections` 的首项固定为 `{"collection_id": "favorites", "kind": "builtin", "name": "收藏"}`，且 `PATCH`/`DELETE` 对它返回 400。

- [ ] **Step 1: 写失败测试**

```python
async def test_collections_require_a_session(self) -> None:
    self.assertEqual((await self.client.get("/api/v1/collections")).status, 401)

async def test_builtin_favorites_cannot_be_renamed_or_deleted(self) -> None:
    cookie = await self._login()
    for method, payload in (("patch", {"name": "x"}), ("delete", None)):
        request = getattr(self.client, method)
        kwargs = {"cookies": {"tgvio_player_session": cookie}}
        if payload is not None:
            kwargs["json"] = payload
        self.assertEqual((await request("/api/v1/collections/favorites", **kwargs)).status, 400)

async def test_member_add_and_remove_are_idempotent(self) -> None:
    cookie = await self._login()
    created = await (await self.client.post("/api/v1/collections", json={"name": "旅行", "kind": "manual"}, cookies={"tgvio_player_session": cookie})).json()
    path = f"/api/v1/collections/{created['collection_id']}/items/{self.media_id}"
    self.assertEqual((await self.client.put(path, cookies={"tgvio_player_session": cookie})).status, 204)
    self.assertEqual((await self.client.put(path, cookies={"tgvio_player_session": cookie})).status, 204)
    self.assertEqual((await self.client.delete(path, cookies={"tgvio_player_session": cookie})).status, 204)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_player_http -v -k collections`
Expected: FAIL

- [ ] **Step 3: 实现**

路由注册照 `library.py` 既有写法；所有写操作返回 204；`PATCH` 支持改名与改条件；智能集合在列表里返回成员计数时用 Task 2 的筛选求值路径，且必须有上限（示例：计数上限 1000，超出返回 `count_capped: true`）。

- [ ] **Step 4: 跑测试**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_player_http`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/tgvio_player/adapters/http/library.py tests/test_player_http.py
git commit -m "feat(player): collections over http with favorites as a builtin"
```

### Task 5: 集合进备份链

**Files:**
- Modify: `src/tgvio_player/application/favorite_backup.py`（或同目录的备份服务）
- Test: `tests/test_player_favorite_backup.py`（不存在则新建）

**Interfaces:**
- Consumes: Task 3 的仓储。
- Produces: 备份 payload 新版本（`schema` 升版），含 `collections: [{name, kind, rules_json, items: [media_id]}]`；旧版本 payload 恢复时视为无集合，不报错。

- [ ] **Step 1: 写失败测试**

```python
def test_backup_payload_carries_collections_and_stays_backward_compatible():
    payload = build_backup_payload(collections=[...])
    assert payload["schema"].endswith("/v2")
    assert restore_collections({"schema": "tgvio.player.favorites/v1"}) == ()
```

- [ ] **Step 2: 跑测试确认失败** → `Expected: FAIL`

- [ ] **Step 3: 实现**：集合序列化进 payload；恢复以服务端事实为准，重复恢复幂等。

- [ ] **Step 4: 跑测试** → `Expected: PASS`

- [ ] **Step 5: 提交**

```bash
git add src/tgvio_player/application/favorite_backup.py tests/test_player_favorite_backup.py
git commit -m "feat(player): collections travel with the favorites backup"
```

### Task 6: 前端筛选结构（纯 TS）

**Files:**
- Create: `player/web/src/library-filters.ts`
- Modify: `player/web/src/api.ts`、`player/web/src/types.ts`
- Test: `player/web/tests/library-filters.test.mjs`

**Interfaces:**
- Produces: `type LibraryFilters`、`emptyFilters()`、`toQuery(filters): string`、`parseQuery(search: string): LibraryFilters`、`sameFilters(a, b): boolean`；日期一律以秒存绝对时间戳。
- Consumes: Task 2 的查询参数名。

- [ ] **Step 1: 写失败测试**

```js
test("filters round-trip through the query string", () => {
  const filters = { ...emptyFilters(), minSeconds: 10, sort: "largest", dateFrom: 1790000000 };
  assert.deepEqual(parseQuery(toQuery(filters)), filters);
});

test("a malformed query falls back to the empty selection instead of throwing", () => {
  assert.deepEqual(parseQuery("?min_seconds=abc&colour=red"), emptyFilters());
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `npm --prefix player/web run test`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**：`toQuery` 只输出非默认值；`parseQuery` 遇到未知键或坏值返回 `emptyFilters()`。

- [ ] **Step 4: 跑测试** → `Expected: PASS`

- [ ] **Step 5: 提交**

```bash
git add player/web/src/library-filters.ts player/web/src/api.ts player/web/src/types.ts player/web/tests/library-filters.test.mjs
git commit -m "feat(player-web): one filter shape shared by the page and its query string"
```

### Task 7: 筛选面板与片库页接线

**Files:**
- Create: `player/web/src/components/filter-sheet.ts`
- Modify: `player/web/src/library.ts`、`player/web/src/styles/browse.css`
- Test: `player/web/tests/library-page.test.mjs`、`player/web/tests/cover-browser.smoke.mjs`

**Interfaces:**
- Consumes: Task 6 的 `LibraryFilters`；现有 `openSheet` 与 `library-toolbar`。
- Produces: `buildFilterSheet(options: { value: LibraryFilters; onApply: (next: LibraryFilters) => void }): HTMLElement`。

- [ ] **Step 1: 写失败测试**（`library-page.test.mjs`）

```js
test("applying a filter reloads the grid from the first page", async () => {
  const page = mount(new VideoLibraryPage(() => {}, () => {}));
  await flush();
  page.applyFilters({ ...emptyFilters(), minSeconds: 30 });
  await flush();
  assert.equal(requests.at(-1).minSeconds, 30);
  assert.equal(requests.at(-1).offset, 0, "a new filter restarts paging");
});
```

- [ ] **Step 2: 跑测试确认失败** → `Expected: FAIL`

- [ ] **Step 3: 实现**：面板走 `sheet`，控件必须 ≥44px、带 `aria-pressed`；应用后 `beginIndex()` 递增 generation 并取消在途请求（Review Focus 第 1 条）。片库页工具栏加"筛选"入口并显示生效条件数。

- [ ] **Step 4: 跑测试 + 浏览器回归**

Run: `npm --prefix player/web run test && npm --prefix player/web run test:browser`
Expected: PASS；并在 `cover-browser.smoke.mjs` 加：筛选后列数仍与密度档位一致；面板打开时闲置 60 秒 → 会话结束后 `.sheet` 不残留（Review Focus 第 4 条）。

- [ ] **Step 5: 提交**

```bash
git add player/web/src/components/filter-sheet.ts player/web/src/library.ts player/web/src/styles/browse.css player/web/tests
git commit -m "feat(player-web): filter the wall without typing anything"
```

### Task 8: 帧墙连续加载

**Files:**
- Modify: `player/web/src/library.ts`、`player/web/src/components/cover-image.ts`（仅如需）、`player/web/tests/cover-browser.smoke.mjs`

**Interfaces:**
- Consumes: Task 7 的筛选与分页；现有 `MAX_ROWS`、`cover-load-queue`（6 通道）。
- Produces: 一次只有一个在途分页请求；滚动到底自动补页直到 `MAX_ROWS`，到达上限时给出明确文案。

- [ ] **Step 1: 写失败测试**（浏览器回归）

```js
check(await evaluate("window.__coverPeak<=6"),"the wall never opens a second cover lane");
check(await evaluate("document.querySelectorAll('.library-page .cover-tile').length>60"),"the wall keeps loading while scrolling");
```

- [ ] **Step 2: 跑测试确认失败** → `Expected: FAIL`

- [ ] **Step 3: 实现**：把自动补页从"最多 3 页"改成受 `MAX_ROWS` 与在途状态约束的连续加载；每一页用 generation 校验；缺封面条目按三态显示（Review Focus 第 5 条）。

- [ ] **Step 4: 跑测试** → `Expected: PASS`

- [ ] **Step 5: 提交**

```bash
git add player/web/src/library.ts player/web/tests/cover-browser.smoke.mjs
git commit -m "feat(player-web): a wall that keeps filling while you scroll"
```

### Task 9: 收藏页 `收藏 | 集合` 分段与成员管理

**Files:**
- Create: `player/web/src/collections.ts`（控制器，≤600 行）
- Modify: `player/web/src/favorites.ts`、`player/web/src/api.ts`、`player/web/src/styles/browse.css`
- Test: `player/web/tests/collections-page.test.mjs`、`player/web/tests/cover-browser.smoke.mjs`

**Interfaces:**
- Consumes: Task 4 的八个端点、Task 6 的筛选结构、现有 `buildCoverTile` 与多选模式。
- Produces: `class CollectionsController`，方法 `load()`、`create(name)`、`rename(id, name)`、`remove(id)`、`addItem(id, mediaId)`、`removeItem(id, mediaId)`、`items(id)`。

- [ ] **Step 1: 写失败测试**

```js
test("the favorites page shows collections next to favorites", async () => {
  const page = mount(new FavoritesPage(() => {}, () => {}));
  await flush();
  assert.ok(page.root.querySelector(".library-segments"));
  assert.equal(byClass(page.root, "collection-row")[0].textContent.includes("收藏"), true);
});

test("adding a cover to a collection never starts playback", async () => {
  // 普通模式点封面仍只播放；加入集合必须走显式入口。
  assert.equal(opened.length, 0);
});
```

- [ ] **Step 2: 跑测试确认失败** → `Expected: FAIL`

- [ ] **Step 3: 实现**：分段复用 `createSlidingIndicator`（不写第二套）；加入/移出走显式入口，多选模式下才批量；内置收藏只读。

- [ ] **Step 4: 跑测试 + 浏览器回归** → `Expected: PASS`

- [ ] **Step 5: 提交**

```bash
git add player/web/src/collections.ts player/web/src/favorites.ts player/web/src/api.ts player/web/src/styles/browse.css player/web/tests
git commit -m "feat(player-web): collections beside favorites, with a builtin that stays read-only"
```

---

## Self-Review

- **Spec coverage**：S1 落在 Task 1/2/6/7/8；S3 落在 Task 3/4/5/9；S4 落在 Task 7/9；S2 与阶段 4 明确不属于本计划（spec §6 已单独成轮）。
- **Step scan**：每个 Task 都以"失败测试 → 确认失败 → 最小实现 → 确认通过 → 提交"推进；实现步骤只给签名与必须固定的值，算法留给实现者。
- **Type consistency**：`LibraryFilters` 字段在后端（Task 1）、查询参数（Task 2）、前端类型（Task 6）三处同名同义；集合方法名在 Task 3/9 一致（`add_item`/`addItem` 仅按语言惯例转写）。
- **Proportion**：计划短于 spec 的 1.5 倍，无函数体转写。
