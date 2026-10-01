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
后续证实该原件是照片，在另一 committed 包已有同摘要同大小对象，116647 bytes 实际读取及全 SHA256 校验通过；不需重播该旧包。
另一个 failed Job 是 download_failed，未形成归档包，继续单列核实。
不重播 Telegram，不复活已删除媒体；其他原件恢复需走既有主进程恢复路径。

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

## 20:42 北京时间跟进

当前封面 483/910，480p 71/871，720p 67/729，收藏封面仍 36/36。
只读失败来源抽查与历史下载恢复状态见 [持续巡查](../operations/2026-10-01-maintenance-progress.md)。
两个工作器无重启/OOM，继续自动重试；此轮没有应用发布。

## 21:49 北京时间跟进

当前有效投影封面 565/910、480p 75/871、720p 72/729，收藏封面仍 36/36。
两个工作器无重启/OOM，副本临时文件正在写入，继续原预算补齐。
失败来源有界 HEAD/Range、退避及资源证据见 [持续巡查](../operations/2026-10-01-maintenance-progress.md)。
本轮仅同步文档及源码快照，没有应用发布；仍不能宣称全覆盖。

## 22:48 北京时间跟进

当前有效投影封面 643/910、480p 80/871、720p 74/729，收藏封面仍 36/36。
封面仅一项 ValueError 失败，累计 6 次，源可读和元数据绑定检查通过，尚未生成真实封面。
副本有效失败任务 13、唯一视频 11，工作器仍有新增完成，不盲目重启。
具体退避及只读证据见 [持续巡查](../operations/2026-10-01-maintenance-progress.md)。
本轮无应用代码变更，仅推送/同步文档和源码快照；继续原预算重试及实时全覆盖验收。

## 23:44 北京时间诊断准备

只读覆盖封面 711/910、480p 84/871、720p 78/729，收藏封面 36/36；仍未完成。
副本 WebDavArchiveError 增至 21 项；六个新增失败来源 PROPFIND 大小正常，
两项失败来源的清单绑定索引有效、renditions 目录存在，但没有对应源的副本条目。
有效 WebDAV 失败末次耗时约 116–586 秒，现有异常类不能区分上传、目录、元数据和校验。
此证据不足以断言编码器、原件或权限损坏；先修正诊断不足。

副本 CLI 改用公共传输错误的精确白名单分类及合法 HTTP 状态，
不输出原始异常、凭据或路径，不改变 checkpoint、重试、FFmpeg 或网络预算。
三项隔离回归覆盖未知错误隐私、HTTP 状态校验、真实 CLI 失败累计次数和冷却。
完整 scripts/check.sh 通过，准备从 clean 已推送候选只更新副本工作器。
封面保持 a5fd3e0；Bot/Player 不变。候选和后验留在独立 completion-rendition-diagnostics 私有证据目录。

## 2026-10-02 00:08 北京时间后验

副本诊断 7e3c35b 已部署，只切换 tgvio-renditions；封面仍 a5fd3e0，Bot/Player 未改变。
原检查点全部行在预演前后逐项相同，保留备份和 763a1f6 回滚容器；无重启/OOM。
当前封面 736/910、480p 85/871、720p 78/729；实时 36 条收藏封面本轮再次逐张鉴权 JPEG/SHA256 验收通过。
完整检查 Python 1043 / Node 215 / TS/Vite 等通过。新副本任务已开始，尚无新的分类失败事件。
最新 [诊断发布回执](../operations/2026-10-02-rendition-diagnostics-release.md) 与私有 completion-rendition-diagnostics
保存 candidate/deployment、检查点备份和 telemetry；completion-release/expected-workers.json 已只更新副本身份。
后续继续读取 monitor，使用 completion-rendition-diagnostics/call-remote.py telemetry-remote.py
核对精确 failure_code/http_status，定位写入/验证瓶颈；不盲目重建其他工作器。
补齐和自动重试继续，最终完成仍需当前有效视频逐项及鉴权可读验收。

## 2026-10-02 00:27 北京时间继续入口

当前有效覆盖封面 752/910、480p 85/871、720p 78/729、收藏封面 36/36，仍未全覆盖。
新日志确认至少三次 archive_upload_unverified；一次 archive_source_read 须区分失效来源，
不得借此复活已删除内容。一次有效上传失败目录发现四个未登记 480p 对象，尚未验证完整内容。
下一步优先受控验证/恢复真正已上传副本，并定位传输/确认失败，不只重复转码或 HEAD。
恢复前须校验实际内容哈希、源绑定、质量/时长及原清单仍未变化；不以文件名/长度判完成。
保持单工作器与现有预算，禁止旁路第二个媒体传输/解码进程。
详细已知/未知范围见 [诊断回执后验](../operations/2026-10-02-rendition-diagnostics-release.md)。
当前副本运行仍 7e3c35b、封面 a5fd3e0，源码文档提交不应触发其他重建。

