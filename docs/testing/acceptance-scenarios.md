# 验收场景

## M1：后端最小闭环

```gherkin
Given 一个新建 Session
When 用户提交一条消息
Then 系统创建唯一 Run
And 保存原始用户消息
And 调用 GeneralAgent
And 原子保存最终 Assistant 消息
And Session 返回可接受下一条消息的状态
```

```gherkin
Given 模型调用超时
When 重试预算耗尽
Then Run 进入 failed 或明确 partial 终态
And Session 不会永久显示 running
And 用户消息仍可在重启后读取
```

## M2：最小桌面交互

```gherkin
Given 用户提交消息后刷新页面
When UI 重新加载 Session
Then 已提交消息仍然存在
And UI 恢复 active Run 状态与事件 cursor
```

## M4：文件修改

```gherkin
Given Agent 读取文件版本 A
And 用户随后把文件修改为版本 B
When Agent 按版本 A 提交 patch
Then 系统返回 FILE_CHANGED
And 不覆盖版本 B
```

## M6：审批

```gherkin
Given 用户批准 operation 的参数 hash A
When 执行前参数变为 hash B
Then ApprovalGrant 无效
And Tool Handler 不会被调用
```

## M7：并行任务

```gherkin
Given Task A 和 Task B 无依赖且 Task C 依赖二者
When Scheduler 并发执行 A 和 B
Then C 只在 JoinPolicy 满足后启动
And 三个 Task 的消息、预算和工具账本相互隔离
```

