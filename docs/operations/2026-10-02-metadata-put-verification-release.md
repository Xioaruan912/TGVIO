# 元数据非成功响应的严格内容核验

当前任务中有 metadata PUT405；新进程日志至少一个该类任务后来 written0/recovered0 完成。
这不证明第一次响应时内容已可读，也不证明全部405根因。
代码缺口是非2xx元数据写响应直接失败，不核验已经提交的完整预期内容；
文件上传已有远端确认路径，元数据应使用更严格的字节验证。

新实现仅增加一个无轮询的stat/GET核验；目标必须是文件、大小正确且完整字节等于
本次payload，才能接受非2xx响应。缺失、错大小、同大小错内容、内容不可读、
读取异常、目录仍失败，保留原HTTP状态。没有额外PUT/DELETE/MOVE或增加timeout。
2xx原合同、失败次数/冷却、公平调度和输入/转码预算保持不变。

新增4项隔离回归，旧实现先失败，新实现35项目标测试通过。
完整Python1066/Node215/TS/Vite/规范门禁通过，WSL Python3.13.5，browser=false。
源码809782137ae3a080ec1957f36d7c39101339e083已推送且clean；
只构建副本维护镜像，封面/Bot/Player不重建。
实际VPS导入镜像、Python3.11隔离门禁、检查点备份、部署和后验记录如下。
不能把隔离修复证明或正常complete写成提供端405全部解决。

## 实际发布与隔离门禁

副本维护实际运行809782137ae3a080ec1957f36d7c39101339e083，
VPS导入镜像sha256:2129d1706c8b4638bd556360114abc4336cf430b776b32755cc6bf8e49163440。
本地镜像ID不同；RootFS、Config、架构、revision及传输包完整SHA逐项等价核验通过，
部署使用实际VPS镜像ID，不用Git HEAD或文档版本代替运行证据。
546个源码文件的哈希、模式、完整集合及源码归档核验通过。

切换前生产等价Python3.11/UID65532/network-none候选通过4项新增行为回归；
停原ddedb1c、SQLite backup一致性备份、只读dry-run扫描仍1017任务，
每个原检查点key/status/attempts/updated/error/written与备份一致。
复制1017条检查点到临时库的隔离调度门禁通过：
长批次公平轮转、到期重试3/10配额、新任务顺序、无重复及原副本不变。
dry-run计划中的974/826是发现任务的清晰度要求，含重复/历史，不能作当前覆盖。

保留原ddedb1c停止容器、镜像和独立检查点备份。
封面仍eaa83cd/768MiB、输入输出单线程；
Bot5dc86a7、Playerc740cfb保持，三者身份和重启数未变。
expected-workers只更新副本维护身份，未重启Bot/Player或重建封面。

## 当前数据与失败范围

00:08:30 UTC，当前有效原画908，真实封面907/908；
480p194/870、缺676，720p185/729、缺544。
相比06:58的174/162增加20/23，两档仍未全覆盖。
36当前收藏本轮再次逐张鉴权JPEG/尺寸/完整SHA256通过，匿名401、退出成功。
这是收藏封面完整读取，不是全库907张逐项鉴权验收或全副本完整MP4哈希。

副本检查点done211/failed47/pending759，当前有效失败24任务/24唯一来源；
全局ValueError23，精确当前有效任务键匹配0。
封面done945/failed72，当前有效失败只有最后1个；
检查点done/任务计划不作为覆盖替代。
最后HEVC原绑定有效、无绑定副本、副本pending/attempt0、封面attempt7自然冷却；
不再重复已有取帧/NAL诊断、扩预算、降严格解码或伪造封面。

原ddedb1c进程的只读日志关联显示24个当前有效来源发生405，
涉及8包，其中7包有别的成功写入、6包有随后成功写入；
样本不支持整个包永久只读的推断，但不证明提供端405根因。
一项metadata405约9768秒后complete/written0/recovered0，
当前检查点done；这不证明首次响应时已可读，也不证明新分支在生产已触发。
失败大小从小于16MiB到1GiB分布，不能将405统一解释为4GiB源预算超限。
公平调度下当前有效失败有attempt1/2，原冷却及累计次数未清零。

