# APIM 直连 Foundry：真实告警驱动后续请求切换

## 架构

业务路径：`客户端 → APIM → Foundry`。

控制路径：`APIM GatewayLogs → Log Analytics → Azure Monitor 告警 → Action Group → Logic App 直接探测备用 Foundry → ETag 更新 APIM Named Value → 配置传播 → 新请求转向备用`。

**没有模型执行器。** APIM 只转发一次，不在当前请求内重试或切换；Logic App 不通过 APIM 当前主路由探测备用，而是直接调用固定的备用 Foundry 部署。调用方不选区，但仍负责业务总截止时间及降级。已经输出的响应不会被替换。

## APIM 直连与鉴权

APIM 用命名 Backend `llm-eastus2`、`llm-sweden`、`llm-embedding` 表示三个部署所在资源，按 `chat-route.primary` 选择聊天后端；embedding 独立，不参与聊天切换。

对外使用根路径通配代理：GET／POST／PUT／PATCH／DELETE／HEAD／OPTIONS 的任意路径均转发，不再枚举接口，也不限定 Azure URL 格式。一般请求仅切换目标主机，原始路径、body、业务查询参数保留；不读取 JSON、不删除或改写 `model`。模型参数合法性由上游判断，原始错误状态和正文返回客户端；支持 SSE，不缓冲响应。

保留三个精确 POST 入口作为兼容例外：现有 Azure 聊天部署 `gpt-5.1` 和 `svhwb107-gpt51` 都遵循同一 `chat-route`，映射目标部署名；`text-embedding-3-small` 的 Azure embeddings 路径固定到 West US 3。其余路径（包括 `/openai/v1/responses`、`/v1/chat/completions`、`/v1/messages`）不改写，直接送当前主后端。

**地址可透传，不等于后端具备所有协议。** 当前可信后端仍为已有 Foundry，未配置第三方供应商；不支持的接口由后端返回 404／405。不同供应商需要另配可信地址、鉴权、健康探测和监控，不能让调用方提供任意目标 URL。非 JSON／multipart body 同样不做策略解析；不含 CONNECT 隧道、WebSocket 或 gRPC 接入。

通配 v1 请求的 body `model` 必须是当前后端实际部署名；不会自动跨供应商／区域转换。通用切换要求备用具有兼容模型名及接口；文件、任务和 response ID 等状态不自动复制。订阅密钥持有者现在可调用模型身份有权限的全部上游数据面接口（包括写入／删除），不是仅限聊天。此 APIM 上更具体前缀的其他 API 仍优先于根通配接口。

客户端使用标准 `api-key` 头，值为 APIM 订阅密钥（不是 Foundry Key）。APIM 校验后移除该密钥及客户端 Authorization；`subscription-key` 查询参数也不转发后端。模型鉴权继续由托管身份完成。

APIM 与 Logic App 都绑定现有用户分配托管身份 `id-svhwb107-exec`，复用其三个资源范围的 `Cognitive Services OpenAI User`。**这是沿用历史身份名称，并非保留执行器。** APIM 通过客户端 ID 选择身份；Logic App HTTP 通过身份资源 ID 选择，token audience 为 Cognitive Services。

Logic App 系统身份另有单一 `chat-route` 范围的 APIM 管理权限，用 ARM audience 读写路由。模型权限与管理权限用途不同，不把部署人员凭据放进策略或工作流，不使用模型 API Key。

## 分后端日志与告警规则

所有转发仍记录 APIM 自身的 `ApiManagementGatewayLogs`。聊天切换告警限制 `_ResourceId`、`ApiId=llm`、`Method=POST`，按真实 `BackendUrl` 精确匹配两地部署聊天路径以及 `/openai/v1/chat/completions`、`/openai/v1/responses`，忽略查询参数后映射为 East US 2／Sweden。任意文件／任务路径、旧 `/execute/*`、embedding 不进入聊天告警，避免把其他 API 故障当作聊天故障。

映射由 `deployment/config.json` 和 `backends.py` 共享，避免 APIM、探测和 KQL 的后端定义漂移。APIM 命名 Backend 也会记录 `BackendId`，但告警不依赖它必定有值；`Region` 不是 Foundry 后端区域。

