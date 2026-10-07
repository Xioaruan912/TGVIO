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

## 2026-10-02 02:19 北京时间封面细节修复与继续入口

最新 [明亮画面封面修复回执](../operations/2026-10-02-cover-detail-release.md)：
封面实际运行 64fdf442b1c163a361613cc50b09f863e9602484，副本保持 668fcd7，
Bot/Player 身份、镜像、重启与版本保持不变。封面仍 768MiB/.75CPU/64PID、输入/输出单线程。
代码完整门禁 Python 1058、Node 215、TS/Vite 通过；候选 537 个文件及镜像逐项核验，
原 checkpoint 每行保留，旧 a5fd3e0 停止回滚和独立 SQLite 备份保留。

实证修复一个约 99% 像素偏白但有真实细节的视频：
旧算法返回空，完整源哈希与正常解码通过；候选保留亮度范围与方差可辨的内容，
仍拒绝纯黑白/低幅噪声，不增加位置、Range 或执行时间预算。
受控实际修复一张 5123-byte 290×640 JPEG，读取原媒体 1.5MiB、28.47 秒，
上传后完整哈希/原画绑定/原清单不变通过，未手改任务完成/累计次数。
正常 catalog 同步后，精确该封面 Player 鉴权 200/private/JPEG/完整哈希通过，匿名 401；
实时 36 条收藏再次逐张鉴权及完整哈希通过。

18:19:47 UTC 有效覆盖：封面 900/910，480p 98/871，720p 94/729，
尚缺 10、773、635。两个内核 oom/oom_kill 为零、工作目录持续写入，磁盘约 22.27GB。
剩余十个封面逐项核对：四个正常 done 待后续 catalog 确认，六个唯一来源失败，
一个有两归档位置；不按 done 补算覆盖、不将所有失败假定为本次误判。
副本仍有 upload_unverified TimeoutError、一次 metadata PUT 405 和未分类 ValueError；
正常 written=2 持续，失败保持冷却。八个有效失败来源 HEAD/大小及端点 Range 正确，
不能以两字节证明完整可解码。

后续先 completion-release/monitor.py；expected-workers 已仅更新封面为 64fdf44，
副本继续 668fcd7。completion-cover-detail 私有目录保存 clean/pushed 候选、源码镜像校验、
部署回执、检查点、单来源有界真实验证、Player 鉴权和剩余封面状态。
其 call-remote.py detail-player-remote.py / detail-sync-status-remote.py /
missing-cover-state-remote.py 可用于只读复核；
missing-cover-refs.json 仅远端 root600 保存私有来源，禁止输出原 ID、路径或图像。
source-diagnostic-remote.py 位于 completion-cover-diagnostics，针对旧 a5fd3e0 且曾完整读小来源，
不得照旧重跑或重复消耗同一文件预算。新 update-remote.py 已完成一次切换，
不能因文档 HEAD 或重复 heartbeat 盲目部署，确认脚本也不应重复改预期版本。

下一轮优先核对正常 catalog 的四张新封面与新算法到期重试的六个来源，
若仍失败再逐项拆分截断容器/位置/解码/内容原因；优先复用已有受控副本并保留总资源边界。
同时继续核实副本传输/索引确认阶段，不能把已验证单档、目录存在或任务 done 当全覆盖。
九个 inactive/404 来源不复活；同摘要照片的旧取消包不重播；
download_failed 原冻结 max3/count6/exhausted 继续单列、不重置、不重发 Telegram。
维护与 tgvio 跟进继续，完成前须当前逐项与鉴权读取证据。

## 02:22 北京时间目录确认

18:22:56 UTC 正常 catalog 已确认上述四张新封面，
实际封面 904/910（缺 6）、480p 100/871（缺 771）、720p 95/729（缺 634），
收藏仍 36/36。不要再将这四张标为待同步；剩余六个缺封面来源待原冷却重试与逐项原因核对。
Bot/Player 健康、所有容器零重启/无 OOM，实际封面 64fdf44、副本 668fcd7 不变。
本轮文档只做 clean/pushed 源码快照同步，维护及每小时跟进继续，尚未全覆盖。

