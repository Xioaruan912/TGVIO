# 2026-10-04 Player 长视频页亮度/音量竖向手势发布

按 `docs/superpowers/plans/2026-10-04-player-level-gestures.md` 交付并上线。
**只切换 Player**，Bot 全程未动。

## 用户需求与已拍定的三条语义

长视频页：**左半屏上下滑调亮度、右半屏上下滑调音量**；片库墙与既有手势零变化。

1. **左右半屏上下滑**，不需要长按（避免与既有"长按按住快进"打架）。
2. 音量手势走**与点声音按钮完全同一条** `requestAudioEnable` 路径，绝不绕开用户设的
   「每次询问 / 一次 / 持续」。
3. 亮度与音量**只在本次会话有效**：跨视频保留、刷新回默认，不写设备偏好。

## 发布事实

| 项目 | 值 |
|---|---|
| 应用提交 | `f673061d7629f7b579f69c52b3af24811be80750` |
| Release id / tag | `level-gestures-f673061` / `tgvio-player:level-gestures-f673061` |
| VPS 导入镜像 ID | `sha256:ecc8fb959b9047ba800bc39a690cbe98a28123ee379a675511c0f72253bbb8a2` |
| 传输包 SHA-256 | `b716382cdff2969900cee4974aac8f763f356c9071234a3d5f65a61f86d515ad`（两端一致） |
| 新 Player 容器 | `fd402c9e2117c3dadbf8d37b43ae37d5fc1902f87c92bf2fd32cd9286331d9c0`（healthy / restarts 0） |
| 回滚点 | `/root/tgvio-player/rollback-20261003T233131Z`（原镜像 `sha256:38e2a4ec…`、原 env、切换前库） |
| Bot | `408fd4e6…` restarts 0，未变 |
| 迁移 | 无（纯前端改动，账本未变） |

## 实现要点

- **手势**：`gestures.ts`（全站唯一的指针状态机）新增一条 **opt-in** 竖向分支。按下时按左右半屏
  定死轴（跨中线不改轴），竖滑成立即接管该指针（`preventDefault` + 指针捕获），
  `fraction = (起点Y − 当前Y) / 舞台高`，向上为正。**未启用（片库墙）时逐字节等同今天**：
  不 `preventDefault`、不捕获、不回调 —— 由三条用例钉住（关闭态、被拒态、横向拖拽仍是进度拖拽）。
- **档位**：新模块 `components/level-control.ts`（160 行）持有**模块级会话状态**；亮度是
  `video.style.filter`（范围 20–100，下限防"滑到全黑以为坏了"），音量是 `video.volume`（0–100）。
  构造时即应用会话档位，所以下一个视频打开就是调好的样子。
- **静音归属**：控件**从不写 `video.muted`**，只向宿主请求（`mute()` / `requestAudio()`）；
  宿主 `large.ts` 的 `enableSound()` 与声音按钮 `toggleSound()` 是**同一个函数**，
  于是"手势与按钮不会对有没有声音产生分歧"是结构保证，不是约定。
- **音量归零的语义**：档位 0 = 静音，但**元素保留最后可听音量**（`lastAudible`）——
  否则先滑到 0 再点"开启声音"会出现"按钮说有声、画面没声"。
- **触控**：只给 `.large-stage` 加 `touch-action: none`（舞台本就 `overflow: hidden`）；
  `.large-player` 与 `#feed` 的 `pan-y` 未动，片库墙仍是原生滚动。
- **可读性**：舞台中央的档位读数（视觉层 `aria-hidden`）+ `.sr-only` 朗读层，
  **手势结束才播报一次**（避免每次 `pointermove` 重写 live region 冲垮读屏）。

## 上线后验

| 检查 | 结果 |
|---|---|
| 门禁 | `bash scripts/check.sh --browser` → **`project_checks=passed`**（Python 1166、Web **344/344**、浏览器 682 checks + 布局 141–147/视口） |
| 公网 | `/` 200、`/healthz` 200、未登录 feed 401 |
| 容器 | running / **healthy** / restarts 0 / revision 匹配提交 |
| Bot | 容器 ID 与重启次数未变 |
| 封面镜像（上一特性） | 仍启用：`files 945 / bytes 12311680 / pending 0 / failed 0` |
| **线上产物确实带本特性** | 抓取线上 bundle：JS 命中 `level-hud`、CSS 命中 `touch-action:none` |
| 真浏览器验证 | fixture 里用真实 `LargePlayer`：左半屏下滑变暗、右半屏下滑降音量、**音量手势不会自行解除静音**、舞台 `touch-action: none`、片库墙 `pan-y` |

## 复审与修复

整支复审（fresh reviewer，`opencode-go/deepseek-v4-pro`，high thinking）：
**0 Critical / 2 Important / 5 Minor**；五条 Review Focus 全部「未发现问题」。

两条 Important 都是一次修复轮内解决（各带先失败的测试）：

1. **音量归零后与声音按钮分叉**：原先滑到 0 会把元素音量也写成 0，之后再点"开启声音"
   会出现"按钮显示有声、画面静音"。现在档位 0 = 静音，元素保留最后可听音量。
2. **拒绝声音提示后每次滑动都重新弹窗**：现在一次拒绝即管完整段手势，新手势才可再问。

另修四条 Minor：`prompting` 在提示抛错时用 `try/finally` 复位、`move()` 复查隐私锁
（锁在滑动中途落下即冻结档位）、读数水平居中改为显式 `left/transform`、
读屏播报改为手势结束播一次。**1 条 Minor 记账未修**：归零会经 `host.mute()` 写
`rememberMuted(true)`，只影响用户显式选择"持续有声"的模式，已确认无实际危害。