## 2026-10-02 01:14 北京时间副本恢复切换

最新 [未登记副本恢复回执](../operations/2026-10-02-rendition-recovery-release.md)：
668fcd7 已推送并实际部署副本维护；封面仍 a5fd3e0，Bot/Player 不变。
完整门禁 Python 1055、Node 215、TS/Vite 等通过。新逻辑在单工作器内有界验证并恢复未登记的副本，
不凭文件名/长度登记，校验完整内容哈希、原画完整摘要注释、高度/时长及完整解码。
恢复与预留原画下载合计不超过既有 4GiB，上限 128MiB 恢复、每候选 64MiB，每高度最多两个；
持续冷却、累计失败、原件与清单边界保持。

受控只读验收一份有效来源的 16023038-byte 480p 未登记对象，全部媒体校验通过，原索引未改变。
不能将此验收计为索引已登记或全覆盖；随后由正常工作器自动恢复，不清零或催促失败预算。
部署预演保持所有原 checkpoint 行，保留独立 SQLite 备份与 7e3c35b 回滚。
第一次发布检查误读统计字段已自动退回旧进程；修正检查后重新预演再完成切换。

17:14 UTC 覆盖封面 800/910、480p 88/871、720p 83/729，收藏封面 36/36；
此轮 36 条收藏再次逐张鉴权读取/JPEG/完整 SHA256 验收。无 OOM，仍未全库完成。
后续先运行 completion-release/monitor.py（expected-workers 已只更新副本为 668fcd7）。
私有 completion-rendition-recovery 保存候选、逐项镜像/源码验证、部署、检查点、已验证孤立对象及验收辅助脚本：
call-remote.py telemetry-remote.py / memory-events-remote.py / recovery-acceptance-remote.py /
recovery-player-range-remote.py。验收 helper 只输出匿名统计，不输出原 ID、Archive 路径或图像。
recovery-acceptance 检查所验来源当前索引、远端对象和累计冷却；Player Range 验收需先等正常 catalog 同步。
不得在正常工作器运行时追加并行媒体下载/解码；不要重复 cold 验证同一文件消耗预算。
后续按 recovered 完成事件、实际索引及 Player 鉴权 Range 核对自动恢复；继续定位 cause_error 指示的上传瓶颈。
原副本诊断 completion-rendition-diagnostics 的 updater 针对旧版本，不能盲目重跑。
当前工作器版本与 Git 文档 HEAD 可以不同；下一次仅因实证缺陷且完整门禁后切换对应工作器。
九个 inactive/404 来源、已找到同摘要照片的旧取消包和 download_failed Job 保持各自边界；
不能借封面/副本补齐重发 Telegram或复活删除内容。全覆盖须当前有效数据逐项及鉴权可读证据。

## 01:21 北京时间恢复验收更新

上述 480p 对象已由正常工作器实际恢复登记，原画绑定保持，未手改 checkpoint。
Player 按该索引完整摘要、包和对象匹配，鉴权精确 Range 206 / Content-Range / 两字节读取通过，匿名请求 401。
本轮恢复的另一高度尚在处理，完整任务未结束；不要以恢复一档推断两档齐全。
17:21:55 UTC 有效覆盖封面 817/910、480p 88/871、720p 83/729、收藏封面 36/36。
重复来源的恢复不能直接累加为去重视频覆盖，后续继续 monitor 与实际读取核对。
副本此时工作文件约 327.5MB，近期写入后在网络/确认阶段，无解码进程、无新失败分类；
后续检查 cause_error 和原任务进展，不盲目扩大 timeout 或宣称上传故障已根治。

## 01:26 北京时间最新状态

17:26:31 UTC 封面 823/910、480p 89/871、720p 83/729，收藏封面 36/36。
副本新版本已出现 complete written=2/recovered=1 及后续正常 written=2，恢复链路实际持续成功。
同时一次 archive_upload_unverified 的 cause_error=TimeoutError，发送/响应阶段尚未分清；
不要盲目扩大 deadline，继续按原冷却核对远端真对象与旧任务，已登记单档保留。
封面失败 7 项 ValueError/有效唯一 6，副本有效失败唯一 15；后续优先核实真正持续阻碍的来源，
不新增并行下载/解码，不把旧索引、目录存在、done 数或一次 Range 当全覆盖。
仍缺封面 87、480p 782、720p 646；维护与现有 tgvio 跟进继续，完成前不结束或重建自动化。
本轮代码实际运行仍 668fcd7（副本）与 a5fd3e0（封面），文档推送只做源码快照同步。