## 2026-10-02 03:43 北京时间低对比与后备位置修复

最新 [低对比及开头空白封面回执](../operations/2026-10-02-cover-lowcontrast-release.md)：
封面实际 eaa83cdb7150daa8a135b8cbb1616acd45625051，副本继续 668fcd7，
Bot/Player 容器、镜像、版本和重启计数保持原身份。
32ccdb0 先修复低对比跨度门槛，eaa83cd 再在原样本全部失败后固定检查第 5/10 秒；
不追加 Range，仍头尾累计 12MiB、0.5MiB/s、768MiB/.75CPU/64PID、输入/输出单线程，
单命令 12 秒、sample 总 180 秒不变。最多增加两个候选/四个命令。
完整门禁分别 Python 1059/1060、Node 215、TS/Vite 通过，真实隔离媒体先红后绿。
每次只切对应封面工作器，冷扫描 1017、检查点每行保留、独立备份与
64fdf44/32ccdb0 停止回滚均保留；不因文档 HEAD 重建其他工作器。

本轮五个原缺图来源都已生成真实封面，并逐张通过 Player 鉴权
200/private/JPEG/尺寸/完整 SHA256，匿名 401，验收后退出登录。
36 条当前收藏再次逐张验证通过。受控补齐采用同一个已验证 runner，
原封面工作器停止且持有维护锁，所有检查点行不变；
已存在封面只验证、不重复解码，不手改 done 或累计次数。

19:43:13 UTC 当前有效覆盖：封面 909/910，480p 124/871、720p 116/729，
缺 1、747、613。最后缺图来源约 460.27MB、1920 高、约 957.85 秒，
12MiB 内仍无可用帧，不能称为损坏或已补齐；此前无绑定副本，须按实时数据再核对。
后续优先核对现有样本可解码的位置或已完整验证、绑定原画的副本，
保持有界预算，不能批量浏览器解码或改用假封面。
封面旧失败 79/有效唯一 7 不表示真实缺图数量；已修好来源待原冷却确认。
副本实际 written=2/recovered=1 和正常 written=2 都持续，
仍有 TimeoutError、metadata PUT 405、upload status 405 和未分类 ValueError 待区分；
下一轮应优先分类当前有效副本失败与最后缺图源，不重复下载已验好的来源。
磁盘此时约 21.74GB，可用资源仍有界，未出现 OOM。

继续先 completion-release/monitor.py；expected-workers 已仅更新封面至 eaa83cd，
副本仍 668fcd7。completion-cover-late 保存本次 clean/pushed 候选、540 文件/镜像校验、
部署、独立检查点和原六来源的不可变 bounded-repair-refs.json（远端 root600）。
call-remote.py repair-player-remote.py 可复核本轮五个已修好源，
它读取不可变批次快照，不依赖会变化的 missing-cover-refs.json；
missing-cover-state-remote.py 可生成实时缺图私有引用，不得输出原 ID、Archive 路径或图像。
bounded-repair-remote.py 已执行一批，不照旧重复整个批次、部署或 confirm 脚本；
update-remote.py 与 confirm-deployment.py 是一次性发布回执，不在后续盲目重跑。
completion-cover-lowcontrast 的后备位置只读诊断已证明一来源第 10 秒有真实内容，
不要重复读取这个已修好的文件。各诊断保留原清单、索引、检查点与其他运行身份。
下一次因实证缺陷修改时才从 clean 已推送提交发布对应工作器。

九个 inactive/404 来源不复活；同哈希照片的旧取消包不重播；
download_failed 原冻结 max3/count6/exhausted 保留、不重发 Telegram。
当前不结束 tgvio 自动化，直到当前可用视频逐项满足封面/应有副本及鉴权可读证据。

## 2026-10-02 04:01 北京时间最后缺图来源诊断

