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
后续需要记录精确VPS导入镜像、Python3.11隔离门禁、检查点备份、实际部署和后验。
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
