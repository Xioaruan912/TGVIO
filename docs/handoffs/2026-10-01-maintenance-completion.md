# 归档、封面和副本补齐的持续恢复

## 范围与事实

本轮用户要求归档相关补齐全部完成、自动重试，并核对全覆盖。
这里只更新独立 cover/rendition 维护代码，不更新 Player、Bot 或原始归档清单。
2026-10-01 11:27 UTC 只读审计：有效 Player 主视频 910；维护发现 1017 个来源任务。
封面 checkpoint done 417、failed 27；副本 done 58、failed 13。
原画按实际高度计算，需要新增 480p 的 871 条、720p 的 729 条，投影仍分别缺 811、673。
这些是当时快照，不是最终覆盖率；checkpoint 包含重复或非有效来源。

Bot 归档包 committed 51、cancelled 1，没有 pending/failed 归档包。
历史 cancelled 记录含 archive_capability_missing，不将它解释为用户主动取消或归档成功。
只读核对显示该历史包的一项原始缓存已不在本地，不能直接重试；它的发布 Job 已成功。
另一个 failed Job 是 download_failed，未形成归档包。两者不能冒称归档完整。
不重播 Telegram，不复活已删除媒体；历史原件恢复需走既有主进程恢复路径。

## 修改与验证

持续维护前五次失败十分钟冷却，其后 1/2/4/6 小时退避继续重试；批次保留失败重试配额，
不清零累计失败，不扩大源文件、Range、FFmpeg 资源预算。元数据 I/O 共用三次有界重试。
封面批次之间检查重试，已验证发现列表十分钟刷新；单候选取帧超时继续下一候选。
副本发现失败三十秒后恢复，不退出 watch。任务完成仍须源绑定及真实远端对象验证。

新增六项隔离回归覆盖长期失败、冷却、调度、取消、候选超时和扫描恢复。
完整 scripts/check.sh 已通过：Python、前端单测、TypeScript/Vite、架构及仓库检查。
本轮没有视觉改动，不以此声称真机播放或生产全覆盖通过。

## 持续验收

生产切换和最新数量另写 operations 发布证据。维护进程使用各自原 checkpoint。
完成标准是有效媒体逐项拥有真实可读封面，以及按原画高度应有的 480p/720p；
收藏覆盖单独核对。历史取消、源丢失或损坏必须列为真实异常，不能以占位代替。

## 已部署与继续入口

持续恢复版本 763a1f6 已部署两个维护进程；4K 内存/编码线程修正 a5fd3e0 仅再切换封面进程。
生产数量、原件异常和逐张收藏验收见 [维护发布回执](../operations/2026-10-01-maintenance-completion-release.md)。
每小时当前线程跟进 ID 为 tgvio，继续检查真实完成条件，尚未全库补齐。
本机私有 completion-release/monitor.py 通过 expected-workers.json 检查当前两个不同版本；
completion-cover-memory 保留封面后续切换及隔离 4K 验证。不要按 Git HEAD 强行重建 Bot/Player。
