# R2-18 Mini App 后续技术方案（本轮不实现）

> 2026-09-14；DESIGNED。仅设计，不新增 listener、公网端口、Web 写接口或付费服务。
> 允许的下一步只有在用户明确立项后才能开始。

## 1. 目标与边界

用 Telegram Mini App 解决纯 Bot 按钮难以完成的合集整理：缩略图网格、拖动排序、批量选择与移除。它必须**复用原生 Bot 已有的应用服务**，不新写一套发布/草稿逻辑。

明确不做：多频道/多目的地、外部 AI、付费媒体服务、永久公网媒体 URL、Web 端发布确认（仍回 Bot 完成）。

## 2. 复用边界

- 编辑/草稿：复用 `CollectionEditingService`（revision/CAS、overlay、冻结 submission）。Mini App 只是另一个前端。
- 建议：复用 `SuggestionService`（本地规则）。
- 预览：可选复用 `PreviewService` 的受控缓存与预算。
- 认证：复用 owner allowlist；不引入独立账号体系。

禁止：Mini App 直接写 SQL、直接发布、绕过 operation token、旁路 FIFO/partial/uncertain。

## 3. 认证与 owner 隔离

- 前端从 `Telegram.WebApp.initData` 取得签名串，POST 给校验端点。
- 服务端按 Telegram 官方算法用 bot token 校验 HMAC 签名，并检查 `auth_date` 新鲜度（建议 ≤ 300 s）；过期拒绝。
- 从 initData 解析 `user.id`，必须命中 `ALLOWED_USERS`；生成短期服务端会话（不透明 id，绑定 owner，TTL 建议 30 min，可注销）。
- 所有后续请求携带会话 id；服务端每次重查 owner，不接受前端传入的 owner_id。
- 防重放：会话与一次性操作 token 都单次消费；对同一 revision 的写操作做 CAS。
- CSRF：会话 cookie 用 `SameSite=Strict` + 自定义头校验；拒绝跨站来源。
- 限流：每 owner 每分钟写操作上限；全局并发上限。

## 4. 数据与 API（建议）

只读：
- `GET /mini/collection/<session_id>`：草稿 revision、条目（entry_id、缩略图引用、kind、excluded、position）。
- `GET /mini/drafts`：owner 草稿列表。

写（全部走服务层 command，带 `expected_revision`）：
- `POST /mini/collection/<session_id>/reorder`：`{expected_revision, positions:[{entry_id, position}]}`。
- `POST /mini/collection/<session_id>/exclude`：`{expected_revision, entry_id, excluded}`。
- `POST /mini/collection/<session_id>/cover`：`{expected_revision, entry_id}`。
- `POST /mini/collection/<session_id>/caption`：`{expected_revision, text}`。
- 发布确认**不在 Mini App**：返回 Bot 深链/提示，让 owner 在 Bot 内点确认 token。

所有写返回新 revision；revision 冲突返回 409，前端必须重新拉取并提示“内容已更新”。

## 5. 媒体授权与缓存

- 缩略图不暴露原始本地路径或永久公网 URL。
- 服务端为每个请求签发短期、owner-scoped 的媒体读取令牌（≤ 5 min），响应 `Cache-Control: private, no-store`。
- 缩略图优先复用已生成的封面/缩略图；否则在受控缓存内即时生成小图（严格尺寸/字节预算），并按 TTL 清理。
- 缓存目录与正式任务缓存隔离；清理不得影响正式任务、草稿或可恢复缓存。
- 响应头：CSP、`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: no-referrer`。

## 6. revision 与并发

- 每次编辑同 Bot 一样 `revision+1`；Mini App 与 Bot 共享同一 draft revision。
- 拖动排序在前端乐观显示，提交后以服务端返回 revision 为准；409 时回滚并提示。
- 冲突不静默合并：要求刷新后再操作。

## 7. 部署与成本

- 需要 HTTPS 域名与反向代理（Caddy/Nginx），由用户另行授权；本轮不新增端口。
- 与现有只读 Dashboard 解耦，不开启 Dashboard 写接口。
- 资源成本：反向代理 + 少量缩略图缓存；无外部付费依赖。
- 运维：独立 systemd 单元或复用容器内 asyncio listener（仅在获得明确授权后决定）。

## 8. 验收（未来）

- initData 签名/auth_date 校验、跨 owner 拒绝、会话过期、重放拒绝。
- revision 冲突返回 409 且不产生副作用。
- 拖动排序/批量移除与 Bot 结果一致；同一草稿在两端的 revision 一致。
- 媒体令牌短期有效、不可枚举、不泄露路径；缓存清理不影响正式任务。
- 限流与并发上限生效；无第二个发布路径。
