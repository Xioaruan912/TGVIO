# 18+ 播放器功能调研：可以搬进 TGVIO Player 的东西（2026-10-04）

**目的**：把同类产品（自托管成人媒体库、成人播放器、VR 播放器、隐私优先工具）的功能面摸清，
筛出**能进 TGVIO 且不违反硬约束**的候选，给出优先级、工作量与风险。

**硬约束（决定取舍的第一原则）**：
- 私有（18+）内容的**一切逻辑在本机**；**不使用第三方/云服务**；**画面与行为数据不出主机**。
- 归档只读（写入由 Bot 侧拥有）；Player 是独立 HTTPS 应用 + 独立 SQLite + 只读 WebDAV。

---

## 1. 调研对象（同类产品与它们的招牌功能）

| 产品 | 类型 | 招牌能力 |
|---|---|---|
| **Stash**（自托管，Go） | 媒体库 | 场景可**评分**、可打**演员/标签/片商/系列**；**标记（markers）**收藏精彩片段并显示在进度条上；画廊/图片；**保存筛选器**；跨字段搜索（标题/路径/OSHASH/校验和/标记名）；**统计**（演员/标签/片商）；**感知哈希**（场景与图片）用于去重与匹配；插件体系；`stash-box` 是社区元数据库（按指纹匹配） |
| **Jellyfin / Emby 成人插件**（ThePornDB、PhoenixAdult、MetaTube） | 媒体库插件 | 联网刮削：标题/简介/片商/日期/类别/标签/演员/海报背景；**人脸检测裁主图**；自动翻译；定时刷新元数据 |
| **VidVana**（2026，Mac/Win 桌面） | 播放器 | **多视频同屏墙（2×2 到 5×5 同时播放）**；**会话自动化**（免手轮换、无需键盘）；**加权轮换**（好内容更常出现）；直接读 Stash 库（只读副本）；本地加密；书签/倍速/截图 |
| **SauceBox** | 下载器+库 | 4 位 PIN 锁屏；**panic 热键（最小化+静音+暂停下载）**；**无损精彩片段裁剪**（毫秒级提取场景亮点）；本地加密状态文件 |
| **VANTA** | 播放器 | PIN 锁（PBKDF2）；**胁迫 PIN（进入空库）**；**panic 全局热键**；**失焦/超时自动锁**；**隐身模式（任务栏标题伪装成 "Files"）**；**封面模糊（悬停才显形）** |
| **DeoVR / HereSphere** | VR 播放器 | 立体模式（**SBS / 上下 / off**）；**投影类型**（平面 / 180 穹顶 / 360 球 / 鱼眼 / mkx200）；缩放与视距；自动对焦景深；软件 IPD；空间音频；头部追踪 |
| **隐私 UX 研究** | 研究 | 85% 受访者表示"如果观看记录会被别人看到就会立刻弃用"；数据最小化（少标识、短保留、不跨会话关联）；panic button / shadow mode 被视为"生存工具"而非锦上添花 |

## 2. TGVIO 现状（避免重复提案）

**已有**：封面瀑布墙与密度切换、短片/长片两种播放器、片库（文件夹/日期/视频）、收藏、合集、
分组、**按封面指纹找相似**（phash，思路与 stash-box 指纹匹配同源但**全本地**）、随机/洗牌、
断点续播、清晰度切换、缩略图拖拽预览、倍速、亮度/音量手势、隐私锁（空闲触发）、全屏/画中画、
媒体会话（锁屏控件）、缓存与预载、封面本地镜像、播放诊断事件。

**结论：TGVIO 已经覆盖了同类产品"播放与浏览"的主体**，差距集中在四处：
**① 多视频同屏；② 会话自动化与偏好权重；③ 元数据/标签/标记体系；④ 隐蔽与安全功能包。**

## 3. 三条决定可行性的硬事实（含来源）

1. **浏览器多流解码有明确上限**：桌面 H.264 硬解约 **16 路** 1080p、软解约 **10 路**；H.265 只有
   **6 路（硬）/ 5 路（软）**。→ **2×2（4 路）稳**；3×3（9 路）在软解边缘、硬解可行；
   4×4 以上需要硬解 + 足够带宽，且要优先 H.264。
2. **TGVIO 自己的服务端流预算只有 4**（`MAX_STREAMS=4`，封面并发是它的一半）。
   → 同屏墙**不能直接吃现有预算**，必须给它独立预算、并把"当前有声的主瓦片"排在最高优先级，
   否则墙会把单视频播放饿死。
3. **"不上云"的自动打标完全可行**：已有多个**纯本地 ONNX**方案（如 `nuditag`：ONNX Runtime +
   22 MB ViT，CPU 即可，输出 CSV/JSON；`nsfwpy-onnx` 五分类；`monbooru` 用 WD14/JoyTag 做本地
   自动打标并明确"应用自身永不联网"）。→ 本地分类/打标**不违反硬约束**。

## 4. 候选功能清单

### P0 —— 便宜、零新架构、价值最高（建议先做一包）

