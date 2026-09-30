# 历史材料的边界

docs/refactor-v2、docs/superpowers、docs/product为既有设计或历史证据。
其中版本、测试数量、待办和“当前”只属于记录发生时，不是现行开发/生产基线。
Git已跟踪的历史材料保留；本地未跟踪的旧设计继续忽略，不能批量git add -f发布未经审查的运维内容。

旧AI_DEVELOPMENT.md可从本轮之前的Git提交恢复。
旧根AGENTS.md未被Git跟踪；标准化前已保存到控制端私有备份目录。备份不进仓库/镜像。
当前规则从根AGENTS.md进入，运行状态必须重新只读核实。