[HEVC 来源巡查](../operations/2026-10-02-hevc-source-audit.md)记录本轮只读证据。
运行封面 eaa83cd、副本 668fcd7，Bot/Player 身份不变，无应用代码发布。
20:01:09 UTC 封面 909/910、480p 129/871、720p 120/729，
仍缺 1、742、609；36 收藏封面再次全部鉴权/JPEG/尺寸/完整哈希通过。
封面原失败来源自然冷却后正常确认，检查点 done 942/failed 75，不替代覆盖。

最后来源 HEVC/hev1、约460.27MB、1072×1920/957.85s、无已绑定副本。
12MiB 严格取帧全部 NAL 错误，没有进入亮度校验；包表确认首关键包位于已填充头部，
hvcC 声明四字节长度而所读首包不符合该结构，降低探测也无效。
四次有界只读诊断累计18.5MiB，单次不超过12MiB；
所有原清单/索引/检查点行不变，同一封面容器恢复，OOM为零。
这些证据不等于完整原画哈希或整文件损坏；不得伪造封面、降低严格校验来宣称成功。
下一轮不重复同一组诊断或盲目加位置，优先正常副本完整源哈希/解码、
安全来源恢复或经过完整验证的绑定副本；没有实证代码缺陷不发布新版本。

私有 completion-cover-late 的 last-diagnostic-remote、last-packet-remote、
last-probe-remote、last-nal-remote 保存匿名结论；均已执行，不照旧重跑。
missing-cover-state-remote.py 才用于实时缺图状态。
原 immutable bounded-repair-refs 不变，不再重跑六来源补齐批次。

当前有效副本 ValueError 失败精确任务键匹配0，近期尾日志未分类不能当现存失败。
completion-rendition-recovery/valueerror-metadata-remote.py 保存匹配方法与本轮结论；
active-upload-phase-remote.py 保存四个有效上传失败索引观察：
均绑定有效，两个已有远端大小正确的480p，两个暂无；这不是完整副本读取验收。
仍有405/TimeoutError按原预算冷却和恢复，normal written=2/recovered=1持续。
磁盘约21.69GB，副本峰值685.35MB，无OOM。
frozen-job-remote.py 当前确认 download_failed 仍 enabled/max3/count6/exhausted。
九个 inactive/404 与同哈希照片取消包沿用既有单列证据，不复活或重播。
只推送文档、同步源码快照并核验所有容器身份不变，自动跟进继续至逐项全覆盖。

## 2026-10-02 05:07 北京时间失败公平调度发布

[公平重试回执](../operations/2026-10-02-maintenance-retry-fairness-release.md)为最新实际发布。
副本运行 ddedb1c1b111af63379c662de0d18da37f002133，封面保持 eaa83cd，
Bot/Player 容器、镜像、版本、健康和零重启不变。
发现旧失败选择按发现列表固定前缀，批次超过冷却后会重复前面的失败项，
后续到期失败可能持续等待；当前22有效失败均attempt1、最久约6.8小时。
两项隔离行为回归旧实现失败、新实现通过；改为到期失败按上次尝试从旧到新轮转，
保留冷却、累计次数、三/十重试配额、新任务顺序与有界资源。
38项针对性、完整Python1062/Node215/TS/Vite/架构治理通过（WSL3.13.5）。
543源码文件/模式/完整集合和归档/候选镜像RootFS/Config/架构/revision核验；
VPS image ID和本地不同但等价字段逐项确认，部署使用实际VPS ID。
停原668fcd7、备份检查点、dry-run仍1017任务，每条原记录不变；
Python3.11/UID65532/无网络隔离调度门禁和复制的1017条检查点核验通过。
保留668fcd7停止回滚、镜像和独立SQLite备份；expected-workers仅更新副本。

21:05:06 UTC 当前封面907/908、480p147/870、720p137/729，缺1/723/592。
有效原画从910到908是新增两个已记录删除、inactive且远端HEAD404；
一个需要480p、均不需要720p，不复活这两来源，原九个历史inactive/404仍单列。
当前36收藏再次全部鉴权/尺寸/JPEG/完整哈希通过，匿名401，验收后退出。
最后HEVC缺图原清单绑定有效，暂无绑定副本、副本状态pending/attempt0，
封面自然失败到attempt7仍冷却，不重复四个既有取帧/包表诊断或扩大预算。