| 信号 | 初始规则 |
|---|---|
| 后端时延 | 5 分钟内至少 5 次请求，平均 `BackendTime` ≥3200ms |
| 错误 | 5 分钟内至少 5 次请求，429 或 500–599 错误比例 ≥20% |

每分钟评估，按 Backend 分维度；`BackendResponseCode` 是服务端返回，`ResponseCode` 是网关最终返回，两者与网关错误需结合分析。连接／转发超时可能没有后端状态码。APIM 后端时延不是纯推理时长，也不是完整 body 读取超时保证。

任一状态码为 401／403 的请求从两条健康告警的样本中排除，避免权限配置错误触发切换；这类错误需另行监控处理。错误率统计网关或后端任一字段的 429／500–599。

只统计已有流量，样本少不证明健康。日志保留采样和故障信息，不记录提示词、答案、向量或鉴权头。日志入库／评估／调用／配置传播会产生异步延迟；不是实时请求 deadline。

## Logic App 决策和更新

1. 校验 common alert schema、规则名、workspace 和单个 Backend 维度。只有 `Fired` 可切换；`Resolved` 不回切。拒绝超过 10 分钟或未来时间的事件。
2. 串行执行，用系统身份读取 `chat-route` 和 ETag；只有告警指向当前 primary、备用在 enabled 中且 10 分钟冷却结束，才进入自动探测。
3. 对固定备用 Foundry 地址连续发送两次真实 GPT 请求：`Reply only OK`、32 token 上限、`reasoning_effort=none`、非流式、HTTP 不重试。
4. 两次均须 HTTP 200、唯一 choice、内容 `OK`、`finish_reason=stop`，才允许继续。探测不使用 `/health` 或合成成功响应。
5. 再确认告警未过期，使用 `If-Match` 条件更新：primary 改备用、enabled 只保留备用、version 加 1，保留其他字段并记入切换时间及原因。
6. 写入及控制面回读须成功。遇到 ETag 冲突或探测／权限失败不自动补救；写后出错不等于回滚，必须检查实际路由。
7. 更新后用**新的真实 APIM 请求**验证网关传播。当前失败请求不重新发送；不自动回切／恢复被隔离后端。

显式直连健康检查命令可在告警关闭时探测固定后端，仅验证调用能力，不读写路由，也不会重新启用被隔离后端。自动事件路径仍须遵守 enabled 和冷却约束。

## 移除执行器后的超时边界

不再提供执行器原有的完整 body 缓冲、JSON 完整性校验、2MiB 限制、5500ms 总读取／1200ms 空闲超时或容器并发限制。

APIM 单次 `forward-request timeout=120` 主要约束等待响应头，不保证完整 body 截止。不再有 intent／rewrite／generate／embedding 分类预算，也不再解释 `X-Remaining-Budget-Ms`；旧 embedding 一秒转发限制已取消。原始请求和响应不做策略缓冲，流式输出开始后不会改写或换后端。

Logic App 的同步 HTTP 网络传输受平台上限约束（Consumption 可能达 120s），没有执行器细粒度预算。延长等待不能被误判为健康；客户端仍需整个业务回合的超时和取消逻辑。8s/15s 目标没有因此获得保证。

## 安全运维及状态

Action Group 的 Logic App SAS 回调 URL 必须保密；payload 白名单不代替鉴权。安全输入／输出历史配置继续保留，不把 bearer token 或响应正文存入交付记录。

控制器每次部署默认关闭告警和写路由开关。迁移顺序是：停告警／检查在途运行 → 配好直连身份及 APIM → 更新工作流与规则 → 验证真实模型和真实直连日志 → 删除不再被调用的执行器组件并复测 → 恢复告警。

沿用演练后的路由 `primary=sweden`、`enabled=[sweden]`、`version=2`，不自动恢复 East US 2。因此没有已启用备用时，即使探测成功也不能自动切回。人工恢复应先确认健康，再带 ETag 修改 enabled，保持现有 primary。

旧[09:17 闭环记录](../deployment/monitoring/drill-20260915.md)经过执行器，只作为历史证据；直连迁移后以[迁移记录](../deployment/monitoring/direct-migration-20260915.md)为准。完整直接拓扑的再次告警演练、时延告警单独触发、错误归因完善、N-1 容量和 Java/Search 8s/15s 仍须分别验收。
