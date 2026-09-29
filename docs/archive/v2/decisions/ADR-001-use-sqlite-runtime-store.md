# ADR-001：使用 SQLite3 作为 Runtime Store

状态：`accepted`

## 背景

桌面应用需要保存原始对话和系统运行状态，并支持事务、崩溃恢复、幂等 Run、checkpoint 和工具账本。部署独立数据库服务会显著增加安装和运维成本。

## 决策

从首个可运行版本开始使用 SQLite3 作为唯一权威 Runtime Store。应用运行不提供 InMemory Store 模式；内存实现只用于有限单元测试。

使用 WAL、foreign keys、短写事务、revision CAS 和 migration。大型 blob 与 artifact 外置保存。

## 后果

优点：零服务部署、事务可靠、便于备份，符合桌面应用形态。

约束：SQLite 同时只有一个 Writer。外部调用不能发生在事务中，高并发结果必须通过短事务提交。

## 参考

- [SQLite Runtime Store](../architecture/persistence-sqlite-runtime-store.md)