私有 completion-retry-fairness 保存 clean/pushed 候选、543文件镜像验证、
独立检查点/部署回执、隔离Python3.11门禁和真实调度后验：
21:07 新进程完成scan并start原failed/attempt1、等待24873秒的最旧到期失败，
准确匹配停机前检查点中的最旧待重试项；此时尚无complete，不能说恢复完成。
call-remote.py retry-start-remote.py / telemetry-remote.py / memory-events-remote.py
可只读复核新进程。update-remote.py / confirm-deployment.py已一次性执行，不重跑。
schedule-audit-remote.py 做实际发现及临时检查点审计，需要时才运行，不重复全量元数据扫描。
completion-cover-late/missing-bound-status-remote.py 可只读核对最后缺图的原画绑定、
已绑定副本及副本检查点；recent-deleted-remote.py已确认两个新删除，不重播。

后续先monitor、实时覆盖/真实恢复结果/405与TimeoutError阶段；不把running或done数当全覆盖。
封面仍768MiB单线程、副本2GiB/1.5CPU/96PID，OOM为零，磁盘约21.56GB。
download_failed Job仍enabled/max3/count6/exhausted，不重置/重发Telegram；
同哈希照片取消包既有验证保留，不重复下载或重播。
有新的实际恢复才核对绑定、完整内容与鉴权接口；暂未全覆盖，tgvio跟进继续。

### 05:15 真实恢复已确认

新副本最旧失败任务已complete/written1/recovered0，准确该来源两清晰度
原清单/索引/Player行绑定、480p/720p DTO及鉴权Range206通过，匿名401、
logout成功、catalog_sync_pending=false；只读每档bytes0-1，不等于完整远端MP4哈希。
私有completion-retry-fairness/first-retry-player-range-remote.py及JSON保存对应证据，
首次不可变引用first-retry-ref.json仅远端root600；后续不要盲目再次做完整读取。
又一失败任务complete/written1/recovered1；第三个仍metadata PUT405，
继续按原冷却/资源预算核实提供端语义或真对象，不盲目延长deadline、删除/覆写对象。
21:15:51 UTC 封面907/908、480p148/870、720p138/729，缺1/722/591。
副本done157/failed35/pending825，有效失败21任务/17唯一来源；
编码与写入持续，新副本峰值390.71MB，无OOM，磁盘21.45GB。
实际工作器ddedb1c/eaa83cd保持，文档后续只同步源码；全覆盖未完成、自动跟进继续。

## 2026-10-02 05:56 北京时间上传只读巡查

最新回执：[补齐与上传巡查](../operations/2026-10-02-maintenance-upload-audit.md)。
21:56:29 UTC 当前有效原画908，封面907；480p161/870，720p150/729，
比上一轮分别增加13/12；36当前收藏又一次全部鉴权JPEG/完整哈希/尺寸通过。
当前有效副本失败22任务/19唯一来源；精确键匹配当前有效ValueError为0，
全局18个ValueError与最近5次未分类不能解释为当前有效来源损坏。
副本done172/failed40/pending805是检查点，不替代覆盖。

新 completion-retry-fairness/dav-readonly-remote.py 已执行四来源受控只读审计：
两个metadata405、两个upload405，四索引存在且绑定，OPTIONS200声明PUT，
索引都是文件，三个有大小正确的本来源480p，一个没有；
不等于完整副本哈希、失败目标证明或405修复。未做PUT/DELETE/覆写。
继续公平重试与原累计次数/冷却，不盲目增加deadline或根据OPTIONS假定上传成功。

最后HEVC缺图原绑定有效，无绑定副本、副本pending/attempt0，封面attempt7冷却；
不重复四个已做的取帧/NAL诊断。正常副本完整源验证或现有安全恢复才推进，
不伪造封面、不复活历史九个或新增两个已删除来源、不重发照片包/冻结Job。
运行ddedb1c/eaa83cd与Bot/Player身份不变，OOM0，副本峰值496.46MB，
实际编码/写入持续，磁盘21.38GB。只同步文档源码，不重建进程。
下一轮先monitor和真实进展，必要时安全核查405，自动跟进继续至逐项全覆盖。

