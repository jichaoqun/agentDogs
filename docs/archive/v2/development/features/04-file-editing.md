# 文件创建与修改

状态：`planned`

目标里程碑：`M4`

## 当前范围

交付 `create_directory`、`create_file`、`apply_patch`、`replace_file` 和 `publish_artifact`。所有内容先进入 staging，验证后原子发布。

删除、跨 workspace 写入和系统目录写入不在本阶段自动执行。

## 实现重点

- `expected_content_hash` 防止覆盖用户并发修改；
- 临时文件、fsync 策略和原子 replace；
- diff 与写入摘要；
- Operation Ledger 幂等 operation_id；
- staging/validated/published 生命周期；
- 文本、代码与结构化文档使用不同 Handler。

## 验收标准

- 能新建和 patch 文本文件；
- 用户同时修改文件时返回 `FILE_CHANGED`；
- 崩溃不会留下半写文件；
- 重试同一 operation 不重复写入；
- workspace 外写入被拒绝。

