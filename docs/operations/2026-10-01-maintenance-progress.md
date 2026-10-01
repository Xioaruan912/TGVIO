# 2026-10-01：持续补齐巡查

本页追加只读巡查结果，不把任务数等同于真实有效视频覆盖。
部署边界与完成判据继承 [维护发布回执](2026-10-01-maintenance-completion-release.md)。

## 12:42 UTC / 20:42 北京时间

| 项目 | 可用投影 | 应有数量 | 待补 |
|---|---:|---:|---:|
| 封面 | 483 | 910 | 427 |
| 480p | 71 | 871 | 800 |
| 720p | 67 | 729 | 662 |
| 收藏封面 | 36 | 36 | 0 |

相对上次 12:23 UTC / 20:23 用户回报：封面增加 23 条，480p 增加 4 条，720p 增加 3 条。
封面 checkpoint done 513 / failed 17 / pending 487；
副本 done 67 / failed 16 / pending 934，含旧失效来源，不能当有效覆盖率。
封面工作器运行 a5fd3e0，副本运行 763a1f6；容器身份不变，重启 0、OOM false。
Bot/Player 仍 healthy，身份及重启数不变。Player 和两个维护 checkpoint quick_check 均通过。
磁盘可用约 22.96 GB；没有磁盘等待证据。

有界 HEAD 抽查三个当前有效失败来源：封面 ValueError 一项、封面 RuntimeError 一项、
副本 RuntimeError 一项，均返回 200 且 Content-Length 与源记录一致。
这只证明当时可访问及声明大小；不能据此声称完整文件可解码或该项补齐成功。
网络失败与首次失败仍在冷却重试，当前没有证据支持盲目修改编码器或重启服务。

历史 download_failed Job 已有启用的自动恢复策略，max_attempts=3，
恢复状态 exhausted、记录累计 attempt_count=6。它没有归档包，不能算成功。
仅识别到 Telegram 相关错误，未确认具体可修复原因；不清零策略或自动触发 Telegram 发布。
已确认在另一归档中的旧取消照片继续单列为历史包状态，不重播。

本轮未修改应用代码、未跑生产媒体单测、未重启或部署服务；
仅记录巡查并修正局部 AGENTS.md 末尾误写的字面换行符，规范含义不变。
私有证据为 completion-release 的时间戳进度、heartbeat-errors-latest.json 和诊断摘要。
补齐仍未完成，每小时跟进继续，VPS worker 自身持续重试。

## 13:49 UTC / 21:49 北京时间

| 项目 | 可用投影 | 应有数量 | 待补 |
|---|---:|---:|---:|
| 封面 | 565 | 910 | 345 |
| 480p | 75 | 871 | 796 |
| 720p | 72 | 729 | 657 |
| 收藏封面 | 36 | 36 | 0 |

相对上次 20:51 用户回报的 500/72/68，分别增加 65/3/4 条。
13:43 巡查封面 failed 已降到 2；13:49 为 3（ValueError 1、WebDavArchiveError 2），
不能将变化中的失败数或 checkpoint done 603 当成覆盖率。
副本 checkpoint done 74 / failed 20 / pending 923；有效失败来源任务 9、唯一视频 7。
两个工作器版本、身份、资源预算不变，无重启/OOM；Bot/Player healthy，三个 checkpoint/catalog quick_check 通过。
磁盘可用约 22.77 GB，归档仍 committed 51 / cancelled 1、Job succeeded 54 / failed 1。

只读有界来源诊断抽查 12 个当前有效失败任务（包含重复来源）：
HEAD 均为 200，声明长度与源记录相符，源大小均在现有 4 GiB 上限内。
其中封面 ValueError 项累计失败 5 次，仍有约 39 分钟退避；
其首尾各一字节的鉴权 Range 返回 206，Content-Range、长度及实际一字节读取通过。
这不能排除较大分段读取失败、解码失败或输出校验失败，不以 HEAD/短 Range 断言完成。
副本待重试项未因达到冷却时间立即完成，仍受单工作器批次和当前任务预算约束。
资源抽查显示副本临时文件约 148 MB，最后写入距检查不足一秒，仍在下载；
封面临时逻辑大小约 420 MB、实际占盘约 4.2 MB（稀疏采样），未发现停止或磁盘等待证据。

