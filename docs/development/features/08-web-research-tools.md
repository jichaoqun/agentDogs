# Web Research Tools

状态：`planned`

目标里程碑：`M8`

## 1. 用户目标

ResearchAgent 可以搜索公开网络、受控获取网页内容，并返回带来源、时间和不确定性的研究结果，同时不能访问本机、私网或云元数据服务。

## 2. 当前阶段范围

### 包含

- Search Provider adapter；
- `web_search` 和 `web_fetch`；
- network.connect PermissionGrant；
- URL/DNS/redirect Policy 与 SSRF 防护；
- 下载、解压、内容类型、timeout 和限流；
- HTML/文本/JSON 安全提取；
- prompt injection 数据隔离；
- WebSourceReference、blob、缓存和 Operation Ledger；
- Research 来源 contract tests。

### 不包含

- 登录网站、cookie 复用和密码填充；
- 表单提交、购买或发布内容；
- 通用浏览器自动化；
- 文件上传；
- 命令沙箱任意联网。

## 3. 架构约束

- [Web Tool 安全契约](../../architecture/web-tool-security.md)
- [Capability Runtime](../../architecture/layer-4-capability-runtime.md)
- [ADR-004 Operation Ledger](../../decisions/ADR-004-operation-ledger.md)

## 4. 实现步骤

1. 实现 URL canonicalization 与 IP 分类纯函数。
2. 实现安全 resolver、连接和逐跳重定向校验。
3. 实现流式下载、大小/时间/content-type 限制。
4. 实现 Search Provider adapter。
5. 实现安全内容解析和 WebSourceReference。
6. 接入 Policy、PermissionGrant、账本、缓存和预算。
7. 加入 ResearchAgent，并完成 prompt injection 与来源测试。

## 5. 验收标准

- SSRF 安全矩阵全部通过；
- 页面内容不能改变系统权限或调用未授权工具；
- Research 结果具有可追踪来源；
- 网络错误、429 和超时会稳定收敛；
- 私网和本地 API 永远不能通过用户普通授权开放；
- 大型正文不进入 Graph checkpoint。

