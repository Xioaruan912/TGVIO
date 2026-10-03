# 相似聚类入口实施计划 —— 阶段 4

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让已可用的封面指纹变成两个可用的找回入口：墙上"按相似排序"，以及从一张封面出发的"和这张像的"（有界、需登录）。

**Architecture:** 相似排序是**客户端**对已加载瓦片的纯函数重排（零新端点、零新分页请求）；"和这张像的"是一个**有界**服务端端点（最多扫 1000 行，超限返回 `truncated`）；近似重复只是同一距离下的一个标记，永不删除或合并。

**Tech Stack:** Python 3.11 + aiohttp + SQLite（无新迁移）；Vite + 严格 TypeScript（无框架）；测试 `PYTHONPATH=src .venv/bin/python -m unittest` 与 `npm --prefix player/web run test`。

**Spec:** `docs/superpowers/specs/2026-10-03-player-cover-similarity-clustering-design.md`

## Global Constraints

- 距离 = 两个 16 位小写 hex 的汉明距离（异或 popcount，0..64）；**缺哈希在进入计算前就被排除**，绝不把两个 NULL 当相似。
- 阈值常量：近似重复 `6`、相近 `16`；端点 `threshold` 上限 `32`（只许收紧）。
- 端点需登录（未登录 401）；扫描上限 `1000` 行，超过返回 `truncated: true`；结果确定性（距离升序，同距离按 media_id 升序）。
- 相似排序是**已加载集合内的重排**，不产生新的分页请求；缺哈希条目按原顺序追加末尾。
- 不新增迁移、不改 `covers.json`、不新增封面并发通路（相似 sheet 里的瓦片走同一个 6 通道队列）。
- 前端新模块 ≤600 行；`main.ts`/`large.ts` 行数不得增长。
- 不做自动去重、不删文件、不改集合成员。

## Review Focus

- **缺哈希被算成相似**（两个 NULL 异或为 0）：距离函数只接受 16 位 hex。测试放 Task 1。
- **端点退化成全库扫描**：上限 1000 行 + `truncated` 标记。测试放 Task 2。
- **退役封面行的哈希泄漏进结果**：只取 active 封面行。测试放 Task 2。
- **"按相似排序"偷偷变成新分页语义**：重排不触发请求；浏览器检查网络计数不变。测试放 Task 3。
- **相似列表里缺哈希条目被伪造**：仍显示 `missing` 三态之一。测试放 Task 3。

---

### Task 1: 纯函数（距离、贪心链、缺哈希）

**Files:**
- Create: `player/web/src/cover-similarity.ts`
- Test: `player/web/tests/cover-similarity.test.mjs`

**Interfaces:**
- Produces: `DUPLICATE_DISTANCE = 6`、`SIMILAR_DISTANCE = 16`、`hamming(a: string, b: string): number`（两边都必须匹配 `^[0-9a-f]{16}$`，否则抛错）、`similarOrder<T extends {id: string; phash?: string | null}>(items: T[]): T[]`。

- [ ] **Step 1: 写失败测试**

```js
test("distance is a popcount over the hex", () => {
  assert.equal(hamming("0000000000000000", "0000000000000000"), 0);
  assert.equal(hamming("0000000000000000", "ffffffffffffffff"), 64);
  assert.equal(hamming("0000000000000000", "8000000000000000"), 1);
});
test("an unreadable fingerprint never becomes a distance", () => {
  for (const bad of [null, undefined, "", "0123456789abcdef0", "0123456789ABCDEF", "g123456789abcdef"]) {
    assert.throws(() => hamming(bad, "0".repeat(16)));
  }
});
test("the chain keeps neighbours close and appends the unknown at the end", () => {
  const items = [
    { id: "a", phash: "0000000000000000" },
    { id: "unknown", phash: null },
    { id: "b", phash: "0000000000000003" },   // distance 2 from a
    { id: "far", phash: "ffffffffffffffff" }, // distance 64 from everything
  ];
  assert.deepEqual(similarOrder(items).map(x => x.id), ["a", "b", "far", "unknown"]);
  assert.deepEqual(similarOrder([]), []);
  assert.deepEqual(similarOrder(items.filter(x => !x.phash)).map(x => x.id), ["unknown"]);
});
```

- [ ] **Step 2: 跑测试确认失败** — `npm --prefix player/web run test` → Expected: FAIL（模块不存在）
- [ ] **Step 3: 实现**：`hamming` 用 BigInt 异或 + popcount（或按字符查表累加，二者皆可，必须整数）；`similarOrder` 以首项为起点贪心取最近（并列取 `id` 升序），缺哈希按原相对顺序追加末尾。
- [ ] **Step 4: 跑测试确认通过** — Expected: PASS
- [ ] **Step 5: 提交** — `git add player/web/src/cover-similarity.ts player/web/tests/cover-similarity.test.mjs && git commit -m "feat(player-web): a pure similar-order over the tiles already loaded"`

