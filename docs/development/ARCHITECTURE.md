# 架构与行为边界

## 服务关系

```text
Telegram -> Bot intake -> SQLite Job -> download/analyze
                                  -> FIFO publish -> effect/receipt
                                  -> Archive manifest + _COMPLETE
                                             |
                                      Player catalog -> player.sqlite3
                                             |
Browser -> Player auth/API -> bounded HTTP Range -> WebDAV
```

Player与Bot独立装配、容器、依赖和数据卷；源码不能相互import。Bot Dashboard与Player职责分离。

## Python依赖方向

| 层 | 职责 |
|---|---|
| domain | 标准库与纯业务模型，不接触网络/存储/UI |
| application | 用例、状态协调与ports，不导入具体adapter/infrastructure |
| infrastructure | SQLite、迁移、媒体探测、具体协议能力 |
| adapters | Telegram/HTTP输入、鉴权、输出，调用应用服务 |
| main.py | 装配、生命周期，不被业务模块导入 |

自动门禁按release_guard中现有规则执行。Bot的infrastructure允许domain/observability，Player还允许application端口。
改架构规则前先证明需要，不能通过放宽门禁掩盖循环或越层依赖。

## Bot必须保留的行为

- Job和副作用以SQLite为真相；下载可并发，发布按接受顺序，claim与状态事务有界。
- 外部发布先记录计划，返回后记录receipt/effect；partial/uncertain不盲目重发。
- 归档与发布状态独立，失败缓存受保护；WebDAV PUT后确认远端大小，慢后端重试保留。
- 讨论组评论回复实际线程根；相册最多10项/组，显式spoiler与视频属性、分卷、封面、撤销语义保留。
- callback不超过64字节，含owner/revision/TTL/single-use保护；危险操作继承现有确认合同。
- 来源读取只由owner主动选择白名单内容；不恢复旧#tgvio触发词/自动监听。
- UTC持久化、Asia/Shanghai展示；不可变migration/checksum；损坏库fail closed。

## Player必须保留的行为

- 只同步committed Archive的有界元数据；浏览器使用opaque media ID，不接收远端路径/凭据。
- Range保留200/206/416、背压、并发预算和断开取消，不整文件缓冲。
- catalog、收藏/进度、可选操作均用Player自己的数据库和权限；不借用Bot主库。
- 普通播放/封面读走只读adapter；显式WebDAV写/删除仅使用已有受控操作与确认，不能让读接口顺带写远端。
- 封面是可选元数据，有1MB预算、独立并发与版本；缺封面不拒绝可播放包。
- Feed复用3个video，只有current播放；UI更新不重建video DOM。
- PlaybackStateController管理UI状态；FavoriteMutations合并每媒体的最新意图；PreloadCoordinator有界低优先级预热。
- 页面生命周期只有一个所有者；请求/定时器/媒体事件在换页时取消或按generation隔离。
- 中文、声音确认、隐私锁、长片续播、手动清晰度、收藏备份与PWA保持；细则见player/AGENTS.md。

## 模块预算

Python生产文件不超过1000行。前端新TS模块不超过600行；当前main.ts（1710）和large.ts（770）冻结为只能减少的债务。
repository_hygiene.py会检查预算、规范跟踪、测试入口与当前文档链接；不得增加白名单以容纳新的杂物大文件。
