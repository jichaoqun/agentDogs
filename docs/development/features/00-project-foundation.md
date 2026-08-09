# 工程骨架

状态：`planned`

目标里程碑：`M0`

## 1. 用户目标

开发人员可以在全新环境中安装依赖、创建数据库、启动后端健康检查并运行测试，为后续功能提供可重复的工程基础。

## 2. 当前阶段范围

### 包含

- Python 后端包和模块目录；
- 依赖与 lockfile；
- 配置加载及开发/测试环境隔离；
- 领域 ID、时间和基础错误模型；
- SQLite connection factory 与 migration runner；
- API health endpoint；
- 结构化日志；
- 单元测试和 SQLite 集成测试入口。

### 不包含

- Session 业务接口；
- Graph、Agent 和模型调用；
- GUI；
- 文件或命令工具。

## 3. 架构约束

- [总体架构](../../architecture/overview.md)
- [SQLite Runtime Store](../../architecture/persistence-sqlite-runtime-store.md)
- [ADR-001：SQLite](../../decisions/ADR-001-use-sqlite-runtime-store.md)

建议初始模块边界：

```text
agentdogs/
  api/
  application/
  domain/
  graph/
  coordination/
  capabilities/
  persistence/
  observability/
tests/
```

## 4. 数据模型与存储

M0 只创建 `schema_migrations`、`runtime_settings`，并准备后续 migration。连接默认启用 foreign keys、WAL、busy timeout。

## 5. 接口与事件

```text
GET /api/health
```

返回应用版本、schema version、数据库可用性，不返回本地路径和敏感配置。

稳定错误码：`CONFIG_INVALID`、`STORE_OPEN_FAILED`、`MIGRATION_FAILED`、`SCHEMA_TOO_NEW`。

## 6. 实现步骤

1. 建立包结构、依赖和测试配置。
2. 建立 Settings 与环境覆盖。
3. 实现 SQLite connection factory。
4. 实现 migration runner 和首个 migration。
5. 实现 health use case 与 API。
6. 补充本地开发命令和 CI 入口。

## 7. 测试

- 空目录首次启动创建数据库；
- migration 重复运行无变化；
- checksum 不匹配拒绝启动；
- 外键实际生效；
- 测试和开发数据库隔离；
- health 在数据库不可用时返回降级状态。

## 8. 验收标准

- 全新 checkout 可按文档启动；
- migration 可从空库执行；
- `GET /api/health` 可用；
- 默认测试不访问外网；
- runtime 数据不进入 Git；
- 业务模块不直接创建 SQLite connection。

