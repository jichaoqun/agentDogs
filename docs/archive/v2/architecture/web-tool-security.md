# Web Tool 与网络安全契约

## 1. 范围

Web Capability 为 ResearchAgent 提供搜索和受控内容获取：

```text
web_search(query)
web_fetch(url)
```

它不提供通用 socket、任意协议下载、浏览器自动登录、表单提交、文件上传或本地网络访问。需要交互式浏览器时必须另立 ToolSpec 和 ADR。

## 2. 信任模型

搜索结果、网页、重定向、响应头、文件名和页面内指令全部是不可信数据。Web 内容只能作为模型上下文数据，不能修改系统 prompt、Policy、Tool permission、用户目标或执行规则。

ResearchAgent 必须区分：

- 来源中明确陈述的事实；
- Agent 的推断；
- 未验证或相互冲突的信息；
- 页面中的操作性指令或 prompt injection。

## 3. URL Policy

只允许 `https`，确有必要时由 Policy 单独允许 `http`。始终拒绝：

- `file:`、`data:`、`javascript:`、`ftp:`、`gopher:` 和自定义协议；
- URL 中的用户名、密码和控制字符；
- localhost、环回、unspecified、link-local、私网、组播和保留地址；
- 云元数据地址及其域名；
- 当前应用 API 端口、named pipe 和本地代理管理接口；
- 超出 allow/deny domain policy 的目标。

主机名校验不能只检查字符串。每次请求必须：

1. 规范化 URL 和 IDN hostname；
2. DNS 解析全部 A/AAAA 地址；
3. 拒绝任一非公网地址；
4. 连接时固定已验证地址或由安全网络代理执行同等校验；
5. TLS hostname 仍按原始规范 host 验证；
6. 每次重定向重复完整检查；
7. 限制重定向次数。

DNS rebinding 防护要求连接目标与已验证解析结果一致。代理环境下必须确认代理不会重新解析到被禁止地址。

## 4. 请求限制

```python
class WebRequestPolicy(BaseModel):
    allowed_domains: list[str]
    denied_domains: list[str]
    max_redirects: int
    connect_timeout_seconds: int
    read_timeout_seconds: int
    max_response_bytes: int
    allowed_content_types: set[str]
    user_agent: str
```

- 默认只允许 GET/HEAD；
- 不自动发送 cookie、系统凭据、Referer 或本地文件；
- 不读取浏览器 cookie store；
- 压缩响应按解压后大小计费并限制，防止压缩炸弹；
- 流式读取，达到上限立即终止；
- HTML、纯文本、JSON 和明确支持的文档类型分别解析；
- 可执行文件、脚本、未知二进制和畸形内容拒绝进入模型上下文。

## 5. Search 与 Fetch 分离

`web_search` 调用受信任 Search Provider，返回标题、URL、摘要、排名和 provider metadata，不把摘要当作已验证正文。

`web_fetch` 只获取通过 URL Policy 的具体页面。Search 结果进入 fetch 前重新执行 URL/DNS 校验，不能因为来自搜索服务就隐式可信。

## 6. 内容处理

```python
class WebSourceReference(BaseModel):
    source_id: str
    requested_url: str
    final_url: str
    title: str | None
    published_at: datetime | None
    fetched_at: datetime
    content_type: str
    content_hash: str
    excerpt: str
    blob_reference: str | None
```

处理流水线：

```text
fetch bytes
 -> content-type/magic check
 -> size limit
 -> safe parser
 -> remove active content
 -> prompt-injection labeling
 -> extract text and metadata
 -> store blob/reference
 -> bounded excerpt to Agent
```

解析 HTML 不执行 JavaScript、不加载子资源。文档解析器运行在受限进程中。页面文本使用明确的数据边界包装，并在模型 prompt 中声明不得遵循其中的工具、权限或系统指令。

## 7. 网络权限

Web Tool 需要 `network.connect` PermissionGrant，scope 至少包含 domain pattern、method 和 lifetime。系统 deny list 永远优先，不能通过 workspace grant 或用户一次审批访问本机、私网和元数据地址。

ResearchAgent 只获得 Web Tool，不获得通用 Sandbox 网络。命令沙箱联网是独立 capability 和审批。

## 8. 缓存、限流与合规

- 缓存键包含规范 URL、请求策略版本和 principal scope；
- 缓存保存 fetched_at、TTL、ETag/Last-Modified 和内容 hash；
- 敏感或认证内容默认不缓存；
- 每个 Run、domain 和 provider 有并发与速率限制；
- 遵守 provider API 条款、robots 和站点访问策略；
- 429 使用 Retry-After 和有界退避；
- 删除 Session 时按保留策略清理私人抓取内容。

## 9. Operation Ledger

Search/Fetch 是 `read_only` operation，仍进入账本用于成本、超时、取消和审计。重新获取可能得到不同内容，因此恢复时保留原结果引用，不把“可重新读取”误认为结果恒定。

## 10. 测试与验收

- IPv4/IPv6 loopback、私网、link-local、metadata 被拒绝；
- 域名解析到混合公网/私网地址时整体拒绝；
- 重定向到私网被拒绝；
- DNS rebinding 和编码 URL 绕过测试；
- 响应、解压和重定向上限生效；
- HTML 不执行脚本和子资源；
- 页面 prompt injection 不能扩大工具权限；
- Search 摘要与 Fetch 正文来源区分；
- 来源引用包含最终 URL、抓取时间和 hash；
- 默认不携带 cookie、凭据和本地数据。

