# 架构与行为边界

## 服务关系

```text
Telegram -> Bot intake -> SQLite Job -> download/analyze
                                  -> FIFO publish -> effect/receipt
                                  -> Archive manifest + _COMPLETE
                                             |
                                             |
                                   WebDAV（网盘）-> PPVIO（独立项目，另一个仓库）
```

本仓库只有 Bot。播放器是独立项目 PPVIO，与本仓库只通过网盘上的归档包交接；源码不能相互 import。

## Python依赖方向

| 层 | 职责 |
|---|---|
| domain | 标准库与纯业务模型，不接触网络/存储/UI |
| application | 用例、状态协调与ports，不导入具体adapter/infrastructure |
| infrastructure | SQLite、迁移、媒体探测、具体协议能力 |
| adapters | Telegram/HTTP输入、鉴权、输出，调用应用服务 |
| main.py | 装配、生命周期，不被业务模块导入 |

自动门禁按release_guard中现有规则执行。infrastructure允许domain/observability。
改架构规则前先证明需要，不能通过放宽门禁掩盖循环或越层依赖。

## Bot必须保留的行为

- Job和副作用以SQLite为真相；下载可并发，发布按接受顺序，claim与状态事务有界。
- 外部发布先记录计划，返回后记录receipt/effect；partial/uncertain不盲目重发。
- 归档与发布状态独立，失败缓存受保护；WebDAV PUT后确认远端大小，慢后端重试保留。
- 讨论组评论回复实际线程根；相册最多10项/组，显式spoiler与视频属性、分卷、封面语义保留。
- callback不超过64字节，含owner/revision/TTL/single-use保护；危险操作继承现有确认合同。
- 来源读取只由owner主动选择白名单内容；不恢复旧#tgvio触发词/自动监听。
- UTC持久化、Asia/Shanghai展示；不可变migration/checksum；损坏库fail closed。

## 模块预算

Python生产文件不超过1000行。
repository_hygiene.py会检查预算、规范跟踪与当前文档链接；不得增加白名单以容纳新的杂物大文件。
