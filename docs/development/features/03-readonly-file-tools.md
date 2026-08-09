# 只读文件能力

状态：`planned`

目标里程碑：`M3`

## 当前范围

为 GeneralAgent 提供 workspace 内的 `list_tree`、`read_file`、`file_info` 和 `search_files`。用户在会话或 workspace 层授予只读根目录。

不包含写入、删除、命令执行、依赖安装和目录外访问。

## 实现重点

- WorkspaceGrant 与规范绝对路径；
- 防止 `..`、符号链接、junction、UNC 和大小写逃逸；
- 文件大小、目录深度、搜索结果和编码限制；
- ToolResult 大内容外置引用；
- 每个调用进入 Operation Ledger，但 `side_effect=none`；
- 文件在读取过程变化时返回版本信息。

## 验收标准

- Agent 可以总结 workspace 中的文本和代码；
- 无法读取授权根外文件；
- 二进制和超大文件安全拒绝或截断；
- 路径攻击测试通过；
- GUI 可以显示本次授权的根目录。

