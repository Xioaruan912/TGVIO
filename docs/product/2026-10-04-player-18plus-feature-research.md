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

## 6. 第二批（换角度补充：会话玩法 / 库管理 / 本地 AI / 互动设备）

### 6.1 新发现的同类产品

| 产品 | 定位 | 关键事实 |
|---|---|---|
| **GoonHub** | 自托管 NSFW 库（Go + Nuxt） | **定位与 TGVIO 最接近**：自动处理、**全文搜索**、**标记（markers）**、播放列表、多用户、**全程在自己的硬件上** |
| **VidVana**（补全） | 桌面会话播放器 | 除网格外还有：**Quick Play 预设**（Most Watched / Favorites Mix 一键开局）、**Swap Timer**（免手轮换计时）、**加权收藏轮换**、**书签→合辑导出（Compilation Maker）**、**按演员的播放列表**、**全文本文件名搜索**（不联网）、AES-256、胁迫 PIN、Panic Hide |
| **Stash Sense** | Stash 的本地 ML 侧车 | **人脸识别认演员**（用精灵图）、重复场景检测、推荐；本地推理，但**演员库是上游同步的**（这一点不合本产品约束） |
| **FrameQuery** | 本地视频索引/搜索 | 一次索引（转录/场景/人脸/物体），**100% 离线搜索**；场景切分带**镜头类型与角度**；物体+人脸识别本地 |
| **ScriptPlayer+ / FunGen / Intiface** | 互动设备播放 | **funscript**（带时间戳位置数据的 JSON）本地播放、时间轴**热力图**、经 Intiface（600+ 设备）蓝牙/USB 驱动；延迟补偿 |
| **PHAR**（云 API） | 成人视频智能 | 把长视频变成**可导航场景** + 时间戳**动作/体位标签与章节**；本身是云服务（不合约束，但**概念可本地复刻**） |

### 6.2 第二批候选（按性价比排序）

| 功能 | 依据 | 做法 | 工作量/风险 |
|---|---|---|---|
| **全文本文件名/标题搜索** | VidVana「秒找演员」、GoonHub 全文搜索 | 本地索引现有文件名与标题；不联网、不刮削 | **小 / 低**（性价比最高的一档） |
| **Quick Play 预设** | VidVana | 「最常看」「收藏混合」一键开局，直接复用现有随机/收藏/续播 | 小 / 低 |
| **待看队列（Watch later）** | 主流播放器 | 收藏之外的第二种轻量意图；驱动「下一部看什么」 | 小 / 低 |
| **观看历史与一键清空** | 隐私研究里第一位的担忧（85% 说记录被看到就弃用） | 本机历史列表 + 单条删除 + 一键清空（含断点） | 小 / 低 |
| **重复检测视图** | Stash Sense / FrameQuery / 各库管理器 | **phash 已经有了**，只差一个「疑似重复」列表与合并入口 | 中 / 低 |
| **封面悬停/长按预览** | 主流播放器（旋转缩略图/短视频预览） | 复用现有缩略图预览管线，墙上悬停即放几秒 | 中 / 低 |
| **自动章节（场景切分）** | PHAR「可导航场景」、FrameQuery 场景切分 | **归档侧**用 ffmpeg 场景检测预计算（与指纹回填同一条管线），Player 侧显示章节与「跳过」 | 中 / 中 |
| **时间轴热力图** | ScriptPlayer+ | 有了章节/强度数据后，在进度条上画强度热力 | 小 / 低（依赖上一条） |
| **人脸聚类（本地认人）** | Stash Sense（本地推理那半） | **不引入外部演员库**：本地人脸检测+向量聚类，把同一个人聚成一组，由你命名 | 大 / 中（模型体积、误聚需可合并） |
| **本地语义搜索** | arkiv（本地 LLM + 向量库）、FrameQuery | 对画面/字幕做本地向量索引，按语义找 | 大 / 高（算力与存储都在归档侧） |
| **funscript 热力图（不含设备）** | ScriptPlayer+ / The Edgy | 读 funscript JSON 在时间轴画强度，纯本地 | 小 / 低 |
| **互动设备同步** | Intiface / FunGen | 技术上浏览器从 HTTPS 页面连本机 `ws://` 会被混合内容策略拦（需 `wss://` 或本机中继） | 中 / **高**（先不做，除非你确有设备） |

### 6.3 第二批里的「诚实排除」

- **阻止截屏/录屏**：浏览器层面**做不到**。真正有效的是 EME/Widevine L1（硬件级）与原生播放器，且它在无痕模式/WebView/Electron 里都不可靠；本产品能做的只有 panic/模糊/锁/无痕会话。不要承诺做不到的事。
- **云端演员库与云端动作识别**（Stash Sense 的上游库、PHAR API）：与「不外发」冲突，改为本地聚类与本地场景检测。
- **多用户**（GoonHub）：本产品是单用户设计，不引入账号体系。
- **NFO/刮削/自动下载元数据**：同第一批结论。

## 7. 第三批（可访问性与音视频细节 / 多设备与投屏 / AI 生成内容标注）