## 2026-10-02 06:55 北京时间补齐与恢复后验

最新 [上传巡查](../operations/2026-10-02-maintenance-upload-audit.md)新增06:55续查。
22:55:13 UTC封面907/908，480p173/870、720p162/729，
比上一轮两档各增加12，仍缺1/697/567；36收藏再次鉴权JPEG/完整哈希/尺寸通过。
副本done185/failed42/pending790；有效失败22任务/22唯一来源，
全局ValueError20但当前有效精确键匹配0，仍有upload/metadata PUT405。
不重复既有四来源OPTIONS，不把正常恢复或OPTIONS200当405解决。

最新真实complete/written2/recovered1来源的原清单/_COMPLETE/原画大小、
绑定索引和Player原画/两档副本行/DTO均核验；
480p、720p各bytes0-1鉴权206通过，匿名401、退出成功、
catalog_sync_pending=false；不是完整远端MP4读取/哈希或全覆盖。
私有completion-retry-fairness/recovered-current-range-remote.py及JSON保存证据，
本轮不可变recovered-20261001-2251-ref.json，脚本已执行，
之后最近成功任务会变化，不照旧重复或覆盖first-retry私有引用。

最后HEVC缺图binding有效、无副本、副本pending0、封面attempt7自然冷却；
不再取帧/包读，继续正常完整源验证或现有安全恢复。
冻结Job本轮仍enabled/max3/count6/exhausted，保持不重发；
原九个、两个用户删除、同哈希照片取消包保持既有边界。

运行ddedb1c/eaa83cd及Bot/Player身份/健康不变，重启0、OOM0，
副本峰值576.85MB，临时写入近期5秒；磁盘21.52GB，quick_check通过。
本轮只读审计/文档推送/独立源码同步，不重建任一工作器；
未全覆盖，tgvio自动跟进继续。下一轮先实时覆盖、真实恢复、上传阶段和资源。

## 2026-10-02 08:08 北京时间元数据响应核验发布

最新 [元数据核验发布](../operations/2026-10-02-metadata-put-verification-release.md)。
副本维护实际809782137ae3a080ec1957f36d7c39101339e083，
image2129d1706c8b4638bd556360114abc4336cf430b776b32755cc6bf8e49163440；
封面eaa83cd、Bot/Player身份/版本未变，四容器零重启，OOM0。
元数据PUT非2xx过去直接failed；新实现仅在文件、大小和完整字节都匹配payload
时接受结果，错内容/不可读/目录仍失败并保留原HTTP状态，不额外PUT/DELETE/MOVE，
不增加timeout/轮询，不清零检查点或改变失败冷却。
旧实现3项初始回归先红，新实现4项、35目标测试、完整Python1066/Node215/
TS/Vite/治理通过（WSL3.13.5，browserfalse）；VPS候选3.11/UID65532/network-none
4回归及临时1017检查点公平调度门禁通过，不声称全套3.11/浏览器验收。

546源码文件/模式/集合和镜像RootFS/Config/revision精确核验，本地与VPS imageID不同，
使用实际VPS ID发布。停dded、SQLite一致性备份、dry-run1017任务、原每条检查点不变，
保留停止dded容器/镜像/独立备份，expected-workers只更新副本。
completion-metadata-verify/update-remote.py、confirm-deployment.py已一次执行，不重跑。
00:08首轮scan/start尚待结果，不能将running写成处理成功；后续先真实事件/覆盖/资源。

00:08:30 UTC封面907/908、480p194/870、720p185/729，缺1/676/544；
36收藏本轮全鉴权JPEG/完整哈希/尺寸通过，匿名401、退出成功。
副本done211/failed47/pending759，有效失败24任务/24唯一来源；
全局ValueError23但当前有效精确键匹配0。最后HEVCbinding有效、无副本、
副本pending0/封面attempt7自然冷却，不重复四个既有取帧/NAL诊断。
旧dded日志24个当前来源405，8包中7有别的成功写、6有随后成功写，
一项metadata405约9768秒后written0完成；不等于首响应已可读或新分支生产触发。
原completion-retry-fairness的retry-outcomes-pre8097821/failed-profile-pre8097821
是旧进程快照；新进程日志不能覆盖旧证据。文件405/提供端根因仍未确认。