| 功能 | 依据 | 做法 | 工作量/风险 |
|---|---|---|---|
| **panic 快捷键** | VANTA / SauceBox / Opera GX 都把"一键藏起来"当第一功能 | 一个全局快捷键：立刻遮罩+暂停+静音（复用现有隐私锁机制），可再按恢复 | 小 / 低 |
| **封面模糊（悬停才显形）** | VANTA | 封面墙加"模糊模式"，默认模糊、聚焦/悬停才清晰 | 小 / 低 |
| **标题伪装** | VANTA 隐身模式 | 标签页标题/应用名可设为中性词（如"文件"） | 小 / 低 |
| **失焦即锁** | VANTA 自动锁 | 现有空闲锁加一条"切走窗口就锁" | 小 / 低 |
| **星级评分（1–5）** | Stash 场景可评分 | 加一列 + 卡片/长片页控件；**同时作为下面轮换的权重来源** | 小 / 低 |
| **会话自动续播与轮换** | VidVana 会话自动化 + 加权轮换 | 播放器加"连播/轮换"模式：一段结束自动下一段、可选按**收藏×评分**加权、可设时长 | 中 / 低 |

### P1 —— 差异化最大，但要动服务端

| 功能 | 依据 | 做法 | 工作量/风险 |
|---|---|---|---|
| **2×2 同屏墙（主瓦片有声）** | VidVana 的招牌；同类文章把"多流不丢帧 + 隐私"列为两大要求 | 新页面：4 个瓦片同时播放（默认静音），点哪个哪个有声并放大；**独立流预算 + 主瓦片优先 + 强预缓冲**；先只做 2×2，3×3 作为可选 | 大 / **中高**（带宽与解码是真实风险，需先做 2×2 实测） |
| **场景标记 + 片段循环** | Stash markers（显示在进度条上） | 长片页可加标记点、进度条显示、一键"循环这一段" | 中 / 低 |
| **本地自动打标 + 标签筛选** | Stash 标签体系；`nuditag`/`nsfwpy-onnx`/`monbooru` 证明可纯本地 | 离线 ONNX 分类器在**归档侧**跑（与指纹回填同一条管线），产出标签写进目录元数据；Player 侧加标签筛选与搜索 | 中 / 中（模型体积、CPU 占用、误判需可覆盖） |
| **本地观看统计** | Stash 统计 | 只看本机的播放次数/时长/最近观看，驱动"最近/常看" | 小 / 低（注意：属行为数据，但**不出主机**） |

### P2 —— 按需再定

| 功能 | 说明 | 判断 |
|---|---|---|
| **VR / 立体播放** | DeoVR/HereSphere 的 SBS/上下 + 投影（平面/180/360/鱼眼）+ 缩放视距 | WebXR 可行，但**只有你有 VR 素材/头显才值得做**；否则纯投入 |
| **胁迫密码（进空库）** | VANTA | 服务端鉴权侧实现，价值高但属"另一个密码体系"，需谨慎设计 |
| **本地状态加密** | SauceBox / VidVana | Player 已有 `player_crypto`（收藏备份加密）可延展；归档整体加密是另一个量级的工程 |
| **投屏 / DLNA** | Stash 有 DLNA | 局域网内合规，但对单机使用价值有限 |

### 明确排除（与硬约束冲突）

- **联网刮削元数据**（stash-box / ThePornDB / PhoenixAdult / MetaTube 的核心能力）——
  会让"你收藏了什么"离开本机，与产品承诺直接冲突。**本地可替代**：文件名/路径解析 + 人工标签 +
  本地 ONNX 打标。
- **云端 AI / 自动翻译 / 人脸聚类上云** —— 同上。
- **任何观看行为外发**（含"匿名统计"）—— 不做。

## 5. 推荐路线

1. **先做 P0 一包**（安全包 + 评分 + 会话轮换）：不碰服务端、不碰预算、每条都能独立验证，
   且其中"评分"是后面轮换与墙的权重基础。
2. **再做 2×2 同屏墙**：先在一台真实设备上量 4 路的带宽与解码（H.264 优先），
   再决定要不要 3×3。这一条风险最高，值得单独一次设计与实测。
3. **标签/标记按需**：标签是"越早打越值钱"的东西，但需要归档侧跑模型；标记纯手工、随时可加。

## 6. 来源

- Stash 官方与文档：<https://stashapp.cc/> · <https://docs.stashapp.cc/in-app-manual/browsing/> ·
  <https://github.com/stashapp/stash> · stash-box：<https://github.com/stashapp/stash-box>
- Jellyfin/Emby 成人插件：<https://github.com/ThePornDatabase/Jellyfin.Plugin.ThePornDB> ·
  <https://github.com/DirtyRacer1337/Jellyfin.Plugin.PhoenixAdult> ·
  <https://github.com/metatube-community/jellyfin-plugin-metatube>
- VidVana：<https://www.vidvana.app/> · <https://www.vidvana.app/help> ·
  Stash 社区讨论（读库/网格细节）：<https://github.com/stashapp/stash/discussions/6834>
- 多视频与选型对比：<https://privatemediapro.com/best-multi-video-player> ·
  <https://privatemediapro.com/best-players>
- 隐私/安全功能：<https://github.com/2Booty/VANTA/blob/main/README.md> · <http://saucebox.app/> ·
  <https://specialplace.hashnode.dev/shadow-mode-and-panic-buttons-safety-first-features-for-high-risk-users-in-adult-tech> ·
  <https://mediaprowebdesign.ie/2026/10/03/viewer-privacy-expectations-drive-adult-media-platform-redesigns/>
- VR 播放器：<https://deovr.com/app/doc> · <https://heresphere.com/>
- 多流解码上限：<https://blog.mickeyzzc.tech/en/posts/network/web-video-codec-survey/>
- 本地（不上云）打标：<https://github.com/ICIJ/nuditag> · <https://pypi.org/project/nsfwpy-onnx/> ·
  <https://github.com/monbooru/monbooru>
