# 2026-10-01 Player UI 与组件重构交接

## 本轮范围

针对用户要求“实际重构 UI 和组件”，在原生 TypeScript + Vite 工程内重组展示层。
本轮继承此前推送与 VPS 发布授权；候选完成全部门禁后只切换 Player。
Bot 与正在执行 480p/720p 补齐的独立 worker 保持原容器运行。

## 实际修改

- 短片：紧凑顶栏、独立视频区域、常驻播放面板、独立导航；桌面使用 92px 左侧导航和剩余空间观看区。缓存估算大小位于控制面板，低高度横屏与更多操作共用一行。
- 长片：DOM 拼装抽离到 large-player-view，播放状态、seek、收藏、清晰度及媒体销毁仍由 LargePlayer 拥有；常用控制显示文字，画中画与危险操作进入更多菜单。
- 片库/收藏/长片列表：共用 browse-frame，真实滚动反馈、标题与返回入口一致；日期/文件夹使用目录卡片，视频沿用统一封面网格。
- 封面：cover-tile 拥有播放/预览/多选，cover-image 拥有静态图片加载/重试/取消，保留两请求预算、20 秒期限和一次自动重试。静态列表不创建 video；移动端预览入口常驻可发现。
- timeline 统一短/长进度 DOM；action-menu 统一原生 details、Escape 收起与焦点恢复；navigation 统一桌面/手机四项导航。
- access-view、confirmations、sheet 分别拥有口令/错误页、声音与删除确认、设置弹层；保留安全默认焦点、背景 inert、关闭与焦点恢复。
- styles/browse.css 接收原来散落在设置/长播放器样式中的列表规则。shell/overlay/large/sheet/settings/cover 使用现有同一套令牌，调整真实结构与响应式，无新框架/组件库。
- ui.ts 从集中拼装变为 243 行装配入口；large.ts 从 770 降至 682 行；自动检查同步收紧 large.ts 上限。AGENTS.md 固化组件所有权，不写运行日志。

本轮未重写 main/feed/player/playback-state/seek/手势/闲置隐私/收藏串行策略。
短片三 video 池保持复用，只有 current 播放；封面 cover 裁切与实际播放 contain 分离。

## 验证

执行 bash scripts/check.sh --browser 最终通过：
Python 3.13.5 全仓 1016 项测试、Node 215 项测试、严格 TypeScript/Vite 构建、
架构/仓库卫生检查与 git diff --check 通过。
浏览器播放/布局 fixture 743 项 + 实际应用 256 项，共 999 项检查通过；
隔离媒体 54 次 HTTP Range 请求，封面请求峰值 2。
同时通过 git diff --cached --check。构建 JS 168.24KB（gzip 50.17KB），
CSS 47.72KB（gzip 9.89KB），未增加运行框架或依赖。

调试时发现横屏缓存读数底部截断，已通过同排布局修复并回归。
一次长片返回按钮命中检查失败未持续复现；保留命中元素/坐标/视口诊断，
随后独立浏览器验收和最终完整门禁均通过，不据此扩大真机验收范围。

浏览器范围：360×800、390×844、430×932、768×1024、1440×1000、
844×390 低高度横屏，另验收 2560×1200 容器上限、150% 文本、长标题、
未知时长、缺图/失败、登录高度缩小、真实命中、焦点、隐私及声音确认。
截图位于 C:/Users/Administrator/TGVIO-ui-rebuild-20261001。
来源为本地 FFmpeg testsrc2 合成 MP4 与提取帧，明确属于隔离测试媒体，
没有生产身份、私人视频或无关网络照片。截图不是生产片库画面。

Android/iOS 真机、系统软键盘、生产真实媒体播放未在本轮验收。
Chrome 视口模拟、HTTP Range fixture 与本地构建不能替代真机/生产播放。

## 真实封面供给

2026-10-01 06:17:33 UTC 只读核查：VPS 910 个有效主视频，
media_covers 共 0 条、有效主视频关联封面 0 条。
这是数据库登记覆盖情况，不推断远端归档是否存在未登记图片。
前端绑定现有 cover_url，并通过 Player API 访问；线上无字段条目显示“暂无封面”。
图片请求中/缺图/加载失败分别处理，失败保留播放与手动重试。
因此封面组件与布局已改版，生产真实封面供给尚缺，不能称真实缩略图覆盖完成。
480p/720p 副本任务不生成静态封面。

最小配套方向：先抽样核实既有 committed package 的 cover 声明与文件；
已有有效声明通过正常扫描与索引合同登记。没有声明的旧归档，需要单独
设计追加型静态封面 sidecar 与索引供给，复用受控源文件、有界单任务处理，
保持原视频、manifest 与 _COMPLETE 不可变，不在浏览器批量解码或直接写生产库。
此项需单独后端/归档任务与成本评估，本轮未修改 Bot 或启动全库封面处理。
收藏接口没有逐项备份状态，继续保留真实备份汇总与重试能力，不造逐项状态。

## Git 与 VPS 状态

此交接随应用变更提交；部署前必须 clean 且 origin/main 已包含候选提交。
候选源码通过 git archive 与逐文件 SHA-256/mode 清单同步独立 snapshot。
运行切换结果、镜像身份、SQLite 一致性备份与 Bot/worker 不变证据，另记
docs/operations/2026-10-01-player-ui-rebuild-release.md；该记录存在且有后验才表示上线。