本次修复解决“非成功响应时不核验已经提交内容”的可复现代码缺口，
没有声称文件上传405、提供端行为或所有失败已消失。
新进程00:08刚启动，首轮扫描/重试结果继续跟进；不把容器running当成功处理。
维护cgroup OOM/OOM-kill0，四容器重启0；磁盘21,288,476,672字节，
Player及两检查点quick_check通过。
原九个inactive/404、两个已删除来源不复活；照片取消包不重播，
冻结download_failed Job保留既有次数和策略，不重发Telegram。

## 回执与后续

私有completion-metadata-verify保存red/green/check日志、clean/pushed源码候选、
镜像归档/候选核验、metadata/scheduler隔离门禁、独立检查点及实际deployment。
update-remote.py、confirm-deployment.py已一次性执行，不重复部署或覆盖回执。
completion-release/monitor.py与expected-workers.json是当前运行身份审计；
新副本8097821、封面eaa83cd分别核验。
旧completion-retry-fairness的retry-outcomes-pre8097821与failed-profile-pre8097821
保留原进程关联，后续新日志不能覆写或冒充旧进程证据。

后续从真实scan/start/complete、当前覆盖、405阶段和资源继续验证；
若需证明某任务恢复，精确绑定索引和鉴权接口后再报告。
未运行浏览器/真机或全视频播放验收；全覆盖尚未达成，tgvio自动跟进继续。
文档后续仅同步独立源码快照并核验容器不变，不重建其他进程。

### 08:23 运行与鉴权后验

新进程已scan/start并实际complete/written2/recovered0，后续complete持续。
初次该来源Player副本数0，不能当可播放验收；精确源绑定、当前校验器干解析
及Player生产只读客户端都接受两条索引，元数据有效、没有删除标记。
只读同步日志显示00:03:17、00:08:38、00:14:05、00:19:57正常完成，
active_videos908/rejected0，无失败事件；配置poll60秒，完整发现+等待的实际完成
间隔约5–6分钟，不能解释为每个来源60秒内立即同步，也未强制刷新/改库。

随后同一不可变metadata-first-ref.json的两档Player行/DTO/绑定索引核验，
480p/720p各bytes0-1鉴权206、Content-Range和两字节长度通过，
匿名媒体401、退出成功，catalog_sync_pending=false。
只是该来源Range抽查，不等于完整远端MP4哈希/解码；
也没有日志字段证明该生产任务触发了非2xx核验分支，不能宣称全部405已解决。

00:23:19 UTC，封面907/908，480p199/870、720p189/729，缺1/671/540；
副本done216/failed46/pending755，有效失败23任务/23唯一来源。
相比上一轮06:58两档增加25/27；磁盘21,183,770,624字节，quick_check/身份通过。
新副本cgroup OOM/OOM-kill0，实测峰值425.37MB；封面仍eaa83cd。
私有completion-metadata-verify/first-player-range-remote.py、first-catalog-parse、
player-index-view、player-sync-log及JSON保留从等待到实际鉴权通过的证据。
first-player-range已执行，不盲目重跑；首任务可能随日志裁切改变，
不可变metadata-first-ref不覆写；旧fairness的first-retry引用也不覆盖。
发布/确认脚本仍是一次性回执，不再执行；后续先实时覆盖、资源与真实重试结果。

### 08:53 当前覆盖与新进程重试巡查

00:53:45 UTC当前有效原画908，封面907/908；
480p212/870、720p199/729，仍缺1/658/530。
相比上一轮08:25的200/190，两档新增12/9，不把checkpoint done当覆盖。
副本checkpoint done226/failed46/pending745，有效失败22任务/22唯一来源；
全局ValueError24，但精确当前有效任务键匹配仍0。
当前新8097821进程日志有2个有效来源metadata PUT405，尚无同来源随后complete；
不能将非2xx完整内容确认修复写成所有提供端405已消失。
22有效失败中attempt2有4、attempt1有18，未重置累计次数。

