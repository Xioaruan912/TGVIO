# Player 一分钟闲置隐私锁定发布

日期：2026-09-30 UTC。计划：`docs/superpowers/plans/2026-09-30-player-idle-privacy-lock.md`。
范围仅 Player 前端、回归与文档；未修改 Player 后端、数据库迁移或 Bot。

## 行为合同

- 短 Feed 解锁后，连续 60 秒无真实用户操作，即遮挡、暂停、强制静音；播放也计时。
- 长视频实际 playing 时豁免；暂停、结束、加载、实际缓冲等未播放状态下，连续 60 秒无操作执行相同锁定。
- 已缓存可继续播放时的网络 `stalled` 不直接视为播放停止；重复等待/暂停事件不能延长宽限。
- 点击、触摸拖动、滚轮、键盘和输入刷新期限；hover、媒体进度、自动 scroll/snap 不刷新。
- 普通操作不自动解锁；明确播放解锁后仍静音，需要主动开声音。
- 进入长视频列表先锁定底下短视频。待恢复播放和旧声音确认不能越过隐私锁；锁定退出 fullscreen/native fullscreen/PiP，晚到 PiP 进入立即再次退出。
- 网页隐私锁不是手机系统锁屏；后台保持既有隐私策略，网页计时器受浏览器调度限制，可见性恢复补检期限。

## 门禁与发布身份

| 项目 | 结果 |
| --- | --- |
| 前端单测 | 118/118；先对缺失实现运行行为测试，5 项失败，再实现至通过 |
| Python 全仓回归 | 919/919 |
| 隔离 Chrome | 390/430/768/1440 视口分别 101 项检查通过 |
| TypeScript / Vite build | 通过 |
| Docker offline foundation / runtime inspection | 通过，Bot test-build 未部署 |
| source-tree / architecture / diff check | 通过 |
| Release | `idle-lock-20260930-ed89177` |
| Runtime commit | `ed89177da70e95ec8e3e61c21078d3d9a2a86b26` |
| Local final candidate | `sha256:a26f4cd53a12b4fb5ab21fade6d4a477129c0e31134a1009ce0a470e158bb167` |
| VPS image | `sha256:2329ad19e8e7ed4f2246e84d7ea87d7cc40a811d5a1663e2d5b818a3ab9abd35` |
| Running Player container | `ed8bd6013ee57afba36c6fe805545e0014737727c318bbf7fdaa2f17231a769b` |
| Cutover | `2026-09-30T00:47:23.479453+00:00` |
| Verified | `2026-09-30T00:48:43.201200+00:00` |

一次早期 shell 试构建因宿主变量展开产生无效 revision 标签，未作为发布候选传输/部署；最终使用 Python subprocess 的确定性参数，从 clean 已推送 commit 的独立 git archive 重建有效候选并逐项验证。

两端 Docker image config digest 不同，已验证全部 RootFS layers、revision/version labels 及镜像内容 manifests，而不是只核对 tag。

- Backend manifest：`ab981063532d70156f3b11e2eb75e79506bd9e9e05b12a672bc4c5a1f0bb790b`，与切换前运行镜像相同。
- Frontend manifest：`7ded2aad834998596cf9fbac353626cfc4bee22e8d6b1647031b4815c2ec29f0`。
- Source archive SHA-256：`7e2a8a1e57f0884a829ba5c1a5b3a6960e4151a3c2da03c12a7fc9dab13bb224`。
- Image archive SHA-256：`272a0f9a28acda968a16794fc68d1a65f0d5ba7e1d3bce10326638d0a9b946d8`。
- Public JS/CSS：`index-DHPJSnas.js` / `index-BVwy2y6C.css`，HTTPS 取得内容与运行镜像逐字节哈希一致。

## 备份、演练、回滚

- Online SQLite backup：`/root/tgvio-player/backups/player-pre-idle-lock-20260930-ed89177.sqlite3`，0600；SHA-256 `d9625ceb3aeedc551b95be0397c0630c31c9f9afb775bfc326216229c301a652`。
- network none 候选迁移演练只打开 backup 副本，schema、迁移账本和各表计数未变，quick_check=ok；演练副本已清理。
- 生产 env 与运行容器内部比较一致，仅更新 image 行；env 内容不输出、不下载、不提交。
- 回滚 image：`tgvio-player:rollback-idle-lock-20260930-ed89177`；原镜像 `sha256:534541462b7cc67250613c080f5f848218b29e32a5e59a190704e560efa04da9`。
- 回滚 env 备份：`/root/tgvio-player/player.env.bak-idle-lock-20260930-ed89177`，0600；留在 VPS。
- 原 release：`/root/tgvio-player/releases/playback-fix-20260930-f2fe464`。
- `current` 与 `.release-commit` 在后验通过后对齐 runtime commit。

## 生产后验

- Player running/healthy，restarts=0。
- Bot ID `efa5a50c9661440ede35a9d9f8e61a3e579215487642efb0e699639c88597154` 和 started-at 未变，未重启。
- HTTPS 首页/healthz：200/200；未登录 Feed：401。
- 登录/认证 Feed：200/200；Secure cookie 生效；真实媒体 Range：206、0–1023、1024 bytes。
- Player SQLite quick_check=ok，schema/ledger 哈希与演练相同。
- 切换后日志窗口 ERROR/CRITICAL/Traceback=0、结构化 HTTP 5xx=0。
- HTTP smoke 仅在 Player 容器内部消费访问口令，不输出或传出秘密/Cookie；Feed 会消费该测试 session 的 shuffle 状态，未写收藏/删除/存储设置。

## 验收边界

Chrome fixture 使用可注入 fake clock 和模拟媒体事件，覆盖长视频事件接线、真实布局/命中、精确期限、恢复/销毁、锁后音量与 PiP/fullscreen 退出调用；不代表 Android/iOS 真机媒体时序或原生 fullscreen 已验收。
短 Feed 期限由共用计时器单测覆盖，生产 HTTP 验证没有执行手机的实际 60 秒等待、播放或触摸交互。下一步仍需真机验收及原生 API 拒绝/失败场景检查。
