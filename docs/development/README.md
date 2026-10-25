# 开发与验证

本仓库是 TGVIO 采集机器人（Python）。播放器是独立项目 PPVIO。
先读根 AGENTS.md 和目标目录的局部约定，再改目标模块。

## 目录职责

| 目录/文件 | 职责 |
|---|---|
| src/tgvio | Telegram Bot，持久化 Job 与发布/归档 |
| tests | Bot 及发布工具的离线回归 |
| scripts | 验证、发布、运维工具 |
| deploy | 占位配置与 pinned host key |
| docs/development | 当前开发/架构说明 |
| docs/operations | 操作规程与有日期的运维证据 |
| docs/handoffs | 本轮范围、结果与下一步 |

不新增 root 下的临时脚本或多个相互矛盾的“最新开发指南”。

## 环境与依赖

生产 Python 以 Dockerfile 固定的 CPython 3.11 镜像及 lock 为准；WSL现有 .venv 使用3.13，不能据此声称3.11验证通过。
Bot requirements.txt 声明直接依赖；requirements.lock 固定安装版本和hash。

新建本地环境：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes --no-deps -r requirements.lock
```

FFmpeg/ffprobe用于离线媒体测试。不要让本地环境加载生产凭据。
依赖升级单独提交，锁文件和镜像输入必须同步，不能为修测试随意升级运行时。

## 统一检查

```sh
bash scripts/check.sh
# 指定生产等价Python：
PYTHON_BIN=/path/to/python3.11 bash scripts/check.sh
```

检查脚本不安装Python依赖、不启动Bot、不部署；默认选择 .venv/bin/python，缺少依赖明确失败。
它执行治理规则、源码秘密/路径扫描、既有Python分层检查与离线Python回归。

## 开发流程

1. 记录Git状态与任务范围，定位行为所有者及现有测试。
2. 缺陷补可失败的行为测试；结构整理先确认基线。
3. 只改相关模块，保留业务合同；大文件逐步缩小。
4. 跑目标检查及全量交付检查，审查diff/运行依赖/迁移。
5. 更新单独交接文档；按任务授权提交、同步或部署。
6. 记录源码版本与运行版本各自事实，失败门禁不写成成功。

界面设计决定（炭黑配珊瑚红、手势）见 docs/refactor-v3/README.md。