最后缺图原清单绑定有效，无绑定副本，副本pending/attempt0；
封面自然重试至attempt8仍ValueError。保持原冷却、12MiB/768MiB/单线程预算，
不重复已做四次取帧/NAL诊断；等待正常副本完整源验证或现有受控安全恢复。
36当前收藏再次逐张鉴权200/JPEG/尺寸/完整SHA256通过，匿名401；
不是全库907张封面鉴权或全部MP4完整读取验收。

新副本实际FFmpeg编码持续，临时文件最近1秒写入；cgroup峰值434,053,120字节，
封面峰值84,774,912字节，两者OOM/OOM-kill0。
四容器身份/版本与expected-workers一致、零重启，Bot/Player健康；
Player和两个检查点quick_check通过，可用磁盘21,273,047,040字节。
无新应用缺陷证据，本轮仅只读审计与文档源码同步，不重建/重启任何服务。

私有completion-metadata-verify/audit-20261002-0053保存本轮11项证据，
明确runtime8097821/eaa83cd；旧pre8097821证据保留，不把两进程日志混用。
九个历史inactive/404、两个删除来源、旧照片取消包和冻结download_failed Job边界保持。
后续仍先实时monitor、最后缺图副本状态、真实405重试结果和资源；
全覆盖未完成，tgvio自动跟进保持。

### 09:57 补齐与新上传失败续查

01:57:21 UTC当前有效原画908，封面907/908，480p226/870、720p213/729；
比09:00两档各增加14，仍缺1/644/516，不能称全覆盖。
副本checkpoint done239/failed52/pending726，有效失败27任务/27唯一来源；
全局ValueError25、当前有效精确键匹配0。22到27的有效失败增长对应上传问题，
新8097821日志有2个metadata405、3个upload405、2次upload_unverified/TimeoutError；
五个当前有效405来源尚无同源随后complete，不能说提供端405或超时已根治。
失败后有新的start，不能仅从一次无FFmpeg快照推断整个工作器停滞。
当前有效失败attempt2有4、attempt1有23，保持累计次数及公平轮转/原冷却。

受控只读抽查最新四个WebDav失败来源：索引存在、包/清单绑定字段匹配、
renditions目录存在且为collection；三个有本来源480p且远端大小正确，一个无副本条目。
这只是索引/HEAD观察，不证明完整MP4内容、目标上传成功或两档补齐。
失败耗时样本约113–339秒，未增加deadline或盲目重复上传/扩大解码预算。
36当前收藏再次逐张鉴权200/JPEG/尺寸/完整SHA256通过，匿名401；
不是全库封面/所有副本逐项完整内容或真机播放验收。

最后缺图原绑定仍有效、无绑定副本、副本pending/attempt0，封面attempt8自然冷却；
不重复四次取帧/NAL诊断或把占位当封面，等正常副本完整源验证/安全恢复。
封面及副本cgroup OOM/OOM-kill0，副本峰值672,038,912字节，
四容器身份/版本保持expected-workers、零重启，Bot/Player健康；
三库quick_check通过、磁盘21,185,736,704字节。
冻结download_failed仍enabled/max3/count6/exhausted，不重置或Telegram重发；
九个旧inactive/404、两个删除和已核验照片取消包边界保持。

私有completion-metadata-verify/audit-20261002-0157保存本轮13项匿名证据，
明确实际8097821/eaa83cd运行范围，旧pre8097821回执不覆写。
本轮没有新实证代码缺陷，不重建任何工作器；文档推送、仅同步独立源码。
后续先monitor、新失败精确归属与真实重试结果、最后缺图副本状态和资源；
尚未全覆盖，tgvio自动补齐/重试继续。

### 10:03 用户要求继续核查

02:03:54 UTC封面907/908、480p230/870、720p214/729；
比09:59增加2/1，副本checkpoint done241/failed52/pending724，有效失败27。
新进程又有两次complete/written2/recovered0；五个有效405来源仍无同源随后complete。
最后缺封面source绑定有效，无绑定副本，副本pending/attempt0、封面attempt8；
不重复已有解码诊断。两个工作器OOM0，四容器身份/零重启不变，Bot/Player健康，
磁盘21,247,078,400字节，quick_check通过。
本轮没有重新取36收藏图片，09:57逐张鉴权证据保留，当前收藏数量/覆盖仍36/36；
未新增媒体内容验收或应用发布。私有audit-20261002-0203保留六项新巡查证据。
继续公平自动重试，未全覆盖；文档源码同步不得触发运行版本切换。

