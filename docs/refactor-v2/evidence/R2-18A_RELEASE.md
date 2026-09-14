# R2-18A 手机首页基础包发布证据

> 2026-09-14；DELIVERED。仅首页基础包，不代表 R2-18 全部完成。

- Full runtime commit：`c7ba056e5a6d2451a9556acdf5fb4a2b32eda04f`，已推送 origin/main。
- Release：`r2-18a-c7ba056-20260914T025757Z`。
- Runtime image：`sha256:73338e97dd2e826ad023cbf20c11aafdde19c480e521bd0fc42316bdbfec7604`。
- Git / 宿主 / 容器 source manifest：`5bde218af46f3ae8ddde713db181ae5327fafedb047f6416c33507296032a9c1`。
- Migration：none；schema v11，hash `344e92de1828579e46afe9df40c00ccc0eab10028718ab2d70b8aa7bb896ed6d`。

## 内容与门禁

新增 owner-scoped 上下文首页、六键过渡导航、历史直达、只读合集预览回调，以及 intake 导航文本防误收。原命令、旧键盘入口和旧结束合集语义保留。没有实现多草稿、风格或效果图生成。

本地与服务器 test image（`--network none` / Bot disabled）均 **409 tests 全通过**；secret scan、106 Python 文件 / 1000 行预算、compileall、composition-root check 全通过，runtime image 内容检查通过。覆盖首页状态投影/脱敏、全部导航文字不进入 caption、聚合计数、预览开关关闭时重复点击只预览不入队。

唯一发布入口执行成功后，独立 `scripts/vps_check.sh`：running / healthy、单实例、restart=0、error_markers=0、bootstrap与Telegram ready各1；APP_COMMIT / release commit / source manifest一致；SQLite quick_check=ok、jobs=0、blockers=[]、runtime lease=1。NTP 仍 no，既有问题未擅自调整。

`scripts/rollback_hostdzire.sh --check r2-18a-c7ba056-20260914T025757Z`：passed。回滚资产与正式 manifest/ledger 位于 `/root/TGVIO-releases/r2-18a-c7ba056-20260914T025757Z/` 下标准 rollback/evidence 目录。previous runtime 是 R2-15B `75e6259`；本包不改 schema，UI 回滚不得覆盖发布后新业务数据，执行前仍须经过现有副作用/活动任务门禁。

## 使用与未验收

发送一次 `/start` 更新常驻键盘；点首页查看当前状态、发布历史直达历史列表；归档/缓存/状态在更多中。页面内“首页”显示上下文内联按钮；收集中“预览发布”不会直接创建 Job。

真实 Android/iOS 布局、旧键盘会话、连续转发、历史翻页与发布全链路未做实机验收；未使用用户媒体进行破坏性测试。后续任务详见 [交接](../R2-18_HANDOFF.md)。
