# 2026-10-03 Player 黑金 Pro 与华丽动效交接

## 本轮范围

用户要求「完全重构前端 UI，完全脱离原本的颜色与控件，增加很多现代化动画」，
随后追加「颜色改成黑金 Pro 的华丽感」与「更关注动效，允许华丽」。
载体是 `player/web`，只改前端，**尚未发布**。

## 已确认的决策

| 决策 | 选择 |
| --- | --- |
| 视觉方向 | 深夜影院 → 升级为 **黑金 Pro**（金属金箔 + 暖黑 + 金色发丝边） |
| 改造深度 | 结构也重排 |
| 动效栈 | CSS + WAAPI 为主；**`motion@^14` 仅用于物理驱动**，懒加载 |
| 标题字体 | 自托管显示字体（尚未落地，见"未完成"） |

依赖实测（本地 esbuild + gzip，非第三方宣称）：`motion` hybrid 20.6KB gzip。
因为是**动态 import**，它单独成块，主包不受影响。

## 已完成（6 个提交，均在本地 main，未推送）

| 提交 | 内容 |
| --- | --- |
| `b913e2b` | 设计文档 `docs/development/PLAYER_FRONTEND_DARK_CINEMA.md` |
| `b5be3b4` | 批次 1：深色令牌层 + `motion.css`（三条弹簧曲线由阻尼方程生成，超调 8.4/0.6/0.2%）+ 顺手清掉 39 处硬编码浅色 |
| `81991b2` | 批次 2：控件重做 —— 自定义进度条（拖动 6→10px、拇指 1.5×+金色光环）、圆形主操作、`indicator.ts` 滑动指示器（底栏与分段控件共用一套）、开关、toast |
| `b31d64e` | **黑金改色 + 金箔层 `foil.css`** + 批次 3 结构层（画面全出血、顶栏 52px 透明、桌面侧栏 92→72px 带 hover 浮层标签、桌面控制面板改横向单行） |
| `732ab0b` | **批次 5 动效层**：View Transitions 切页、隐私锁虹膜、金色流光、封面指针追光、滚动驱动顶栏/卡片入场、金色聚焦光环、抽屉 fling 关闭（真实速度物理）+ `motion` 懒加载 |
| `ecb26bd` | 批次 4 部分：页面大标题、金色内嵌边、目录卡金边、选中环改 gold-bright、抽屉金色手柄 |

### 关键设计点（不要推翻）

- **金箔只给金属件**：品牌标、圆形播放键、主操作、「解锁并播放」、登录提交。
  标题用金色裁字，但**默认色先声明为香槟金**，不支持 `background-clip: text` 时不会隐形。
- **白字禁止放在金色上**（只有 2.1:1）。所有金色实心控件用 `--accent-ink`（8.8:1）。
- **`motion` 不静态 import**：`src/motion.ts` 内部 `import("motion")`，所以单测不受影响、
  且物理引擎只在首次拖抽屉时下载。
- **View Transition 的包装放在 `navigation.ts` 的点击处理器里**，不是 main.ts
  —— main.ts 有 1710 行债务上限，只能缩小不能增长（当前 1709）。
- **底栏/分段的滑动指示器共用 `components/indicator.ts`**，不要写第二套。
- `.large-player:not(.controls-visible) .large-topbar` 这类选择器是**被测试禁止的**
  （长播放器顶栏与控制区不得因闲置被隐藏）。

## 验证证据（当前状态）

```
npm run typecheck                       通过
npm run test                            237 passed / 0 failed
npm run test:browser                    6 视口 × 125 + 274 checks 全通过
bash scripts/check.sh --browser         project_checks=passed
git diff --check                        干净
构建                                    入口 50.61KB gzip / CSS 12.36KB gzip
                                        物理引擎单独成块（懒加载）
axe（上一轮深色态实测）                  5 个页面 0 violation
```

## 未完成（下一会话从这里接）

1. **批次 4 剩余**：长播放器顶栏改**透明浮层**（当前仍是普通网格行，未覆盖到画面上）。
2. **批次 6 收口**：
   - axe 在**金箔态**重跑全部页面（金边可能影响非文本对比度）
   - 150% 文本、`prefers-reduced-motion` 回归
   - 六视口截图归档
3. **自托管标题字体**（用户已同意）：拉丁+数字，**必须随包发布，不得引外部 CDN**。
4. **推 GitHub**（用户已授权推 main）。
5. **VPS 发布**（用户要求发布，但选择"先做完 3-6 再发"）：
   - 规范要求 clean + **已推送**提交
   - 流程：`scripts/player_release.sh` 构建候选 → 传输校验 SHA-256 → VPS 导入核对 image ID/OCI revision
     → 取发布锁 + 0600 Player 配置与 SQLite backup API 备份 → `scripts/player_deploy.sh --env-file <player.env> --execute`
     → 后验 Player 容器 ID/health/restarts、**Bot 容器 ID 与 restart 计数不变**、DB `quick_check`、HTTPS 首页与资源 SHA-256
   - 保留旧镜像与旧 release source 作回滚点，写 `docs/operations/2026-10-03-*.md`
   - 专用密钥：`/root/.ssh/tgvio_hostdzire_ed25519`，固定 host key：`deploy/hostdzire_known_hosts`
   - `player_deploy.sh` 必须在 **VPS 上**执行（它 `docker inspect tgvio` 并本地 compose up）；本机不是 VPS
6. **设计文档需回填**：`PLAYER_FRONTEND_DARK_CINEMA.md` 里写的还是"深夜影院"色板，
   实际已升级为黑金 Pro，需要同步。

## 环境坑位（本轮踩过，务必照做）

| 坑 | 处理 |
| --- | --- |
| `wsl.exe -d Debian -- bash -c '…$var…'` 会**吃掉 `$`**，循环/命令替换静默失败 | 把脚本写成文件再用 `wsl.exe -- bash /root/x.sh` 执行 |
| Git Bash 把 `/root/...` 转成 `C:/Program Files/Git/root/...` | `MSYS_NO_PATHCONV=1` |
| WSL 里起的 fixture 服务器会随 wsl.exe 退出被杀 | `powershell Start-Process` 调 `wsl.exe -- bash /root/run-fixture.sh`（脚本内 `exec node tests/ui-acceptance.server.mjs`） |
| 聊天历史图片上限 30 张，超了报 `Too many images in request` | **不要连续读截图**；用 `agent-browser eval` 做 DOM 实测；需要给用户看时用 ffmpeg `xstack` 拼**一张**总览图 |
| 没有 ImageMagick/PIL | 用 `ffmpeg` 的 `scale+pad+xstack` 拼图（脚本见 `/root/make-sheet2.sh`） |
| View Transition 运行期间 `elementFromPoint` 返回 `html` | 真机点击不受影响（已用 CDP 实测）；测试探针需先 `await` 视图过渡动画结束 |

fixture 服务器：`cd /root/TGVIO/player/web && node tests/ui-acceptance.server.mjs`，
监听 `127.0.0.1:5179`，隔离测试媒体（FFmpeg 合成），**不接生产**。
