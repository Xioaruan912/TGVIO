# 2026-10-02：副本失败诊断发布回执

## 原因与范围

补齐仍未完成；此发布修复诊断信息不足，不声称已解决所有 WebDAV 失败。
切换前副本 WebDavArchiveError 已有 21 项，有效失败任务共 22、唯一视频 17。
六个新增失败来源的只读 PROPFIND 可见且大小匹配；两项清单绑定索引有效，
renditions 目录存在，但对应源没有副本条目。有效 WebDAV 失败末次耗时约 116–586 秒。
不能仅根据这些结果断言是源丢失、解码、权限或上传故障。

副本失败/扫描报告新增精确白名单 failure_code，以及合法整数 HTTP 状态。
原异常文本、凭据、原媒体标识和 Archive 路径不进入新增字段；
未知错误使用 unclassified，不尝试输出原异常。
checkpoint 仍按原异常类型记失败，累计尝试和退避保持，未扩大媒体、网络或 FFmpeg 预算。

## 版本与后验

应用提交 7e3c35baecaa07390c3dfc44f38bd8073d4bbfee 经 clean、已推送 Git archive 构建。
候选 532 个文件逐项核验 SHA256/模式；VPS 镜像 Config、RootFS 与候选一致。
只切换副本工作器到 7e3c35b，仍为 2 GiB / 1.5 CPU、96 PID，Archive-only env 和自身工作卷。
封面保持 a5fd3e0、768 MiB / 0.75 CPU；Bot/Player 容器身份及重启数未改变。

停旧副本后 SQLite backup、quick_check 通过；预演前后的全部原检查点行逐项一致。
保留旧 763a1f6 停止容器、镜像和一致性备份。新工作器 running、restart 0、OOM false。
本机 expected-workers.json 仅更新副本身份；不能按最新 Git HEAD 重建封面或 Bot/Player。
16:08 UTC 新工作器已完成扫描并开始任务，尚未产生新的失败分类事件；
继续用实际新日志定位，不能将进程启动视作补齐成功。

## 覆盖：2026-10-01T16:08:05Z / 2026-10-02 00:08 北京时间

| 项目 | 可用投影 | 应有数量 | 待补 |
|---|---:|---:|---:|
| 真实封面 | 736 | 910 | 174 |
| 480p | 85 | 871 | 786 |
| 720p | 78 | 729 | 651 |
| 收藏封面 | 36 | 36 | 0 |

实时 36 条有效收藏本轮再次逐张鉴权读取：JPEG 200、字节数、尺寸、SHA256 均通过；
未登录抽查仍为 401，返回 private 缓存语义。没有输出图像或 Cookie。
这不代替剩余全库封面与副本的逐项最终验收，也没有本轮真机播放验收。

catalog 和两个维护 checkpoint quick_check 通过，VPS 磁盘可用约 22.51 GB。
Archive 根有界 PROPFIND 207，但不提供 quota 字段，不能据此声称远端容量充足。
历史九个 inactive/404 来源、已在另一归档的取消照片和 download_failed Job 保持单列；
不复活删除内容，不重发 Telegram。

## 验证与证据

三个新增隔离回归通过：未知错误隐私、HTTP 状态过滤、CLI 失败仍累计到 6 次且保留冷却。
完整 scripts/check.sh 通过：Python 1043、前端单测 215、TS/Vite、架构及仓库规范；
宿主 Python 为 3.13.5，未将其宣称为 3.11 全套验证。候选容器导入、FFmpeg 和 UID=65532 检查通过。
未运行浏览器布局验收；本轮未修改前端。

私有证据在 completion-rendition-diagnostics：源码/镜像清单、部署回执、
检查点备份、telemetry-latest.json 和 archive-quota.json；
completion-release 保留当前 monitor/expected-workers、失败阶段抽查、逐张收藏及时间戳进度。
后续先检查 HTTP 分类、真实对象和资源，再选择受控修复，不清零失败或伪造覆盖。
每小时跟进及两个后台工作器持续执行，真实全覆盖完成前保持跟进。
