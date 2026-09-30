# Player 加载与交互修复：Player-only 发布证据

日期：2026-09-30（UTC）。范围仅为 Player；Bot 未构建为 release、未切换、未重启。

## 1. 发布身份

| 项目 | 已验证值 |
| --- | --- |
| Release | `playback-fix-20260930-f2fe464` |
| Runtime full commit | `f2fe464ae59d3874344d5d5420c7b2f1bcb7cc67` |
| 本地候选镜像 | `sha256:f0908a071c77a4db26a2a487875c3604adf62893d77f7d944b6bead6775701c1` |
| VPS 镜像 | `sha256:534541462b7cc67250613c080f5f848218b29e32a5e59a190704e560efa04da9` |
| Player 容器 | `365f05ffdd0852029183434b56b6ba2c5da450e4d84a7c82a2e820e57e9683bf` |
| 切换开始 | `2026-09-30T00:22:39.962824+00:00` |
| 后验完成 | `2026-09-30T00:24:00.091920+00:00` |
| 发布目录 | `/root/tgvio-player/releases/playback-fix-20260930-f2fe464` |
| 前端 JS | `index-BiSBaL3Z.js` |
| 前端 CSS | `index-BVwy2y6C.css` |

本地与 VPS image config digest 不同（Docker store 序列化差异），因此同时验证全部 RootFS layer digests、revision label、镜像内前后端内容 manifest，而不以可变 tag 或两端 image id 相等代替校验。

- Backend manifest：`ab981063532d70156f3b11e2eb75e79506bd9e9e05b12a672bc4c5a1f0bb790b`；与切换前运行后端一致。
- Frontend manifest：`d37ece9ced332a7f961551916197ea01c324514628e0b86d3b772da8f7fa3bd8`；本地构建、VPS 镜像和运行容器一致。
- Source archive SHA-256：`5525e7b44595118db4eb4e7ba35d9f8ce25bad088a567d73940a490096b1817d`。
- Image archive SHA-256：`fd85e25330be2a2198a7639a99cadf626f3c72edaa306beb3431d0cf599ad1ad`。

## 2. 本次修改

- 合并双加载圈/文案，poster 改为装饰背景；首帧后的缓冲不再盖黑。
- 多次 waiting/playing 恢复持续监听；迟到的 play Promise 和首帧回调校验 load token。
- 连续清晰度切换保留待恢复位置，等待 metadata 后恢复；用户暂停、隐私锁与释放可阻止迟到恢复播放；临时倍速不跨池复用。
- Feed 补页有请求预算、无新增退出和取消/超时清理，下一条已存在时不等待网络补页；上下文 cursor 不推进时提供失败/继续入口。
- 收藏使用跨视图共享的 per-media 串行队列，失败只回滚最新意图，销毁视图解除订阅。
- 手势方向锁定、指针归属、pointercancel/lostpointercapture/后台收尾；画面拖动先预览，松手再提交 seek；短视频双击快进接入设置。
- 隐形控件退出指针命中与键盘焦点；长视频进度触摸区至少 44px，与按钮独立分行；隐私锁定隐藏控件保持 inert。
- 新增 `player/AGENTS.md` 和回归测试；未引入新框架或依赖。

## 3. 门禁

- 前端 `npm test`：108/108。
- 本地全仓 Python：919 tests OK；其中 Player 相关 170 tests OK。
- `npm run build`、`release_guard verify-tree`、`architecture`、`git diff --check`：通过。
- `npm run test:browser`：隔离临时 Chrome profile，在 390/430/768/1440 视口分别通过 49 项布局/命中/模拟媒体事件检查。
- `scripts/build_check.sh`：通过；测试容器 network none，foundation 与 runtime image scan 通过。它产生的 Bot 镜像只是诊断 test-build，未传 VPS、未部署。
- 本地基础检查最初因虚拟环境缺 Telethon 无法运行；使用已锁定依赖的 Docker 离线 foundation gate 完成验证，未安装/修改本机依赖。
- 真正 Player release 镜像来自 clean、已推送的 runtime full commit，仅构建一次；传输后校验 archive hashes、layers、labels、内容 manifest。

## 4. 备份与演练

- 在线 SQLite backup：`/root/tgvio-player/backups/player-pre-playback-fix-20260930-f2fe464.sqlite3`，mode 0600，SHA-256 `343a064064955bb3624e2ebf3f719def141ab6dcc262a2a38d3ab95ca6514891`。
- 候选镜像在 network none 下只打开 backup 的演练副本，不启动服务；核对历史迁移账本 1–8、所有表计数和 schema，执行迁移 runner 后完全不变。
- Compose 与当前容器环境键值在切换前内部比较一致；只原子更新 Player env 的 image 行，环境内容不输出、不传出 VPS。
- 回滚镜像：`tgvio-player:rollback-playback-fix-20260930-f2fe464`，原镜像 `sha256:a536ecd06d90ff872b6643a3b91a680694c004f3721d917c420ba7ba30694b9f`。
- 原发布目录：`/root/tgvio-player/releases/probe-range-tail-20260928-03a91b4`。
- 远端 root-only 环境备份：`/root/tgvio-player/player.env.bak-playback-fix-20260930-f2fe464`，mode 0600；未下载或展示。
- 当前 Player `current` 与 `.release-commit` 在后验通过后对齐；Bot 元数据未修改。

## 5. 生产后验

| 检查 | 结果 |
| --- | --- |
| Player | running / healthy / restarts=0 |
| Bot | 容器 ID `efa5a50c9661440ede35a9d9f8e61a3e579215487642efb0e699639c88597154` 与 started-at 未变，未重启 |
| HTTPS `/`、`/healthz` | 200 / 200 |
| HTTPS 未认证 Feed | 401 |
| 登录与认证 Feed | 200 / 200；Secure cookie 生效 |
| HTTPS 静态资源 | 两个入口资源 200，内容哈希与运行镜像一致 |
| 真实媒体 Range | 206，bytes 0–1023，实际 1024 bytes |
| SQLite | quick_check=ok，schema 和迁移账本哈希不变 |
| 切换后日志窗口 | Traceback/ERROR/CRITICAL=0，结构化 HTTP 5xx=0 |

认证 smoke 只在既有 Player 容器内部消费访问口令，不打印、保存或传出凭据/Cookie；未执行收藏、删除或存储设置变更。Feed 读取会创建/消费该测试 session 的 shuffle 状态，属于 HTTP 验收的预期副作用。

## 6. 未验收范围

- 未执行 Android/iOS 实机触摸、PWA 或 Safari 媒体时序回归。
- 隔离 Chrome fixture 的媒体事件是模拟事件，不能替代生产真实视频首帧、长时间缓冲恢复和手机手势验收。
- 本次为首轮渐进修复，未完成整个前端的大规模模块重构；后续仍应按 `player/AGENTS.md` 分阶段推进。
