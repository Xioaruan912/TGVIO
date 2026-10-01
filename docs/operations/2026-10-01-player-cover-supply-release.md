# 2026-10-01 Player 真实封面供给发布

## 根因与修改

前端已使用 cover_url，但生产 media_covers 最初为 0。
只读抽样 20 个有效归档包，20 个没有 covers.json；仅是抽样，不代表全库覆盖率。
旧 Bot 内补齐入口的头部读取不是实际 Range，无绑定的旧 v1 索引也未被 Player 消费。
本次独立维护 worker 生成真实 JPEG，Player 验证绑定索引并复用已有鉴权接口。
实现、所有权和限制见 ../development/COVER_SUPPLY.md；
回归与交接见 ../handoffs/2026-10-01-player-cover-supply.md。
src/tgvio/AGENTS.md、src/tgvio_player/AGENTS.md 已加入长期供给与隔离合同。

## 运行版本与来源

| 项目 | 已核验事实 |
|---|---|
| Player 应用源码 | c740cfba61e780ef04107f7548788696a7baea8f |
| Player 实际镜像 | sha256:19edb34c649d21047359e7a1209ceb2d769881720dcd5901b02f659960fbc8d9 |
| Player 实际容器 | b6ed58dd173b9708ffd90610cc300297bd4afd7a7503eb56b2b96cf2e89cb753 |
| Player 切换 UTC | 2026-10-01T07:40:58.853540+00:00 |
| 封面 worker 源码 | 2659ff1571db5d2a5df164231f64d4dfb2a27d4b |
| 封面 worker 实际镜像 | sha256:b20730274883656c080bfdee52f4fa8b90f3f4471a9d75900ad8bdb01e6cb790 |
| 封面 worker 实际容器 | 76757ffc628119d88db42089e74acb5d570e0c4b2988d28810a623a5d6715ed6 |
| 两次候选源码文件 | 各 525 个，逐文件 SHA-256/mode 验证 |
| Player 源码 archive SHA-256 | 752acdca493b240567f066c151eadf2446d70d4ae88586f1aaa17ef9a8b1c317 |
| worker 源码 archive SHA-256 | 5c90e136a54d6503694d17529afc246563db3f37faa85047fb43522a1f94ac18 |

消费侧用 scripts/player_release.sh 从 clean、live origin/main 一致的 git archive 构建。
生成侧复用 Dockerfile.renditions 的 FFmpeg 维护环境，覆盖入口为 backfill_covers。
导入前验证传输 hash，导入后验证 RootFS、平台、Config、OCI revision，运行 UID 65532。
仅 Player image 切换一次；后续网络恢复修复只更新独立封面 worker。
本回执为纯文档提交，不会再次构建或重启应用。

## 生产抽样与恢复

初次有限抽样出现归档超时，未登记假图片。随后原 35 秒、约 372 MB、4K 视频
实际取帧成功；该码率也解释了原画播放的网络压力，不将封面修复宣称为播放性能验收。
上传后回读 JPEG，大小与 SHA-256 均匹配，原 manifest/_COMPLETE 校验未变化。
持续扫描也遇到临时错误，已补最多三次 I/O 重试与扫描失败恢复；
错误范围/损坏 JSON 不重试，不用放宽完整性校验解决网络问题。

登录后抽查 3 个生产封面：JPEG 200、完整长度、内容 hash 与索引一致，
有效尺寸不超过 640；未登录 cover 401、缓存为 private。用临时运维会话验证并登出，
未把口令、Cookie、Archive 路径或私人画面写入 Git/截图/自动测试。

## 补齐快照与尚未完成的工作

时间：2026-10-01T08:07:14Z。

- 原归档任务：1017 条，含不同包内重复位置；当前有效视频 910 个。
- 已完成/写入封面：23 / 23。
- 当前失败：2；永久阻塞：0。
- Player 已投影真实封面的有效视频：13。
- 收藏总数 36，其中已投影封面 13。
- 封面 worker running、restarts 0，无 OOM；进度持续增加。
- 清晰度副本投影 53 条，原 worker 保持运行；不代表全库副本已齐。

已生成数量与 Player 已同步数量是两种事实，目录同步完成后重新进入列表显示新封面。
本快照失败类别：{"TimeoutError": 1, "WebDavArchiveError": 1}，保留冷却后重试。
剩余历史视频由后台串行、限速补齐，优先收藏与续播；不是全库已完成。
之后继续发现新归档；失败最多五次、十分钟冷却，完成项一天后复核。
损坏、全黑/白、超过 4GiB、取样预算不够或不兼容的旧索引可能仍缺图，
不以无关图片代替。无需浏览器全库解码或原视频完整下载。

## 数据与发布后验

- 切换前后收藏 36、续播 5 不变；数据库及一致性备份 quick_check=ok。
- migration [1,2,3,4,5,6,7,8,10] 保持不变，无新迁移。
- Bot 容器 ID/image/restart 与前一轮完全一致。
- tgvio-renditions 容器 ID/image/restart 完全一致，继续补齐 480p/720p。
- 封面 worker 只有四个 Archive 环境字段，专用工作卷与检查点，无 Bot/Player DB 挂载。
- 有资源限制、只读 rootfs、cap-drop、无新增权限、日志轮转、SIGTERM 清理。
- 保存 Player 配置/SQLite backup 与维护检查点 backup；旧镜像和 release source 保留。
- 首页/healthz 200，未登录 feed 401，CSS/JS/index SHA-256 与候选一致。
- 无反向代理或公网监听修改，无生产视频删除，无 Bot 身份启动。

最终门禁：1034 Python、215 Node、严格 TypeScript/Vite 构建、架构/卫生/diff 检查通过。
消费侧初次实现还通过 999 个隔离浏览器检查；之后只改维护 I/O 和文档，
无前端资产变化。实际 FFmpeg 测试使用合成尾部 moov 视频；自动测试不连接生产。
没有重新验收 Android/iOS 真机播放，也不声称本地构建等同生产播放成功。
