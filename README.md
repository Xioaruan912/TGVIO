# TGVIO

Telegram 视频采集机器人：从普通频道或受限频道收集视频，组合成合集，发布到你自己的频道，同时把原片归档到网盘（WebDAV）。

想看的时候用 [PPVIO](https://github.com/Xioaruan912/PPVIO)：它是独立的个人视频播放器，直接从同一个网盘读取，不受 Telegram 限速。两个项目只通过网盘目录交接，互不依赖。

```text
你 ──转发 / 受限频道挑选 / 链接──▶ TGVIO ──发布──▶ 你的频道
                                     │
                                     └─归档(WebDAV)──▶ 网盘（如 OpenList 挂载的 115）──▶ PPVIO
```

## 能做什么

- 转发视频 / 图片或发链接即可发布；支持合集、发布前整理顺序、选封面、改文字、发布风格。
- `/source` 登录自己的账号后，可用 `/pick` 从白名单来源（含受限频道）挑选内容发布。
- 每次发布打成一个“归档包”（原片 + `manifest.json` + `_COMPLETE.json`）写进网盘的 `YYYY-MM-DD/` 目录；发布与归档互不阻塞，失败自动重试。
- 出错时私信提醒你；每天自动收起已完成任务、清理缓存。

## 相关仓库

| 仓库 | 内容 |
|---|---|
| [TGVIO](https://github.com/Xioaruan912/TGVIO)（本仓库） | 机器人、镜像与部署脚本 |
| [PPVIO](https://github.com/Xioaruan912/PPVIO) | 个人视频播放器（读网盘，含封面、低清副本、收藏、删除、去重） |
| [115List](https://github.com/Xioaruan912/115List) `tgvio` 分支 | 适配的 OpenList：115 取链防风控、链接缓存、批量删除后核实 |

## 安装机器人

准备：一台装有 Docker 的 Linux 服务器；机器人令牌（@BotFather）；API_ID 与 API_HASH（my.telegram.org）；你的频道（机器人需为管理员）；你的数字用户 ID（@userinfobot）。

```bash
git clone https://github.com/Xioaruan912/TGVIO.git
cd TGVIO
bash install.sh      # 选「1) 安装并启动」，按提示填 5 项
```

脚本会生成 `.env`、构建镜像、检查配置、启动容器，并观察约 20 秒确认能登录 Telegram；凭证不对会停下并说明原因。之后在 Telegram 给机器人发 `/start`。

同一菜单还能查看日志、停止、删除（可选保留数据）、重建/更新（自动 `git pull`）、分组修改配置。归档到网盘在「7) 修改配置 → 4) 归档」里填 WebDAV 地址与账号。

## 更新生产环境

维护者在本机一条命令发布（需已配置部署密钥），只接受已推送到 `main` 的干净提交，每次都保留回滚点：

```bash
python3 scripts/deploy_hostdzire.py --phase R2-NN
```

细节见 [运维规程](docs/operations/README.md)。

## 数据

`.env`、`data/`（数据库）、`session/`（登录信息）、`downloads/`（缓存）、`logs/` 都只在服务器本地，换机器时一起迁移，切勿提交或外传。

## 开发

先读 [AGENTS.md](AGENTS.md)：它有规则，也有代码地图。提交前运行 `bash scripts/check.sh`。许可见 [LICENSE](LICENSE)。
