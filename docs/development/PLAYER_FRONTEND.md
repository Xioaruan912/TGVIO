# SKY TGVIO Player 前端设计与实现边界

## 产品与布局

私人视频空间围绕观看、找片、收藏与续播组织。沿用原生 TypeScript/Vite、
中文 SKY TGVIO 品牌及短片/长片/收藏/片库四区导航，设置独立。
不新增社交数字、作者资料、会员、营销 Hero 或推荐算法。

浅色令牌统一在 player/web/src/styles/base.css，旧 SKY 名称为兼容别名。
Header、Media Stage、Player Panel、Navigation 独立占位；媒体用中性深色、
object-fit: contain 完整播放。低高度横屏采用画面与控制区分栏。
常驻播放、声音、收藏、隐私锁；下载、文件夹、随机播放在低频菜单。
永久删除单独分隔，并保留服务端权限及安全默认焦点。

## 内容封面

片库、收藏、长片共用 components/cover-tile.ts 与 styles/cover.css。
短片/混合网格统一 9:16，独立长片网格 16:9；封面图片 object-fit: cover。
竖封面标题独占一至两行，横封面标题单行；类型、续播位置与时长在独立信息行。
局部渐变跟随真实信息高度，字体放大与纯白画面仍可读；未知时长不显示假 0:00。
只有混合网格重复显示类型。收藏标记与选择标记互斥，选择用描边与勾选，
不通过整图变暗表达。外层网格按实际容器宽度计算，移动端以两列为主，桌面容器最大 1440px。

封面主体是语义 button；预览、选择、图片重试是兄弟控件，不嵌套 button。
正常浏览直接进入既有播放器；多选只改选择。预览由用户触发，静音，
同时最多一个，切页面/隐藏/播放/销毁时释放。预览按钮准确表达进行状态。
卡片焦点外描边可见；hover 缩放只作用于内部画面，reduced-motion 取消位移。

封面状态明确区分 loading / ready / missing / failed。缺图与失败使用实色中性
蓝灰降级，不伪装成真实内容。请求失败不等于视频不可播放，提供独立重试。
IntersectionObserver 只让临近可视区的图片加入共用两槽队列；
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
失败、150% 文本、指针命中、键盘、焦点、隐私、单预览、声音确认及登录缩高；额外检查 2560px 容器上限和浏览器像素采样的白字对比度。
测试模式明确，不在生产失败时自动切换假数据。
这些验收不代表 Android/iOS 真机、真实软键盘或生产媒体已验证。
