# TGVIO

把视频、图片或链接发给 Telegram 机器人，它会自动发布到你的频道，并把原片备份到网盘；再配一个只给自己用的网页播放器，在手机上刷、看、整理这些视频。

```text
你 ──转发/链接──▶ Telegram 机器人 ──发布──▶ 你的频道
                         │
                         └─归档(WebDAV)──▶ 网盘（如 OpenList 挂载的 115）
                                               │
                     网页播放器 ◀──只读目录、按需取流──┘
```

## 能做什么

**机器人（Bot）**

- 转发视频/图片或发链接即可发布；支持合集、发布前预览与排序、选封面、改文字、撤销。
- 每次转发打成一个“归档包”（原片 + `manifest.json` + `_COMPLETE.json`）写入网盘；发布与归档互不阻塞，失败自动重试。
- `/source` 登录自己的账号后，可用 `/pick` 从白名单来源挑选内容发布。
- 附带两个可选维护进程：为旧视频补封面、补 480p/720p 低清副本。

**播放器（Player）**

- 短视频上下滑刷（未看过的优先）、长视频独立播放器（续播、倍速、清晰度、手势）。
- 片库按日期/文件夹浏览、筛选与排序；收藏、集合（自动在网盘建同名文件夹备份）。
- 永久删除（可撤销，后台删净网盘文件）；疑似重复视频审核；同一视频存了多份时自动只留一份。
- 看过标记（默认半个月后重新算作没看过）、隐私锁、闲置自动退出、可安装为手机应用（PWA）。
- 读取网盘可走 WebDAV，或经 OpenList 直取 115 链接（更快）；本机磁盘缓存让起播与拖动更快。

## 三个仓库

| 仓库 | 内容 |
|---|---|
| [TGVIO](https://github.com/Xioaruan912/TGVIO)（本仓库） | Bot、Player 后端、维护进程、镜像与部署脚本 |
| [TGVIO-Player](https://github.com/Xioaruan912/TGVIO-Player) | Player 网页前端（Vite + TypeScript）；本仓库用 `player-web.lock` 固定所用提交 |
| [115List](https://github.com/Xioaruan912/115List) `tgvio` 分支 | 适配 TGVIO 的 OpenList：115 取链防风控、链接缓存、批量删除后核实；构建与升级见该分支 `tgvio/README.md` |

## 安装机器人

准备：一台装有 Docker 的 Linux 服务器；机器人令牌（@BotFather）；API_ID 与 API_HASH（my.telegram.org）；你的频道（机器人需为管理员）；你的数字用户 ID（@userinfobot）。

```bash
git clone https://github.com/Xioaruan912/TGVIO.git
cd TGVIO
bash install.sh      # 选「1) 安装并启动」，按提示填 5 项
```

脚本会生成 `.env`、构建镜像、检查配置、启动容器，并观察约 20 秒确认能登录 Telegram；凭证不对会停下并说明原因。之后在 Telegram 给机器人发 `/start`。

同一菜单还能查看日志、停止、删除（可选保留数据）、重建/更新（自动 `git pull`）、分组修改配置。归档到网盘在「7) 修改配置 → 4) 归档」里填 WebDAV 地址与账号。

## 安装播放器（可选）

播放器是独立容器，只读取网盘里已完成的归档包，不碰机器人的数据和登录信息。

1. 复制 `deploy/player.env.example` 到仓库外（权限 `0600`），填写访问口令、恢复密钥（32 字节随机，换机器要沿用）、WebDAV 地址/账号、归档根目录，并设 `TGVIO_PLAYER_ENABLED=true`。
2. 构建镜像：`bash scripts/player_release.sh --tag tgvio-player:local`（前端按 `player-web.lock` 自动取用）。
3. 把 env 中 `TGVIO_PLAYER_IMAGE` 改为该标签，启动：`bash scripts/player_deploy.sh --env-file /path/player.env --execute`。
4. 播放器只监听本机 `127.0.0.1:8790`，请用你自己的反向代理（HTTPS）对外提供。

可选加速：OpenList 与播放器在同一台机器时，填 `TGVIO_PLAYER_WEBDAV_INTERNAL_URL` 走内网，填 `TGVIO_PLAYER_OPENLIST_API_URL` 后可在播放器设置里切到“115 直连”。

## 更新生产环境

维护者在本机一条命令发布（需已配置部署密钥），只接受已推送到 `main` 的干净提交，每次都保留回滚点：

```bash
python3 scripts/deploy_hostdzire.py                    # 机器人 + 播放器
python3 scripts/deploy_hostdzire.py --target player    # 只发播放器（核实机器人未被动到）
bash scripts/maintenance_redeploy.sh                   # 封面/副本维护进程
bash scripts/player_browser_smoke.sh feed,settings     # 真 Chrome 只读冒烟
```

细节见 [运维规程](docs/operations/README.md)。

## 数据

机器人：`.env`、`data/`（数据库）、`session/`（登录信息）、`downloads/`（缓存）、`logs/`。播放器：自己的数据目录（`player.sqlite3`、缓存、封面镜像）和 player env。这些都只在服务器本地，换机器时一起迁移，切勿提交或外传。

## 开发

先读 [AGENTS.md](AGENTS.md)：它有规则，也有代码地图，按地图直接找到要改的文件，不必通读全部代码。提交前运行 `bash scripts/check.sh`。许可见 [LICENSE](LICENSE)。