原九个、两个已删、照片取消包及冻结Job不复活/重播/重发；
磁盘21.29GB，quick_check通过。仅文档源码同步不触发其他重建。
继续自动跟进至当前可用媒体逐项真实封面、应有副本与鉴权可读证据。

### 08:23 已确认新进程实际处理与鉴权接口

新进程scan/start后真实complete/written2/recovered0，后续持续complete；
00:23:19 UTC封面907/908、480p199/870、720p189/729，缺1/671/540，
副本done216/failed46/pending755，有效失败23任务/23唯一来源。
初验该来源catalog_sync_pending=true；两条元数据实际被Player校验器及只读客户端
接受、无删除标记。同步正常完成间隔约5–6分钟，配置poll60是轮询等待，
不是每个包60秒内完成；未强制刷新、改库或重启Player。
随后同一metadata-first-ref的480p/720p DTO/索引/鉴权Range bytes0-1全通过，
匿名401、退出成功、catalog_sync_pending=false；不是完整MP4内容验收，
也没有证据说该来源实际触发非2xx确认分支或全部405已根治。
completion-metadata-verify/first-player-range、first-catalog-parse、player-index-view、
player-sync-log保存私有匿名结论；不可变metadata-first-ref勿覆盖，Range脚本已执行
不要按“第一条start”盲目重跑，旧fairness first-retry私有引用保持原样。
新副本OOM0/峰值425.37MB；身份及quick_check保持、磁盘21.18GB。
继续自动补齐最后缺图与所需副本；只同步文档源码，不重复发布。

### 08:53 自动补齐继续

最新[元数据核验发布](../operations/2026-10-02-metadata-put-verification-release.md)新增08:53巡查。
00:53:45 UTC封面907/908、480p212/870、720p199/729，较08:25增加12/9；
副本done226/failed46/pending745，有效失败22，当前有效ValueError精确匹配0。
新8097821日志2个有效来源metadata405尚无同源随后complete，不能宣称405根治。
36当前收藏再次全部鉴权JPEG/尺寸/完整SHA256通过。
最后缺图binding有效、无绑定副本、副本pending0，封面自然attempt8；
不重做取帧/NAL诊断或放宽预算，等待正常副本源验证/受控恢复。
OOM0，编码持续，磁盘21.27GB；8097821/eaa83cd与Bot/Player身份、零重启保持。
私有completion-metadata-verify/audit-20261002-0053保存本轮证据和运行范围，
旧pre8097821关联回执保持。只推文档、同步独立源码，不重复发布；
自动跟进继续，下一轮从monitor和当前missing状态开始。

### 09:57 新上传失败与补齐进展

最新[元数据核验发布](../operations/2026-10-02-metadata-put-verification-release.md)新增09:57续查。
01:57:21 UTC封面907/908、480p226/870、720p213/729，两档比09:00各增加14；
副本done239/failed52/pending726，有效失败27，当前有效ValueError精确匹配0。
新8097821日志metadata405有2、upload405有3、upload_unverified/TimeoutError有2；
五个有效405来源尚无同源随后complete，不宣称修复提供端全部问题。
最新四WebDav失败索引绑定字段匹配/目录有效，三已有大小正确480p、一暂无；
不是完整MP4哈希/两档鉴权验收。失败后新start继续，资源预算/累计次数保持。
36收藏再次全部鉴权JPEG/尺寸/完整哈希通过；最后缺图binding有效、无副本、
副本pending/attempt0、封面attempt8；不重做既有HEVC取帧诊断。
OOM0、副本峰值672.04MB、磁盘21.19GB，四运行身份/零重启不变。
冻结Job本轮仍enabled/max3/count6/exhausted，不重发；
删除来源/照片取消包不复活或重播。
私有completion-metadata-verify/audit-20261002-0157保留本轮运行范围及13项证据。
本轮仅文档源码同步，不改代码/部署工作器；自动跟进继续至逐项全覆盖。

