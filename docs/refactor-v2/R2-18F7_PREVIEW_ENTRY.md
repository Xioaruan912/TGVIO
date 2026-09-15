# R2-18F7：效果预览入口可发现性

状态：DELIVERED（2026-09-15）。migration=none。commit `31a51d26eadbbb7aacc1bbc584bd04aafc1ed6ca`，release `r2-18f7-31a51d2-20260915T025757Z`，479 tests，schema v16 不变；证据见 [R2-18F7_RELEASE.md](evidence/R2-18F7_RELEASE.md)。

用户 iOS 使用反馈：直接转发未经过合集，第一层合集预览也没有效果预览入口。

范围：
- 收集卡主按钮改为“👀 预览与整理”，使用只读文字预览路径；保留旧文本和旧 callback。
- 第一层文字预览提供“🖼 生成效果预览”，沿用 owner/revision 校验与独立资源预算。
- 保持 preview_enabled 默认 true；生成仍需主动点击，不自动下载、不自动发布、不改变直接转发语义。
- 首页说明先新建合集才能预览；草稿和同款再发提示同步。
- 按钮回归发现原有 COLLECTION_NEW_BUTTON 缺少导入，修复新建/结束/预览文本路由 NameError；新增直接调用 handler 的测试。
- 设置页动态显示持久清理时间，生产已设 19:00；每日编号业务日边界不改变。

验收：新旧导航文字不进入文案；打开文字预览不创建 Job/不执行效果生成；按钮不超过64字节；完整离线门禁及生产后验。

回滚：无 schema 变化，恢复上一运行时镜像与源码，不恢复数据库；19:00 配置仍保留。
真实 iOS 按钮布局和效果图片仍需用户验收，不以 fake 测试替代。

过程记录：首次构建在该 release 完成切换后、写元数据前被中断（`.release-commit`/`current` 停留 F6，`remote_preflight` 报 release-commit/source-manifest）；随后建立在线备份、用发布脚本 `atomic_metadata` 语义对齐到正在运行的 F7、确认 `blockers=[]`，再从 clean `31a51d2` 重跑唯一入口得到完整 release。