### Task 2: 有界的"和这张像的"端点

**Files:**
- Modify: `src/tgvio_player/infrastructure/sqlite.py`（新方法）、`src/tgvio_player/application/ports.py`（声明）、`src/tgvio_player/adapters/http/media.py`（handler）、`src/tgvio_player/adapters/http/server.py`（注册路由）
- Test: `tests/test_player_http.py`

**Interfaces:**
- Produces: `similar_cover_ids(phash: str, *, threshold: int, limit: int) -> tuple[tuple[str, int], ...]`（`(media_id, distance)`，确定性排序）；`GET /api/v1/media/{media_id}/similar?limit=&threshold=` → `{"items": [...DTO...], "threshold": int, "truncated": bool}`。

- [ ] **Step 1: 写失败测试**：未登录 401；目标无哈希 → `200 {"items": []}`；`threshold=99`/`limit=0` → 400；同距离按 media_id；`limit` 上限 60；**退役封面行的哈希不出现**；扫描截断 → `truncated: true`（用假仓库或 patch 上限）。
- [ ] **Step 2: 跑测试确认失败** — `PYTHONPATH=src .venv/bin/python -m unittest tests.test_player_http -v -k similar` → Expected: FAIL（404/无路由）
- [ ] **Step 3: 实现**：SQL 只取 `media_covers` 中 `active=1`、`phash IS NOT NULL`、`media.active=1` 的行，**上限 1000 行**；距离在 Python 侧算（同一 `hamming` 口径）；阈值/上限校验；无哈希返回空列表而不是 404。
- [ ] **Step 4: 跑测试确认通过** — `PYTHONPATH=src .venv/bin/python -m unittest tests.test_player_http` → Expected: PASS
- [ ] **Step 5: 提交** — `git commit -m "feat(player): a bounded way to ask what looks like this cover"`

### Task 3: 墙上两个入口（相似排序开关 + 相似 sheet）

**Files:**
- Modify: `player/web/src/types.ts`（`Clip.phash`）、`player/web/src/api.ts`（`clipFromMedia` 映射 + `similarMedia`）、`player/web/src/library.ts`（开关与 sheet 入口）、`player/web/src/styles/browse.css`
- Test: `player/web/tests/library-page.test.mjs`、`player/web/tests/cover-browser.smoke.mjs`

**Interfaces:**
- Consumes: Task 1 的 `similarOrder`；Task 2 的端点。
- Produces: `Clip.phash: string | null`；`api.similarMedia(mediaId, limit?, threshold?, signal?)`；墙上工具栏的"按相似排序"开关（`aria-pressed`）与选择栏里单选时的"和这张像的"。

- [ ] **Step 1: 写失败测试**（`library-page.test.mjs`）：开关打开后**不产生新的分页请求**且瓦片顺序变为 `similarOrder` 的结果；单选时出现"和这张像的"并请求 `similarMedia`；结果里缺哈希的条目仍显示 `missing` 三态之一。
- [ ] **Step 2: 跑测试确认失败** — `npm --prefix player/web run test` → Expected: FAIL
- [ ] **Step 3: 实现**：排序只重排 `this.controller.rows` 的 DOM 顺序（复用既有 `tiles` 映射，不重新请求）；sheet 用 `buildCoverTile` 渲染结果并标注距离；缺哈希瓦片照旧三态。
- [ ] **Step 4: 跑测试 + 浏览器回归** — `npm --prefix player/web run test && npm --prefix player/web run test:browser` → Expected: PASS（含焦点合同、列数与密度档位一致、在途封面 ≤6）
- [ ] **Step 5: 提交** — `git commit -m "feat(player-web): find the covers that look like this one"`

---

## Self-Review

- **Spec coverage**：§3.1 → Task 1/2 的常量与距离；§3.2 → Task 1/3；§3.3 → Task 2/3；§3.4 → Task 2 的 `duplicate` 标记与 Task 3 的文案；§3.5 → Task 2 的 401/空列表与 Task 1 的拒绝坏值；§3.6 → 不实现（已交付）。
- **Step scan**：三任务均为「失败测试 → 确认失败 → 最小实现 → 确认通过 → 提交」。
- **Type consistency**：`hamming`/`similarOrder`/`DUPLICATE_DISTANCE`/`SIMILAR_DISTANCE` 在 Task 1 定义、Task 3 消费；`similar_cover_ids` 在 Task 2 定义、端点消费；字段名 `phash` 贯穿 Task 1/2/3。
- **Review Focus**：五条各落在对应任务的 Step 1。
- **Proportion**：计划短于 spec，无函数体转写。
