# R2-21 SKY Player 发布（已完成）

## 实际运行

- 用户明确授权测试后推送GitHub及VPS部署；只发布Player，Bot/代理/数据库结构未修改。
- runtime commit：`db6dcbab9b4218bd3b1852a5d4a02a9b8ffd9f0e`，已推送origin/main。
- release：`sky-ui-20260930-db6dcba`；source为clean pushed commit的git archive。
- 地址：https://csdn.im/。
- VPS image：`sha256:bf72af8602bb8841cd3c644aa91cd6eeb59b6abc23b9c1c0b70f30f3122639eb`；container：`4f02e4df3069f715cc907a89383fce2d65b3074ff6cb9ca21a8f39f884833d39`。
- started：`2026-09-30T08:14:55.8968774Z`；healthy、restarts=0、单实例。
- `/root/tgvio-player/current` 指向release目录，`.release-commit`与实际revision一致。
- frontend：`['/assets/index-BBB6c8JW.css', '/assets/index-DigYDOg2.js']`；manifest `c0f69d266cc34c3e4787d23c42dad0f3d2825cc2819b74aed9b403bd20286071`。
- backend manifest `ab981063532d70156f3b11e2eb75e79506bd9e9e05b12a672bc4c5a1f0bb790b`，与上一运行版一致。

## 测试与生产后验

本地自动门禁与browser-act验收见 `R2-21_PLAYER_SKY_UI_ACCEPTANCE.md`：前端129/129，Python919/919，Chrome布局85/85/85/84，TS/Vite/architecture/tree/diff通过。

| 线上检查 | 结果 |
|---|---|
| HTTPS root / health | 200 / 200 |
| 未登录Feed | 401 |
| 容器内私有登录 / 认证Feed | 200 / 200 |
| 真实媒体Range 0–1023 | 206，1024 bytes，Content-Range匹配 |
| JS/CSS | 200，下发资源与镜像内容SHA256一致 |
| SQLite | quick_check=ok，schema/ledger/user_version未变 |
| migration副本演练 | --network none，schema/ledger/表计数完全一致 |
| 发布窗口日志 | ERROR=0、CRITICAL=0、Traceback=0、structured5xx=0 |
| Bot | id和started_at与本次实时preflight一致，未重建/未重启 |

发布后独立browser-act访问生产匿名页：SKY登录卡片实际渲染、口令为空、390px无横向溢出；未输入生产凭据。
**生产认证后的浏览器播放、Android/iOS真机及原生Fullscreen/PiP边界仍未验收**，不能把API Range检查或本地CDP触摸称作真机通过。

## 不影响在线版本的预检调整

- VPS Python3.11.2不支持tarfile.extractall(filter=...)；仅在所有成员预先验证为安全相对路径且普通文件/目录后，使用兼容提取。最初失败发生在空source阶段，没有切换容器。
- 历史started_at因主机此前重启已失效；本次读取并固定实时健康Bot基线，切换后复核相同。不能把历史文档时间当生产现状。
- Docker Desktop/containerd的导出index ID与VPS经典image config ID不同：本地 `sha256:e17763c450bb80714ef17b10df9f497da8c34c1214f6803f1c8982c0d8d8b4aa`、VPS `sha256:bf72af8602bb8841cd3c644aa91cd6eeb59b6abc23b9c1c0b70f30f3122639eb`。所有RootFS layer摘要、源码revision/version、backend/frontend完整内容一致；只构建一次候选，没有重编译。
- 演练receipt JSON的ledger数组与SQLite tuple形态不同，改为完整canonical JSON比较；未放宽任何schema、ledger或count门禁。
- 这些均在cutover前确认，live env/DB未因预检失败修改；通过全部门禁才执行单次Player切换。

## 备份与回滚

- 原image：`sha256:2329ad19e8e7ed4f2246e84d7ea87d7cc40a811d5a1663e2d5b818a3ab9abd35`；rollback tag：`tgvio-player:rollback-pre-sky-ui-20260930-db6dcba`。
- 在线SQLite备份：`/root/tgvio-player/releases/sky-ui-20260930-db6dcba/rollback/player.sqlite3`，0600。
- 原env：`/root/tgvio-player/releases/sky-ui-20260930-db6dcba/rollback/player.env`，0600，仅保存在VPS。
- prior source：`/root/tgvio-player/releases/sky-ui-20260930-db6dcba/rollback/prior_source`。
- 回滚流程为恢复原env/image与旧Player Compose，不覆盖live DB（避免抹去发布后的真实用户数据）。本次未触发上线后回滚。
- 所有Compose/env/认证检查仅在VPS内部消费；秘密、Cookie、DTO及生产.env未下载、输出或提交。

发布记录为后续docs-only提交，GitHub HEAD可比runtime commit新；不能以报告提交号替代运行镜像revision。