### 10:08 单任务耗时核查

当前8097821日志精确匹配有效来源：31个成功任务、新增58个副本；
成功任务中位131.8秒，近期两档各written2任务为134.6秒和254.7秒。
7次有效上传失败合计1262.5秒，分类metadata405两次、upload405三次、
upload_unverified/TimeoutError两次；失败消耗时间不等于全程停滞。
单维护进程CPU上限1.5核、内存2GiB；cgroup nr_throttled26140/nr_periods34660，
说明运行期存在CPU限速，不能换算成每个阶段占比或精确可加速倍数。
现有日志无分阶段计时，不能断言下载/转码/上传各占多少。
私有performance-remote.py/performance-latest.json保存只读实际证据，无原媒体标识输出。
本轮仅审计，未调大CPU/并发/预算或发布代码。

### 11:02 真实恢复、封面重试与当前覆盖

03:02:20 UTC有效原画908、封面907/908；登记480p249/870、720p231/729，
仍缺1/621/498。相比10:03的230/214，分别增加19/17，仍不是全部对象鉴权验收。
副本checkpoint done259/failed50/pending708，有效失败25任务/25唯一来源；
02:54时有效失败24，随后新upload405增至25。全局ValueError25，精确有效键匹配0。
02:55新进程日志有6个有效405来源、暂无同源随后complete；03:00又一upload405，
不能将正常恢复写成405全部解决或把checkpoint done当真实覆盖。

02:54:43实际complete/written2/recovered1来源，原清单哈希/_COMPLETE、
原画远端大小、绑定索引已核验。初次Player行0、catalog_sync_pending=true，
同步日志当时最近02:52:59正常完成，尚未发现新索引，无同步失败。
后续同一不可变recovered-20261002-0254-ref的Player两档行/DTO/精确对象绑定、
480p/720p各bytes0-1鉴权206、完整Content-Range及两字节长度通过；
匿名401、logout成功、catalog_sync_pending=false。
仅该来源Range抽查，不是完整远端MP4哈希/解码或所有副本已验证。

本轮36收藏初验某封面HTTP502，不写成全通过；错误类/fixture位置单列保留。
隔离验收仅对502/503/504一次有界重试、原完整校验不变，随后36张全部
鉴权200/private/JPEG/尺寸/完整SHA256通过、匿名401、logout成功，
此轮验收过程中也有一次502重试恢复。这不是Player生产重试策略变更。
新增当前有效TimeoutError来源原绑定/原画大小有效、77秒失败；
第一次索引480p远端大小核验false，随后只读stat确认文件存在、非目录、
实际与预期均1,937,245字节。说明读取/元数据结果存在波动，
不据第一次false宣称损坏，也不据大小正确宣称完整内容或两档恢复。

最后缺图原绑定有效、无绑定副本、副本pending/attempt0、封面attempt8，
原冷却/12MiB/768MiB/输入输出单线程保持，不重复四次HEVC取帧/NAL诊断。
实际FFmpeg编码及临时写入持续，封面/副本OOM及OOM-kill0；
四容器身份/运行版本仍eaa83cd/8097821及既有Bot/Player、零重启、后两者健康。
三库quick_check通过，磁盘21,129,236,480字节，未调大CPU/并发/预算。
冻结download_failed仍enabled/max3/count6/exhausted；九个旧inactive/404、
两个删除和已验证照片取消包保持不复活/重播/重发边界。

私有completion-metadata-verify/audit-20261002-0302保存本轮18项证据；
recovered-0254-range-remote选择固定完成时间，不按变化的最近complete重绑定，
已鉴权验收通过，不再盲目重跑或覆写该私有引用。
favorites原失败及favorites-retry成功分别保留；新helper均只读或鉴权验收，
没有生产代码热补丁或数据库写入。本轮仅文档推送/独立源码同步，不重建服务。
尚未全覆盖，tgvio自动跟进继续；下一轮先当前monitor、有效失败归属/重试、
最后缺图副本与上游读取波动，不把旧图/副本验收当实时全部可读。