### 10:03 用户继续核查

02:03:54 UTC封面907/908、480p230/870、720p214/729，比09:59增加2/1；
新副本两次written2完成，有效失败27、五个405来源仍无同源随后complete。
最后缺图binding有效，无副本、副本pending/attempt0、封面attempt8，继续原冷却；
不再重复既有取帧或几分钟前36收藏图片读取。OOM0/零重启/身份不变，磁盘21.25GB。
私有completion-metadata-verify/audit-20261002-0203保存六项证据；
没有代码修改或服务切换，后续自动巡查从实时monitor开始，未全覆盖。

### 10:08 用户询问单任务慢的原因

已核查当前8097821日志：31有效任务成功、58副本新增，中位131.8秒；
7次上传失败累计1262.5秒，1.5核上限确有CPU throttling，仍单任务有界处理。
performance-remote.py及JSON保存匿名时间/资源证据；没有分阶段计时，
不声称某阶段精确占比或增加资源后的倍数。未变更运行配置。

### 11:02 恢复结果鉴权通过与封面暂时读取失败

最新[元数据核验发布](../operations/2026-10-02-metadata-put-verification-release.md)新增11:02续查。
03:02:20 UTC封面907/908、登记480p249/870、720p231/729，比10:03增加19/17；
副本done259/failed50/pending708，有效失败25、有效ValueError精确键匹配0。
02:55六个有效405来源暂无同源随后complete，后续又一upload405，仍待恢复。
02:54:43 written2/recovered1精确来源先Player同步待完成，
随后同一不可变recovered-20261002-0254-ref两档绑定/DTO/鉴权Range0-1全通过，
匿名401、logout成功、pending=false；不是完整MP4或全覆盖。
recovered-0254-range-remote固定完成时间、已验收，不照旧重跑；旧引用保持。
36收藏初验502失败，单列保留；有界一次重试版最终全36鉴权/完整哈希通过，
验收中一次502恢复；没有改Player生产重试逻辑。
一当前Timeout来源480p大小先false、后stat实际等于预期1,937,245；
不据波动宣称源损坏或两档完成。最后缺图仍binding有效/无副本/pending0/封面attempt8。
OOM0/四身份零重启、磁盘21.13GB、冻结Jobenabled/max3/count6/exhausted边界保持。
completion-metadata-verify/audit-20261002-0302保留18项证据，未修改应用或扩大资源；
仅文档源码同步，自动跟进继续至逐项全覆盖。

## 2026-10-07 13:16 北京时间实际复核

本轮回执见 [10 月 7 日覆盖核查](../operations/2026-10-07-maintenance-coverage-audit.md)。当前有效原视频 1048，封面 1047，480p 759/1005，720p 653/845；50 条当前收藏封面均通过真实鉴权读取和完整哈希核验。未全覆盖。

monitor 已显式核对并接受有发布回执支持的新 Player f673061 与新封面 abd7ff1；旧保护基准留存，Bot 和副本 8097821 身份保持。后续不可再拿旧 Player/封面容器当唯一当前版本，也不可从 Git HEAD 重建运行进程。

私有监控仍在 2026-10-01/completion-release/monitor.py；expected-workers.json 已据实际、发布回执核实更新，新增 protected-services-20261007.json 与 baseline-reconciliation-20261007.json。本轮冻结证据另存 2026-10-07/maintenance-audit；读取结果仅匿名统计，不输出私有媒体标识或路径。失败统计已修正 6 小时封顶。

两项维护继续运行且 cgroup 无 OOM；当前有效副本失败 20 条（19 上传核验、1 媒体处理），上传超时明显耗时。最后同一 HEVC 无封面、无绑定副本，attempts 分别 28/16；保留自动重试，不复做此前有界首包/帧诊断，不无条件扩大预算。download_failed 当前 exhausted/count8，保持隔离，不清零、不重发 Telegram。下一次先跑已修正监控，再按实时数据重新核实覆盖、失败和读取。
