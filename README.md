# TGVIO

把视频、图片或链接发给 Telegram 机器人，自动发布到你的频道，并可备份到网盘；另带一个私人网页播放器。

- 机器人：转发或发链接即可发布，支持合集、预览排序、选封面、改文字、撤销。
- 播放器：网页端浏览与播放已归档的视频，支持收藏、续播、隐私锁。前端在独立仓库 [TGVIO-Player](https://github.com/Xioaruan912/TGVIO-Player)，本仓库用 `player-web.lock` 固定所用版本。

## 首次安装

准备：一台装有 Docker 的服务器、机器人令牌（找 @BotFather 创建）、API_ID 与 API_HASH（在 my.telegram.org 获取）、你的频道（机器人需为管理员）、你的数字用户 ID（找 @userinfobot）。

```bash
git clone https://github.com/Xioaruan912/TGVIO.git
cd TGVIO
bash install.sh   # 选「1) 安装并启动」，按提示填写
```

然后在 Telegram 里给机器人发 `/start`，按键盘按钮操作。日志、停止、重建、修改配置也都在 `install.sh` 的菜单里。

## 日常使用

- 直接转发视频或图片，或发送视频链接，机器人会自动发布。
- 「新建合集」可把多条内容合成一组，发布前可预览、排序、选封面。
- `/source` 登录自己的账号后，可用 `/pick` 从指定来源挑选内容发布（只处理你有权使用的内容）。

## 更新生产环境

一条命令同时发布机器人和播放器（需本机已配置部署密钥）：

```bash
python3 scripts/deploy_hostdzire.py              # 机器人 + 播放器
python3 scripts/deploy_hostdzire.py --target player   # 只发播放器
```

只接受已推送到 `main` 的干净提交；每次发布都保留回滚点。细节见 [运维规程](docs/operations/README.md)。

## 数据

`data/`（数据库）、`session/`（登录信息）、`downloads/`（缓存）、`logs/`（日志）、`.env`（配置与密钥）都留在服务器本地，换机器时一起迁移，切勿外传。

## 开发

先读 [AGENTS.md](AGENTS.md) 和 [开发文档](docs/development/README.md)，提交前运行 `bash scripts/check.sh`。许可见 [LICENSE](LICENSE)。
