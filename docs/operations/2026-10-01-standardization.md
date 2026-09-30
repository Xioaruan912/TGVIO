# 2026-10-01 标准化与VPS清理证据

## 范围与验证

开发规范、文档、测试与检查工具标准化；没有更改Python/前端运行应用代码，没有数据库迁移。
本地完整检查通过：Python3.13.5的999项；Node20.19.2前端198项；严格TS检查与Vite构建；治理/架构/秘密扫描、shell语法、git diff --check。
隔离Chrome在390/430/768/1440宽度分别124/124/124/123项检查通过；不等于iOS/Android真机验收。
补充生产等价Python3.11.16：既有test镜像挂载当前源码只读，--network none运行999项，OK（1项skip）。未启动Bot或使用生产凭据。

## 生产初始状态与后验

| 服务 | 实际运行commit | 容器ID前缀 | restart | health |
|---|---|---|---|---|
| Bot | 5dc86a70bafb016c05f52b5ad0e1ac4d375cb799 | 408fd4e67f66 | 0 | healthy |
| Player | 8afd10769a39e568bcea93a7ae14e0d42bcd2aeb | f5e9cc7165e1 | 0 | healthy |

清理后以上ID、image、restart和health均不变；vaultwarden仍healthy。
cover-grid-20260930T170819Z-45491d8候选存在，实际未运行；本轮没有替用户切换该候选。
保护Player当前sky-ui-8afd107、上一回滚sky-ui-27cbd8a和cover-grid候选。
核验Bot与Player上一可用回滚镜像仍存在。

## 已执行清理

先生成精确plan，执行前验证真实路径、symlink祖先、inode/device、大小和运行身份。
仅删除：

- 四个2026-09-23旧Player release中的source/web/node_modules。
- tgvio-player-home-refresh-20260925/source/player/web/.test-dist。

文件体积合计225,614,618字节；Docker未使用且超过168小时的构建缓存另回收107.5MB（Docker报告）。
两者约333MB（十进制），不是整盘df差值；运行数据会持续写入。
执行后根盘可用27,067,121,664字节；下载34GB、Player缓存17GB、DB/session/env、release源码/镜像传输包和回滚备份均保留。

VPS完整plan/receipt：/root/TGVIO-maintenance/2026-10-01/cleanup-plan.json、cleanup-result.json。
控制端审计脚本私有备份：/root/.local/state/tgvio-standardization/2026-10-01/vps-cleanup.py。
未来清理继续按operations/README流程，不将本次路径清单复用于新版本。

## 源码同步方式

本轮提交推送后，通过git archive同步到独立/root/TGVIO-snapshots/<full-commit>/source，核验archive SHA256和全文件manifest。
/root/TGVIO-source-current专指最近同步源码；/root/TGVIO-current仍指Bot运行release。
共享运行目录/root/TGVIO的旧AGENTS先私有备份，再写最小导航，指向源码快照内的完整规则。
Player共享运行目录同样写导航。旧共享目录中未使用的历史src不是开发基线。
快照同步不切换容器/镜像/运行release指针，不改生产配置或运行数据。
精确提交、hash与最终同步结果以VPSsource-sync.json及本轮最终交接为准。
