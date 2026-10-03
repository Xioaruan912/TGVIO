# TGVIO Player 前端设计与实现边界

## 产品与布局

私人视频空间围绕观看、找片、收藏与续播组织。沿用原生 TypeScript/Vite、
中文 TGVIO 品牌及短片/长片/收藏/片库四区导航，设置独立。
不新增社交数字、作者资料、会员、营销 Hero 或推荐算法。

展示组件与业务控制器分离。ui.ts 保留兼容导出与 shell 装配；
access-view、confirmations、sheet、navigation、player-panel、timeline、
large-player-view、browse-frame、action-menu、cover-tile / cover-image 分别拥有单一展示职责。
LargePlayer 只调用展示层提供的稳定 video/控件，媒体监听与播放意图不交给组件。
短/长进度条共用 timeline 构建，交互仍由 seek-control 管理。

令牌统一在 player/web/src/styles/base.css，是唯一真源；
Header、Media Stage、Player Panel、Navigation 独立占位。
手机为 52px 顶栏与 66px 导航基线；桌面为 72px 图标侧栏与宽观看区，
视频下方控制卡分别安排标识、进度、直接操作与缓存大小。
缓存显示不再占用顶栏，原 netSpeed 偏好键兼容。更多菜单原生 details，
Escape 收起并恢复焦点；长片永久删除/画中画与播放/声音/收藏分开。媒体用中性深色、
object-fit: contain 完整播放。低高度横屏采用画面与控制区分栏。
常驻播放、声音、收藏、隐私锁；下载、文件夹、随机播放在低频菜单。
永久删除单独分隔，并保留服务端权限及安全默认焦点。

## 浏览与设置

片库、收藏、长片共享 browse-frame：返回、标题、辅助上下文、工具栏、
状态提示、选择条、独立滚动内容。实际 list 的 scrollTop 驱动标题分隔线，
浏览容器在超宽窗口也不超过 library-max。
日期和文件夹为明确的目录卡片，显示真实数量与日期基准。
收藏保持“播放已加载”范围说明，只有选择模式才显示勾选。
长片继续观看采用真实续播位置，章节不复制白色嵌套卡片。

设置桌面为右侧抽屉，移动为底部 Sheet；分组、开关、可执行项箭头、
关闭与焦点恢复保持同一语义。口令页是独立无媒体的访问组件，
删除/声音确认由 confirmations 展示，原权限与确认回调不变。

## 内容封面

片库、收藏、长片共用 components/cover-tile.ts 与 styles/cover.css。
每张卡片按自身类型保持真实比例：短片封面 9:16，长片封面 16:9，
封面图片 object-fit: cover。片库“全部”是长短片混合网格，由
components/cover-masonry.ts 做列式填充：列数、列间距与左右边距仍由 CSS 拥有，
该模块只把实测高度换算成 translate 偏移与容器高度，因此卡片内部、DOM 顺序、
焦点顺序与容器查询均不变，也保留 hover 位移。仅长片网格仍用 16:9 的 cover-grid-wide。
竖封面标题独占一至两行，横封面标题单行；类型、续播位置与时长在独立信息行。
局部渐变跟随真实信息高度，字体放大与纯白画面仍可读；未知时长不显示假 0:00。
只有混合网格重复显示类型。收藏标记与选择标记互斥，选择用描边与勾选，
不通过整图变暗表达。外层网格按实际容器宽度计算，移动端以两列为主，桌面容器最大 1440px。

封面主体是语义 button；选择与图片重试是兄弟控件，不嵌套 button。
正常浏览直接进入既有播放器；多选只改选择。列表不提供行内预览，
浏览界面不因 hover/聚焦发起媒体请求。
卡片焦点外描边可见；hover 缩放只作用于内部画面，reduced-motion 取消位移。

封面状态明确区分 loading / ready / missing / failed。缺图与失败使用实色中性
蓝灰降级，不伪装成真实内容。请求失败不等于视频不可播放，提供独立重试。
cover-image 的 IntersectionObserver 只让临近可视区的图片加入共用两槽队列；
实际请求有 20 秒期限及一次自动重试。离开可视区取消尚未开始的排队；
卡片 destroy 清理 observer、监听、定时器、src 和请求槽，旧回调不复活新状态。
列表不批量创建 video、Canvas 解码或完整 Blob，不持久缓存私人封面。

## 数据与媒体合同

MediaDto.cover_url 是可选静态图片字段，clipFromMedia 直接映射。
后端现有 /api/v1/media/:id/cover 要求 Player 认证，只代理 catalog 中合法的
committed Archive cover 元数据，并有独立并发/字节预算。浏览器不拿 WebDAV
路径或凭据。字段为空只表示没有可用封面声明，不代表视频坏了。

本次不自动生成旧媒体封面，也不改 Bot/Archive 生成流程。需要补旧库时，
应沿用既有封面生成与 committed manifest 流程，在明确媒体范围和成本后
产生合法归档版本、同步 catalog；不得现场改写已提交包或绕过 API。

三 video 池、状态控制器、seek/手势、声音确认、60 秒隐私合同、清晰度恢复、
收藏队列及 Range 流式播放继续由既有模块拥有。视觉组件不接管播放意图。
长片分页去重、三次自动补页、1000 条预算、显式重试、退出请求取消及续播
焦点/滚动恢复；不改变存储或排序架构。

## 验证

npm test / npm run build / npm run test:browser / git diff --check。
浏览器入口先跑 playback/layout fixture，再跑实际应用 fake API + 真正隔离
H.264 MP4 和由其提取的 PNG 帧。端口与 Chrome profile 独立，退出清理媒体。
可设置 TGVIO_SCREENSHOTS 保存截图与 JSON 报告。

覆盖 360、390、430、768、1440 和 844x390；长标题、未知时长、缺图、
失败、150% 文本、指针命中、键盘、焦点、隐私、拖动缩略图、声音确认及登录缩高；额外检查 2560px 容器上限和浏览器像素采样的白字对比度。
测试模式明确，不在生产失败时自动切换假数据。
这些验收不代表 Android/iOS 真机、真实软键盘或生产媒体已验证。