历史 download_failed 恢复策略仍 exhausted、累计 attempt_count=6，未形成归档包；
不重置失败记录，不自动重发 Telegram。旧取消照片在另一有效归档的校验结论不变。
当前收藏投影仍 36/36，本轮没有重复逐张鉴权读取，沿用此前完整 36 张验收证据；
最终全覆盖仍需实时逐项及鉴权可读核验。

本轮没有确认新的可修复代码缺陷，没有应用变更或服务重启。
仅将巡查和交接文档推送并同步 VPS 源码快照，运行镜像继续保留各自已验证版本。
私有证据保存在 completion-release 的时间戳进度、heartbeat-range-latest.json、
heartbeat-resource-latest.json 及其本地结果；不向仓库写入原媒体标识、路径或凭据。
补齐仍未完成，工作器持续执行及自动重试，每小时跟进保持启用。

## 14:48 UTC / 22:48 北京时间

| 项目 | 可用投影 | 应有数量 | 待补 |
|---|---:|---:|---:|
| 封面 | 643 | 910 | 267 |
| 480p | 80 | 871 | 791 |
| 720p | 74 | 729 | 655 |
| 收藏封面 | 36 | 36 | 0 |

相对上次 21:51 用户回报的 573/75/72，分别增加 70/5/2 条。
封面 checkpoint done 679 / failed 1 / pending 337，唯一失败为 ValueError；
14:43 的 TimeoutError 和 WebDavArchiveError 已不再失败。
副本 done 77 / failed 25 / pending 915，含无效历史来源；当前有效失败任务 13、唯一视频 11。
失败数及 checkpoint 完成数不代替有效投影或最终可读验收。

本轮优先抽查八项失败来源，HEAD 全为 200、声明大小匹配且在 4 GiB 源上限内。
封面 ValueError 的首尾一字节鉴权 Range 再次通过状态、长度及实际读取校验。
该项累计失败 6 次，抽查时尚余约 105 分钟冷却；次数没有清零。
追加只读元数据检查：manifest/_COMPLETE 摘要绑定、covers.json v2 绑定均有效，
该视频没有 cover 条目或现有绑定低清副本，源大小在 12 MiB 内。
不能据此确定整文件 Range、解码或 JPEG 校验的具体失败阶段，也不能直接判为损坏或源丢失。
未启动额外媒体解码、未扩大取样预算，等待既有工作器到期重试。

资源两次抽查副本临时文件总大小约 48 MB 后增至 66 MB；
其间日志出现 written=2 的真实完成记录，并开始下一项，未发现全局停滞。
封面维持稀疏有界采样。磁盘可用约 23.00 GB。
两个工作器保留各自版本 a5fd3e0/763a1f6、身份和预算，重启 0、OOM false；
Bot/Player healthy 且身份不变，catalog 和两个 checkpoint quick_check 通过。
本轮读取 expected-workers 及 completion-cover-memory 的发布/隔离验证证据，没有按文档 HEAD 重建容器。

收藏实时投影仍 36/36，此前逐张鉴权 JPEG 验收结论保留；
本轮没有重复全量媒体读取，最终验收仍需实时逐项证据。
归档 committed 51 / cancelled 1、Job succeeded 54 / failed 1；历史异常边界不变。
download_failed 自动恢复仍 exhausted、累计次数 6，不自动重发 Telegram。
没有确认可修复的新代码缺陷；本轮仅推送巡查/交接并同步源码快照，不切换服务。
仓库卫生及差异检查通过；没有应用代码变更，未重复运行完整媒体回归。
私有证据为时间戳进度、heartbeat-priority-range-latest.json、
heartbeat-cover-binding-latest.json、heartbeat-resource-first-1443.json 及后续资源结果。
全库尚未补齐，后台持续补齐与自动重试、每小时跟进继续。

## 16:08 UTC / 2026-10-02 00:08 北京时间

覆盖封面 736/910、480p 85/871、720p 78/729，实时收藏封面 36/36；
相比 23:32 用户回报新增 40/1/0，仍未全库完成。
副本失败阶段抽查、只更新副本诊断工作器及新的逐张收藏鉴权验收，
见 [诊断发布回执](2026-10-02-rendition-diagnostics-release.md)。
副本现在运行 7e3c35b，封面仍 a5fd3e0；Bot/Player 未切换。
两个工作器各自检查点和原重试预算保持。下一轮须读取新的 telemetry，不只重复 HEAD。
