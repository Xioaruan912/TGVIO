# 2026-10-25 播放器拆分为独立项目 PPVIO

## 范围

把 TGVIO 里的播放器（`src/tgvio_player`、`web/`、Player 发布脚本与测试）拆成独立仓库
[PPVIO](https://github.com/Xioaruan912/PPVIO)；TGVIO 只保留采集机器人。

## 事实

- 拆分基于 TGVIO `c2a3f80`。PPVIO 首个提交 `Xioaruan912/PPVIO@main`。
- 播放器代码原本就不 import Bot；唯一跨包的测试（封面合同常量）改为固定值。
- 网盘与数据库里已写入的格式标识（`.tgvio/`、`tgvio-file-v1`、`tgvio-rendition-v1`、`tgvio-folder-v1`、
  `tgvio-library-folder-v1:`、`tgvio-player-*` 加密/备份格式串、`tgvio.archive*` schema）在 PPVIO 中保持原名。
- PPVIO 改名的部分：包 `ppvio`、环境变量 `PPVIO_*`、容器 `ppvio`、主机目录 `/root/ppvio`、
  镜像标签 `io.ppvio.web.revision`、登录 cookie `ppvio_session`（切换后需重新登录一次）。

## 已完成（TGVIO 侧）

- 删除 Player 代码、前端、测试、`Dockerfile.player`、`docker-compose.player.yml`、`requirements.player.lock`、
  `deploy/player.env.example` 与 `scripts/player_*`。
- `deploy_hostdzire.py` 只发 Bot（`--target bot` 保留兼容）；Bot 镜像测试阶段不再安装 Player 依赖。
- `release_guard` 禁止 Bot import `ppvio`；`check.sh` 与 CI 去掉前端步骤。
- README、AGENTS、开发 / 架构 / 运维文档改为指向 PPVIO；Player 相关历史记录保留，只作追溯。

## 切换（PPVIO 侧）

第一次 `python3 scripts/release.py` 时，`scripts/adopt_tgvio_player.sh` 接管旧服务：
SQLite 备份 → `player.env` 改名为 `ppvio.env`（去掉服务已不读取的 WebDAV 四项）→ 停 `tgvio-player`（保留容器）→
数据目录原地移到 `/root/ppvio/data` → 在同一端口 8790 启动 `ppvio`；任何一步失败自动还原并重新启动旧容器。
