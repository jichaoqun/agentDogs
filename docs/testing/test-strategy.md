# V2 测试策略

## 1. 测试层次

### 单元测试

覆盖状态转换、路由、schema、预算、Policy、路径验证和纯函数。不得访问真实模型、网络或用户文件。

### Contract 测试

所有 Store、ModelGateway、Tool Handler、Agent 和 Event Sink 实现运行统一 contract suite，保证替换 adapter 不改变领域语义。

### SQLite 集成测试

使用真实临时数据库，启用与应用一致的 PRAGMA 和 migration。覆盖事务、外键、唯一约束、CAS、WAL、busy timeout 和备份恢复。

### 后端端到端测试

通过 HTTP/CLI 驱动 Session -> Run -> Graph -> Agent -> final message，默认使用 FakeModelGateway。

### UI 测试

使用真实 API 或协议级 fake backend，覆盖会话、提交、取消、重连和错误状态。关键视图进行桌面和窄窗口截图检查。

### 真实能力 Smoke Test

真实模型、真实沙箱和外部网络测试默认关闭，通过显式环境开关运行，不作为普通单元测试前置。

## 2. 测试数据原则

- 每个测试使用独立临时目录和数据库；
- 不读取开发者真实 home、workspace 或凭据；
- 时间、ID、模型和事件发布器可注入；
- 并发测试使用 barrier 控制竞争时机；
- 故障测试使用明确 failpoint，不依赖随机 sleep。

## 3. CI 分组

```text
fast        单元与纯 contract
store       migration 和 SQLite
integration 后端闭环
ui          浏览器或桌面交互
failure     崩溃与恢复
security    路径和沙箱
smoke       外部能力，手动或定时
```

## 4. 合并门槛

- 当前里程碑 fast/store/integration 全部通过；
- schema 改动包含 migration 和升级测试；
- 状态机改动包含非法转换测试；
- 副作用改动包含幂等或 unknown 测试；
- 用户可见改动包含验收场景或 UI 测试；
- 不允许通过跳过失败测试合并。

