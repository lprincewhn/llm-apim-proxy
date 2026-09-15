# 真实告警驱动 APIM 后续请求切换

## 目标与不做的事

目标是监控当前后端 A 的运行质量，确认备用 B 健康后改变**未来请求的路由**，让应用只访问 APIM，不负责选区。

不对当前请求重试，不在一次请求里切换 endpoint，不透明续接已输出的 SSE，不自动回切。调用方仍需实施自己的总截止时间和降级；告警控制链不能替代单次请求超时控制。

## 两条路径

**业务路径：** 客户端调用 `/llm/*` → APIM 从 Named Value `chat-route` 读取主后端 → 执行器调用对应 Foundry 部署一次 → 返回完整响应或明确错误。执行器负责托管身份、固定后端白名单、完整 JSON、大小和时限约束，不负责健康路由决策。

**控制路径：** APIM `ApiManagementGatewayLogs` → Azure Monitor scheduled-query rule → Action Group → Logic App → 两次真实备用 GPT 探测 → 使用独立托管身份调用 APIM 管理 API → ETag 条件更新 `chat-route` → 配置传播 → 新请求转到 B。

保留执行器是因为当前 APIM 和 Logic App 都实际依赖它，不是保留旧的请求内补救方案。embedding 继续使用现有单独部署，不参与聊天双后端切换。

## 日志与告警

诊断采样为 100%，不收集请求／响应正文、模型凭据或客户端鉴权头。查询严格限定实验 APIM 的 `_ResourceId` 和 `ApiId == "llm"`，从 `BackendUrl` 的 `/execute/eastus2` 或 `/execute/sweden` 提取后端。

| 告警 | 初始条件 | 计算方式 |
|---|---|---|
| `llm-backend-latency` | 最近 5 分钟至少 5 次请求，平均后端时延 ≥3200ms | `avg(BackendTime)`，单位毫秒 |
| `llm-backend-errors` | 最近 5 分钟至少 5 次请求，429／500–599 比例 ≥20% | `ResponseCode` 错误数／请求数 |

每分钟评估，按 `Backend` 分维度触发。无足够样本不视为健康证据。不是 P95，也不是连接／body 空闲计时器；日志入库、评估、工作流和配置传播都会产生延迟。

本实现统计业务最终响应码，**也会计入调用方主动缩短预算造成的 504**。实验用此机制触发告警；生产应补充错误归因，避免将不合理客户端预算、容量限制等一概视为区域故障。

## 控制器判断与更新

1. 接收 Action Group 的 common alert schema；限制规则名、目标 workspace、单个 `Backend` 维度。
2. 只处理 `Fired`，拒绝超过 10 分钟的事件和未来时间；`Resolved` 不触发回切。工作流并发为 1。
3. 通过托管身份读取 `chat-route` 及 ETag。只有告警对应当前 primary、当前和备用都在 enabled 中、距上次切换至少 10 分钟，才继续。
4. 依次对备用发送两次真实 GPT 请求：`Reply only OK`、32 个输出 token 上限、`reasoning_effort=none`、非流式。两次均须返回 HTTP 200、唯一完整 choice、内容 `OK`、`finish_reason=stop`。
5. 执行器预算为总计 5500ms／空闲 1200ms；HTTP 动作禁用重试。Consumption Logic App 的网络传输上限仍可能为 120s，不能承诺工作流单动作 5.5s 内结束。
6. 再次确认事件新鲜，使用 `If-Match: <ETag>` 修改 Named Value。保留其他字段，将 primary 改成备用、enabled 改成仅备用、version 加 1，记录 `lastSwitchAt` 和 reason。
7. 条件写入冲突、探测失败、权限错误或回读不一致均不视为成功；无自动重试或自动回滚。写后失败仍需检查实际状态，不能假设已撤销。
8. 控制面回读成功后，仍需用**新的真实 APIM 请求**确认网关传播；后台配置更新不会接管当前失败请求。

当前状态契约示例（演练后）：

```json
{
  "primary": "sweden",
  "enabled": ["sweden"],
  "version": 2,
  "lastSwitchAt": "2026-09-15T09:17:39.0311001Z",
  "reason": "Azure Monitor llm-backend-errors quarantined eastus2"
}
```

被摘除后端不会自动恢复；如果只剩一个 enabled，控制器不会虚构健康备用。运维应先探测被隔离后端，再通过带 ETag 的人工配置将其重新加入 enabled，通常保持当前 primary，避免无意回切。

## 身份与安全

| 身份／入口 | 权限 |
|---|---|
| APIM 客户端 | 已有 APIM 订阅鉴权，限制速率 |
| 执行器 `id-svhwb107-exec` | 三个模型资源范围的 `Cognitive Services OpenAI User` |
| Logic App 系统身份 | 仅 `.../namedValues/chat-route` 范围的 `API Management Service Contributor` |
| Action Group → Logic App | SAS 回调地址；必须保护其读取权限，不写日志或提交仓库 |

执行器密钥作为工作流 secureString 参数；探测和管理动作使用安全输入／输出历史配置。告警内容白名单不是来源的密码学证明，持有回调 URL 者仍可调用，因此回调保密是必要前提。

首次部署及重新部署控制器都会关闭两个告警并设置 `switchEnabled=false`。管理员授权后，先验证真实 MI 读写、在禁止写路由模式下验证两次模型探测，再启用告警。具体命令见[控制器操作手册](../deployment/monitoring/README.md)。

## 已完成的闭环

2026-09-15 UTC：09:15:31–36 五次受控预算超时形成真实 APIM 504 日志；09:17:33 错误告警 Fired；09:17:39 Logic App 在两次真实 Sweden 探测通过后写入路由；09:17:49 新 APIM 请求返回 Sweden `200 / OK`，仅一次尝试。

实际演练阶段没有手动调用告警回调，也没有人工更新路由。从首个错误到路由时间戳约 2 分 8 秒，仅代表该次观察。详见[证据与限制](../deployment/monitoring/drill-20260915.md)。

## 文档与发布边界

本次仓库清理删除旧请求内补救方案和演示资源的生成代码，但**未执行云资源删除、镜像发布或在线 API 变更**。现有云端旧镜像／旧演示入口可能仍存在；不属于本方案，也不参与告警查询。实际上线清理应另行安排，不能把删文件等同于删除云资源。

仍需单独完成：时延告警触发演练、错误归因优化、N-1 持续容量、生产网络和 HA、Java/Search 业务集成以及 8s/15s 性能验收。两个短探测和一次短回答不证明这些目标。
