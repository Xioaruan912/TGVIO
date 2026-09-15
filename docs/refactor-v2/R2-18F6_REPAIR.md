# R2-18F6 二次验收修复

状态：DELIVERED。2026-09-14，上一轮 F5 独立验收仍有缺口；本阶段不新增 schema。

## 修复边界

- 数据库 append_collection_entries 在同一写事务检查 submission；已冻结则拒绝新增媒体/文字。Bot 单媒体/相册入口捕获专用异常，明确提示“未加入，请新建并重新转发”，不默默遗漏。
- 为已授权 creating submission 提供无 token 的内部恢复入口；runtime 每批至多20条、游标翻页、10秒间隔，失败保留冻结记录并继续其他项。恢复只读冻结快照，不读取新偏好、不从草稿重新创建授权。停止runtime时取消并等待恢复任务。
- 预览使用独立 download_bounded：真实流式数据每块写入前核对上限，不走正式下载回退；远端可用size超过限制直接拒绝。网络传输可能多读一个64KiB块用于检测，落盘永不写入超限块，不承诺网络零超读。
- 下载取消后等待子任务退出再清目录；路径检查失败立即拒绝，不回退原始路径；拒绝缓存根及祖先符号链接、缓存外来源文件。
- 不改变正式下载路径、FIFO、Archive、partial/uncertain 或撤销语义。

## 验证

新增临时SQLite冻结后媒体/文字拒收与重开数据库无token恢复、真实adapter预算前置拦截、路径拒绝不下载测试。全量门禁/远端隔离测试及独立后验通过后才标交付。不用用户媒体做测试；Android/iOS实机仍待验。

## 发布与回滚

按唯一入口发布R2-18F6，migration=none，schema v16不变；代码回滚不能恢复旧DB覆盖新业务记录。准确commit、release、image及后验在发布后补录。

## 正式交付证据

- [x] commit `53cbb2d0654b7d1e686216e0e4530d5c88c5ac29` 已推送；release `r2-18f6-53cbb2d-20260914T120716Z`，2026-09-14，migration=none。
- 本地及服务器无网络测试容器475 tests通过；secret scan、compileall、123个Python源文件/1000行预算、runtime image检查通过。
- image `sha256:bcb3174a437a23bb1834eb5b14c790fe5f30aa271651e71d5c7ffccf5e352cd6`；Git/宿主/容器source manifest `fe13b19bc0d54faaff7ed583c8e415047b78dcda2cfa7ed830d9ad307cc706d5`。
- 独立vps_check：healthy、单实例、restart0、error0、bootstrap/Telegram ready各1、jobs0、blockers=[]、quick_check=ok；schema v16/hash `59624f44635dd5ff7a31f313780d0aa4669cbf4bf18efae7405f98ec4ea0102c` 不变。
- `rollback_hostdzire.sh --check r2-18f6-53cbb2d-20260914T120716Z` passed；标准release目录保留manifest/ledger及rollback资产。F6 发布时 NTP 仍 no 且未擅自调整；2026-09-15 最新报告已为 `ntp_synchronized=yes`。F6 之后由 F7（`31a51d2`）补上效果预览入口，见 [R2-18F7_RELEASE.md](evidence/R2-18F7_RELEASE.md)。
- 未做真实Android/iOS与用户媒体破坏性测试；不能以测试通过代替实机验收。
