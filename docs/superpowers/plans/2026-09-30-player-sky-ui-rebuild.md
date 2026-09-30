# SKY 移动端前端重构

## 范围与边界

用户明确要求彻底重构 UI，天蓝视觉，不复用旧 TikTok 排版，手机移动端优先。
本轮授权本地重构与 browser-act 隔离浏览器验收，不部署、不提交；核心 API / Range / 三 video 池 / 有界预热 / 隐私及声音合同保持。

## 信息结构

- 独立应用页头：品牌、页面上下文、设置；不盖在视频上。
- 独立媒体舞台：Feed、加载、隐私遮挡、播放提示；这里才接受画面手势。
- 常驻播放面板：视频信息、48px 进度触摸区、时间标签、播放/声音/收藏；高级操作放可展开菜单。
- 独立底部导航：短片、长片、收藏、片库；不与播放或进度重叠。
- 长播放器采用相同媒体/进度/transport分层；列表、搜索筛选、设置及登录使用天蓝卡片、chips、sheet/dialog组件。

## 功能模块

组件拆分为 app-header / navigation / media-actions / dialog / seek-control；设置 view 独立，main 只传应用动作。
进度条控制器显式处理 pointer capture、指针坐标、预览和一次提交；源改变/锁定/后台/取消不提交；媒体 timeupdate 不覆盖拖动中的预览。
先写失败回归锁定 seek，再实现；更新旧隐藏控件测试为新常驻面板合同，保留隐私/加载/媒体竞态回归。

## 视觉与动作

天空蓝+白色卡片+深蓝文字，统一 radius/spacing/shadow/motion tokens，安全区、320px窄屏、横屏及桌面响应；按钮最低44px，seek48px。
原生轻量动画：卡片/Sheet入场、按钮press、状态过渡、收藏反馈、skeleton；尊重 prefers-reduced-motion。不引入无必要框架或CDN组件。

## 验收

单元测试、严格TS/Vite构建、Python Player回归、架构/source tree/diff门禁。
browser-act新建空白chrome（用户已确认），仅访问loopback mock API与自产测试媒体；390/430/320宽及桌面检查、真实鼠标/触摸拖动短长进度、播放暂停、收藏、导航、搜索、设置sheet焦点/关闭、隐私锁与Reduced Motion；保存截图与证据（不包含凭据）。
隔离Chrome不能冒充iOS/Android实机或生产验收；实际未测项单列。

## 后续授权与验收更新

用户在恢复验收后明确要求：测试完成后上传 GitHub，并进行 Player-only VPS 发布。
不得将此授权扩大到 Bot、生产代理、秘密或数据库结构修改；发布须有 SQLite 在线备份、离线迁移副本演练、回滚镜像/env 与生产后验。
实际验收发现并修复跨 tab 遗留列表、收藏服务端初值/进行中意图、短/长隐私遮罩透帧；保留所有旧媒体竞态与闲置测试。
