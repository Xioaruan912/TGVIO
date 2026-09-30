# 2026-10-01 项目标准化交接

本文件保留规范阶段证据。后续已收到前端提示词，当前交接见 [前端改版记录](2026-10-01-player-redesign.md)。

## 范围

用户要求读取WSL Debian /root/TGVIO、标准化项目、约束后续AI、同步GitHub/VPS并清理垃圾。
前端视觉与交互重构等待用户提供提示词；本轮整理开发工具和规范，不改变应用行为。

## 初始事实

- Git main与origin/main均为45491d8，工作区clean。
- 根AGENTS.md约342KB且未跟踪；docs整体ignore导致关键规范只存在本地。
- AI_DEVELOPMENT、根AGENTS与Player规范存在重复/过时状态与生产记录混杂。
- 基线Python993 tests通过；Node20前端197项中一项因直接导入quality.ts失败。
- main.ts1710行，large.ts770行；本轮登记只能减少的债务，不猜测新设计。
- VPS实际运行Bot5dc86a7、Player8afd107；45491d8的cover-grid镜像存在但并非当前运行容器。
- VPS下载约34GB、Player缓存约17GB属于运行数据；不可批量删除。

## 本轮实现

- 根/局部规范纳入Git，分开通用规则、服务边界、当前文档、历史与交接。
- 增加UTF-8/LF编辑约定、生成物ignore和治理检查。
- 统一前端测试编译配置，所有TS严格检查，Node测试导入JS，临时产物自动清理。
- 增加根scripts/check.sh及CI，自动核验治理文件、文档链接、秘密/路径、模块预算、Python和前端。
- 修复check_foundation临时目录退出清理，修正过时脚本/文档状态表述。
- VPS同步与精确清理另有operations证据，上传源码与运行版本分别记载。

## 验证及交付

- 完整scripts/check.sh --browser通过：Python3.13.5的999项、前端198项、TS/build、治理/架构与四种Chrome视口回归。
- Python3.11.16离线只读挂载当前源码：999项OK，1项skip；未启动Bot。
- VPS清理225,614,618字节旧依赖/测试产物及107.5MB过期构建缓存，运行身份/health/restart不变。
- GitHub提交及源码快照同步在本轮收尾执行，精确commit与receipt由Git和VPS记录；源码同步不代表runtime切换。
- 详细证据见[运维记录](../operations/2026-10-01-standardization.md)。
- 根AGENTS从342KB历史混合记录缩减为约70行长期规则，旧文件私有备份；规范不再作为执行日志。
- 前端视觉与交互重构仍等待用户提示词。
下一步：用户给出前端设计提示词后，再明确页面、交互、组件划分和验收，逐步缩小main.ts并保留播放/隐私/Range合同。
