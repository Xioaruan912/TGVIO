# TGVIO 完全重构 V2

> 状态：规划基线（2026-09-10）
> 适用范围：`TG_Upload_bot` Git 仓库与 HostDZire 上的 TGVIO 生产实例
> 权威性：从本文件建立之日起，新重构工作以本目录为准；旧 `docs/REFACTORING.md`、`docs/R0_BASELINE.md`、`todo.md` 和 `AGENTS.md` 的历史阶段记录仅用于追溯。

## 1. 结论

本次不是在旧 `src/bot.py` 上继续拆 facade，也不是再写第三套实现。HostDZire 已运行一套真正的 clean-room rewrite（包名 `tgvio`），它已经具备 durable Job、PublishPlan、side-effect journal、Telegram 发布、Archive V2、恢复、诊断和 137 项离线测试。V2 重构以这套生产源码为唯一代码基线，先把它完整、可验证地恢复进 Git，再按可回滚阶段继续治理。

在生产源码进入 Git、来源清单可复现之前，禁止修改本地旧运行代码后直接覆盖 `/root/TGVIO`。

## 2. 总目标

- 保证当前生产 TGVIO 的所有已验证能力不回归。
- 把旧系统仍有价值但 TGVIO 尚未恢复的功能逐项纳入显式兼容合同。
- 让 domain、application、ports、adapters、infrastructure、interfaces 和 runtime 具有可自动检查的单向依赖。
- 用版本化 migration、事务边界、durable claim/lease 和外部副作用凭据保证重启安全。
- 保持单 VPS、单 Bot、单进程优先；没有量化需求前不引入 Redis、Celery 或 Kubernetes。
- 每个成功的发布构建在同一交付阶段传到 HostDZire，完成备份、切换、健康检查和回滚点记录；不留下“只在开发机验证、生产未交付”的 release build。

## 3. 不可协商的原则

1. SQLite 是持久状态真相；进程内队列只能做唤醒和缓存。
2. 先持久化计划，再执行 Telegram/WebDAV 外部副作用。
3. 外部发送拿到凭据后先提交 effect/receipt，再更新 UI。
4. 不确定是否已产生外部副作用时进入 `partial/uncertain`，不得盲目自动重发。
5. 全局发布顺序是业务语义，不依赖某个 `Future` 永远等待来维持。
6. WebDAV Archive 与 Telegram 发布正交，任何一个的 UI 失败都不能改变另一个的真实结果。
7. 生产 `.env`、`session/`、`data/`、`downloads/`、`logs/` 永远不进入 Git、镜像层或发布包。
8. 绝不同时运行两个使用同一 `BOT_TOKEN`/Telethon session 的实例。
9. 不修改已经部署的 migration；只新增前向 migration，并在生产副本上先演练。
10. 每个阶段都必须可独立部署、可回滚、可解释；不做一次性大爆炸替换。

## 4. 文档地图

- [CURRENT_STATE.md](CURRENT_STATE.md)：本地、Git 与生产的证据化现状和阻塞项。
- [FEATURE_CONTRACT.md](FEATURE_CONTRACT.md)：必须保持、待补齐和明确退役的功能合同。
- [TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md)：目标模块、依赖、状态机、事务与恢复模型。
- [ROADMAP.md](ROADMAP.md)：按依赖排序的实施阶段、验收与回滚边界。
- [DEPLOYMENT_HOSTDZIRE.md](DEPLOYMENT_HOSTDZIRE.md)：每个 release build 到 HostDZire 的强制交付协议。

## 5. 完成定义

“完全重构完成”必须同时满足：

- 生产源码的完整 Git commit、构建 manifest、镜像 digest 和容器内源码 manifest 可以相互追溯。
- [FEATURE_CONTRACT.md](FEATURE_CONTRACT.md) 中所有 `REQUIRED` 项为 `VERIFIED`，或有用户明确记录的退役决策。
- 大文件、合集、封面/评论区、spoiler、FIFO、取消/暂停/重试/撤销、Archive、恢复和磁盘清理都有自动测试及至少一次受控生产验收。
- SQLite 使用不可变版本化 migration，升级与回滚演练有记录。
- 依赖边界测试通过，单个基础设施或 UI 文件不再承担整个子系统。
- 发布脚本默认 fail closed，不读取或输出密码，不覆盖运行卷，不启动第二个 Bot。
- 最新 release 已推送 GitHub 并部署 HostDZire；容器健康、无新增重启、数据库完整、源码 hash 一致。

## 6. 当前工作纪律

- 本轮先完成文档，不改生产运行代码、不重建容器。
- 下一阶段首先执行 R2-01“生产源码回收与 Git 权威恢复”，不是直接实现新功能。
- 任何发现的生产新增逻辑先合并回 Git，禁止拿本地旧树覆盖生产。
- 文档中的密码、token、Authorization、代理/WebDAV 凭据一律视为缺陷；主机地址、端口、用户和目录不是秘密，可记录用于自动化。
