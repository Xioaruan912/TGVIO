# 重构说明入口

> 状态：历史入口，2026-09-10 起由 [refactor-v2/README.md](refactor-v2/README.md) 取代。

当前本地 Git 树和 HostDZire 生产源码已经分叉：本地 `main` 仍是旧 `src/` 架构，生产运行的是 `/root/TGVIO` 下的 `src/tgvio` clean-room rewrite。旧版本文曾描述 schema 5、B1 等阶段，已经不再代表当前代码或下一步；相关提交仍可通过 Git 历史追溯。

不得从本地旧树构建后覆盖生产。新的权威资料是：

- [refactor-v2/CURRENT_STATE.md](refactor-v2/CURRENT_STATE.md)：本地、Git、镜像、数据库与生产事实。
- [refactor-v2/FEATURE_CONTRACT.md](refactor-v2/FEATURE_CONTRACT.md)：现有能力、兼容缺口和明确退役项。
- [refactor-v2/TARGET_ARCHITECTURE.md](refactor-v2/TARGET_ARCHITECTURE.md)：目标依赖、数据、调度和副作用模型。
- [refactor-v2/ROADMAP.md](refactor-v2/ROADMAP.md)：R2-00～R2-10 实施与验收顺序。
- [refactor-v2/DEPLOYMENT_HOSTDZIRE.md](refactor-v2/DEPLOYMENT_HOSTDZIRE.md)：所有 release build 的 HostDZire 交付协议。

当前唯一允许的下一代码阶段是 R2-01：先把生产源码完整、脱敏、可验证地回收进 Git，保持行为不变；之后才能开始 migration、scheduler 或功能等价工作。
