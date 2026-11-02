# GitHub Actions 前端运行时修复

- 范围：修复 Project checks 的 console 任务；不改 Bot 业务逻辑。
- 原因：Node 20 不满足锁定的 Vitest 5、jsdom 30 等依赖要求，测试 worker 在 undici 中启动失败。
- 修改：CI 和 Docker 前端构建阶段统一使用 Node 24；依赖及锁文件保持原样。
- 验证：Python 3.11.2、Node 24.21.0 下 `bash scripts/check.sh` 通过，507 项 Python 测试及前端类型检查、1 项测试和构建通过；actionlint 通过。
- Git 状态：本记录随工作流修复一起提交；GitHub 新运行结果以 Actions 为准。
- VPS 状态：本次不切换应用容器、不重启服务、不接触运行数据。
