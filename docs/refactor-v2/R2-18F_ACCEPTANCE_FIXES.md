# R2-18F 验收缺陷修复

2026-09-14，IN PROGRESS。此前“仅剩实机验收”的结论被独立审计推翻。

## F1 无迁移安全补丁

- 确认 token 消费失败时 fail closed，不创建 submission/Job；旧风格确认必须重新生成。
- 效果预览完成下载后重查 owner/revision/状态/TTL；雪花/ask 偏好保守隐藏。
- 超时覆盖生成及发送；取消记录失败；失败结果在 Bot 层显示文字降级提示。
- 启动清理只处理 preview ledger 记录的精确目录，不扫描删除正式 Job 目录。
- 结果卡过滤已撤销 effect，显示全部/部分撤销，收藏列表同步提示。
- 已有活动合集时，“再发/同款”拒绝冒充新建空合集，要求先保存或结束。

测试以临时 SQLite/fake client 验证，不使用用户媒体；全量门禁后 migration=none 发布。

## 后续 F2 必须完成，不能标成已修复

- 同款再发需要持久化 draft-scoped style，不能修改个人全局风格；新增 migration 前做最新生产副本 rehearsal。
- 草稿提交消费 token 后崩溃的完整冻结恢复、跨连接提交/编辑竞态需要进一步回归；本补丁不把 fail-closed 改成忽略错误重试。
- 预览实际流量硬预算、全局等待队列/TTL、启动清理分页及路径安全继续收紧。
- 真机操作仍未验收。

每阶段的 commit/release/独立后验以后续发布证据为准；未部署不标交付。

## F1 发布证据（2026-09-14）

- [x] F1：`06e0451cffd055b8e8f41c2cdac633fe4e1dc2e7` 已推送并发布；release `r2-18f1-06e0451-20260914T063914Z`，migration=none。
- 本地与生产隔离 test image：449 tests passed；secret scan/compileall/123 Python 文件1000行预算/runtime image inspection passed。
- image `sha256:f150008212d15d98ef460526b0758d0dd492234de482471a8399f0d80a31d964`；Git/宿主/容器 source manifest `f938a378725f1347c1697d7a74fc7f9169d37fba37928040eecccfc51ffdedd5`。
- 独立 vps_check：healthy、单实例、restart0、error0、bootstrap/Telegram ready各1、jobs0、blockers=[]、quick_check=ok。schema v14/hash `ef4930f53f2f3acaf515fee376676ea4d1b6c418f89fa5da47f1be57189924e8` 不变。
- `rollback_hostdzire.sh --check r2-18f1-06e0451-20260914T063914Z` passed。标准 release rollback/evidence 目录资产完整；previous runtime `1a9b86c`。无迁移代码回滚不要恢复旧数据库覆盖新业务事实。
- 新增回归：风格快照不一致拒绝创建Job/submission、owner雪花偏好、下载中revision变化、发送超时清缓存；原结果卡测试追加真实撤销checkpoint断言。
- 未用真实媒体做破坏性测试；真实手机验收及 F2 尚未完成。NTP no 仍需独立运维窗口。
