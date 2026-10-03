# Player 日期/文件夹选片重构

> **执行状态（2026-09-30）**：第一/二阶段已完成并通过本地全部门禁，详见 [`docs/refactor-v2/evidence/R2-23_PLAYER_LIBRARY_SELECTION_AND_COVER_ASSESSMENT.md`](../../refactor-v2/evidence/R2-23_PLAYER_LIBRARY_SELECTION_AND_COVER_ASSESSMENT.md)：前端 170/170、Python **937/937**、真实 Chrome 5 视口与 4 视口布局、architecture/verify-tree/diff --check 全通过。修复了“自动连播重置 60 秒隐私期限”等 7 处边界缺陷，并修复了线上 429 的**流容量公平性**缺陷（能力探测不再占播放槽位、单客户端不再吃满全局预算）。**未部署、未推送；生产仍为 `db6dcba`。** 封面评估结论：Archive 现有产物不含可消费小封面，需走归档侧版本化封面（推荐）或 Player 自有有界生成，且不得占用播放流槽位。遗留：startup range 回退路径在夹层中挂起，待单独定位。

## 用户决定与顺序

1. 完全替换旧“同组”交互，先解决当前文件夹选片与900条片库查找。
2. 日期采用现有归档目录日期，明确标为归档日期；不是准确的网盘上传完成时间。
3. 同时支持单条点播和多选播放，只播放已选内容。
4. 前述验收完成后才评估已有小封面、增量生成、缓存命中及任务状态；本阶段不引入封面生成、不接入Telegram身份、不重启Bot。

## 已确认根因

旧group是日期聚合，不是真实文件夹；点击一条会把所有已加载items塞入home。片库只允许opaque编号前缀，缺少日期索引、目录浏览。group/count查询未排除media_variants，转码重复。legacy YYYY/MM/DD日期解析缺失；未收录的旧包不可能靠catalog SQL补回。

## 第一阶段（不新增DB迁移）

- Player已有catalog构建日期索引→Archive package子文件夹→原版视频列表；日期未知单独归类，legacy保留UTC目录依据，v2为北京时间任务创建日。
- 新auth GET `/api/v1/library/dates`、`folders`、`videos`。不改变发现层；不读取视频、不预热；先服务器完整过滤再keyset分页，计数与列表同谓词，跨包按media ID去重。
- folder ID不泄漏路径，只返回安全日期/批次标签；所有query与cursor严格验证。
- UI保留SKY分层，日期可直接跳转、目录面包屑、分类、主动/近底分页；请求abort+generation、去重、有限预算、重试。
- 卡片单条播放、显式复选框、多选最多100条；选择順序稳定，不自动选整页。播放时保留库DOM/滚动/选择；返回可继续挑选。
- 独立LibraryPlayback拥有1个LargePlayer，不修改home shuffle或收藏context。短/长分类采用原有idle合同，previous/next/结束自动推进有token隔离，旧结束事件不能跳新视频。
- 当前视频更多菜单改为“所在文件夹”，用media_id查归属；不依赖旧groups字段，所以已入catalog的legacy/未知日期仍可进入。

## 验收门禁

900条离线catalog夹具、跨日/同日多包/跨包重复/转码/未知/legacy/非法参数、全库后页目标date可找到、分页数与total一致、无prepare/prefetch副作用。
前端纯播放队列、控制器、取消及焦点生命周期测试；browser-act loopback真实生成媒体验证单条/多选/返回位置/不影响home、窄屏与横屏、真实seek、idle短长区别。
完整Python/Node/TS/Vite/architecture/tree/diff；生产若发布另行记录实际后验，不把本地fixture当真机或生产浏览器播放通过。

## 下一阶段封面评估

固定封面与任意时间scrub区分；先清点Archive是否已有可消费小封面，再决定Player版本化缓存/有界增量队列。没有可靠小图就占位，不能为封面整文件读取或全库抢占播放资源。需独立总deadline、传输字节/磁盘/CPU预算、pressure取消、命中和失败状态指标；不照搬MistRelay无界队列/public缓存头。