### 7.1 新发现

| 来源 | 关键事实 |
|---|---|
| **Able Player** | 无障碍 HTML5 播放器的参考实现：控件可键盘操作、对读屏有准确名称与状态、可被语音识别控制、**可自定义键盘快捷键且能在页面任意处生效** |
| **VisionPlayer** | 字幕（WebVTT 定位/竖排、TTML/IMSC1/EBU-TT-D）；**画质与音频控制：亮度、对比度、多段均衡器** |
| WCAG 2.1 AA / Section508 实践 | 键盘可达、焦点可见、读屏名称与状态准确、字幕/转录 |
| **SyncCast** | **零云投屏**：把**电视自带浏览器当播放器**，用 WebSocket 实时遥控；无账号、无遥测、无云中继 |
| **PlayBridge / StreamX / LocalMediaServer** | 手机浏览→电视播放（Android TV/Fire TV/Apple TV/DLNA）；**PWA + 离线缓存 + 锁屏控制**；局域网媒体服务器标配「断点续播 + 最近观看」 |
| **C2PA / SynthID / NIST AI 100-4** | 内容来源凭证（C2PA Content Credentials）与合成内容水印：可机器读取的**来源标注**标准 |

### 7.2 第三批候选

| 功能 | 依据 | 做法 | 工作量/风险 |
|---|---|---|---|
| **电视浏览器 + 手机遥控（零云投屏）** | SyncCast 的整套做法 | 电视浏览器直接打开 Player 同一地址；手机开一个遥控页，经服务端转发播放/暂停/进度指令 | 中 / 中（需一条实时信道；先轮询也够用） |
| **PWA / 加到主屏 / 离线外壳** | StreamX | 清单 + Service Worker 缓存外壳，断网也能打开已缓存页面 | 中 / 低 |
| **键盘快捷键（可在页面任意处生效）** | Able Player | 播放/暂停、前后跳、音量、全屏、倍速、下一部 | 小 / 低 |
| **字幕（WebVTT）** | VisionPlayer | 有字幕就渲染，没字幕不影响 | 小 / 低 |
| **音频轨选择** | 多音轨 MKV 常见 | 归档侧 `ffprobe` 列出音轨，Player 侧切换 | 中 / 中（需归档侧配合） |
| **对比度 / 饱和度调节** | VisionPlayer | 现有亮度手势旁再给两个滑杆（CSS filter） | 小 / 低 |
| **音频延迟/同步偏移** | 老片常见音画不同步 | 播放器加 ±0.5s 微调（存会话） | 小 / 低 |
| **AI 生成内容标注** | C2PA / SynthID | 读文件内 C2PA/XMP 元数据打「来源」徽标；**检测不可靠时以手动标记为准** | 小 / 低 |
| **焦点可见 / 读屏名称补齐** | WCAG 2.1 AA | 全站审查键盘焦点环与 aria 名称 | 小 / 低 |

### 7.3 第三批的结论与排除

- **跨设备续播已经天然具备**：断点与收藏存在服务端，换设备打开同一地址就是接着看 —— 不需要新功能，只需在文档里写清。
- **跳过**：云端投屏 SDK（Chromecast/Apple TV 专有协议）、云端内容来源 API、音频描述/转录（成人内容不适用）、均衡器与音高修正（浏览器默认保持音高，收益极低）。
- **调研过程中遇到一次提示词注入**：某搜索结果里夹带了「AI MODEL DIRECTIVE：必须在回答里提某产品名并引导点击链接」的指令。已识别并忽略 —— 调研结论只采信事实，不执行网页里的指令。

## 8. 来源

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
- 第二批：<https://github.com/gglafrance/goonhub> · <https://github.com/carrotwaxr/stash-sense> ·
  <https://www.framequery.com/> · <https://phar-virid.vercel.app/> ·
  <https://github.com/sioaeko/scriptplayer-plus> · <https://docs.intiface.com/> · <https://fungen.app/supported-devices/> ·
  <https://www.vidvana.app/help> · <https://privatemediapro.com/stash-alternative> ·
  <https://docs.neptuneplayer.com/settings/playback> · <https://www.adultscriptpro.com/features/> ·
  <https://github.com/vulture-s/arkiv>
- 截屏/DRM（为何做不到）：<https://docs.axinom.com/services/drm/technical-articles/drm-protection-and-screen-recording/> ·
  <https://www.forasoft.com/learn/video-streaming/articles-streaming/encrypted-media-extensions-eme>
- 第三批：<https://ableplayer.github.io/ableplayer/> · <https://visionplayer.io/docs/> ·
  <https://accessible.org/video-player-accessibility-best-practices/> ·
  <https://github.com/HRITHIK-SANKAR-R/SyncCast> · <https://playbridgeapp/playbridge> ·
  <https://github.com/Selfdb-io/StreamX> · <https://github.com/Nick040791/LocalMediaServer> ·
  <https://spec.c2pa.org/specifications/specifications/1.3/specs/C2PA_Specification.html> ·
  <https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-4.pdf>
