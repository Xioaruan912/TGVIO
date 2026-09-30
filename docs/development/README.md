# 开发与验证

仓库是两个独立 Python 服务加一个 Player Web 前端，不是一个共享状态的应用。
先读根 AGENTS.md 和目标目录的局部约定，再改目标模块。

## 目录职责

| 目录/文件 | 职责 |
|---|---|
| src/tgvio | Telegram Bot，持久化 Job 与发布/归档 |
| src/tgvio_player | Player API、认证、catalog、Range代理与独立数据库 |
| player/web/src | Vite/TypeScript UI 与播放控制 |
| tests | 两个 Python 服务及发布工具的离线回归 |
| player/web/tests | Node 行为测试、隔离浏览器 fixture |
| scripts | 验证、发布、运维工具 |
| deploy | 占位配置、Player配置样例与 pinned host key |
| docs/development | 当前开发/架构说明 |
| docs/operations | 操作规程与有日期的运维证据 |
| docs/handoffs | 本轮范围、结果与下一步 |

不新增 root 下的临时脚本或多个相互矛盾的“最新开发指南”。

## 环境与依赖

生产 Python 以 Dockerfile 固定的 CPython 3.11 镜像及 lock 为准；WSL现有 .venv 使用3.13，不能据此声称3.11验证通过。
Bot requirements.txt 声明直接依赖；requirements.lock 固定安装版本和hash。
Player后端仅安装 requirements.player.lock，不依赖 Telethon。
前端使用 Node 20.19+ 或 Node 22.12+、npm及package-lock.json，不混用 yarn/pnpm。

新建本地环境：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes --no-deps -r requirements.lock
.venv/bin/python -m pip install --require-hashes --no-deps -r requirements.player.lock
npm --prefix player/web ci
```

FFmpeg/ffprobe用于离线媒体测试；Chrome用于可选布局回归。不要让本地环境加载生产凭据。
依赖升级单独提交，锁文件和镜像输入必须同步，不能为修测试随意升级运行时。

## 统一检查

```sh
bash scripts/check.sh
bash scripts/check.sh --browser
# 指定生产等价Python：
PYTHON_BIN=/path/to/python3.11 bash scripts/check.sh
```

检查脚本不安装依赖、不启动Bot、不部署；默认选择 .venv/bin/python，缺少依赖明确失败。
它执行治理规则、源码秘密/路径扫描、既有Python分层检查、离线Python回归和前端check。
--browser额外运行隔离loopback fixture，不能代表iOS/Android真机或生产Range验收。

前端单独执行：

```sh
npm --prefix player/web run typecheck
npm --prefix player/web test
npm --prefix player/web run build
npm --prefix player/web run check
```

tsconfig.test.json继承严格生产配置，编译所有src到临时.test-dist。Node行为测试只导入编译产物，不直接导入.ts。
测试构建每次重建并清理，禁止依赖上次生成文件侥幸通过。

## 开发流程

1. 记录Git状态与任务范围，定位行为所有者及现有测试。
2. 缺陷补可失败的行为测试；结构整理先确认基线。
3. 只改相关模块，保留业务合同；大文件逐步缩小。
4. 跑目标检查及全量交付检查，审查diff/运行依赖/迁移。
5. 更新单独交接文档；按任务授权提交、同步或部署。
6. 记录源码版本与运行版本各自事实，失败门禁不写成成功。

前端设计与验收边界见 [Player 前端设计](PLAYER_FRONTEND.md)。main.ts既有1710行只登记为债务，不能以标准化为名未经行为回归整页重写。
