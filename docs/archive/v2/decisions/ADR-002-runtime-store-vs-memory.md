# ADR-002：Runtime Store 与 Memory/Knowledge 分离

状态：`accepted`

## 背景

原始会话和系统状态是可审计事实；Memory 是从事实中提取的长期信息；Knowledge 是外部内容及索引。混合保存会使删除、纠错、恢复和权限边界不清晰。

## 决策

Runtime Store 只保存原始消息和系统运行事实。当前 V2 不实现长期 Memory 自动提取和 Knowledge 索引。

未来 Memory/Knowledge 作为独立数据域，只能读取已提交事实并保存可删除的派生数据，不得修改原始消息、Run、checkpoint、Operation Ledger 和事件。

## 后果

初期能力更小，但数据语义、隐私删除和故障恢复更清晰。未来引入检索系统时需要独立 schema、权限和生命周期设计。

