# llm-apim-proxy

**APIM 直连 Foundry，真实 Azure Monitor 告警驱动后续请求切换。** 不使用模型执行器，不在当前请求中重试或换后端。

业务：`客户端 → APIM → Foundry`

控制：`APIM GatewayLogs → Azure Monitor → Action Group → Logic App 直接探测备用 Foundry → ETag 更新 APIM 路由 → 新请求转向备用`

完整说明见 [中文方案](docs/monitoring-failover.zh-CN.md)、[部署手册](deployment/README.md)及[控制器操作手册](deployment/monitoring/README.md)。

对外使用 **Azure OpenAI 原生路径** `/openai/deployments/{deployment}/chat/completions` 和 `/embeddings`，不再提供 `/llm/intent`、`/rewrite`、`/generate` 等自定义业务接口。原始请求 body、调用方 `api-version` 及 SSE 响应透传；客户端使用 `api-key` 头携带 APIM 订阅密钥。两地聊天部署名称不同，仅在网关内映射目标部署名，不改写 body。

## 组件

| 路径 | 用途 |
|---|---|
| `deployment/configure_apim.py`、`llm-policy.xml` | APIM 命名后端、托管身份、单次转发策略、GatewayLogs |
| `deployment/backends.py`、`config.json` | 校验并共享 Foundry endpoint／部署映射，供 APIM、探测和 KQL 使用 |
| `deployment/monitoring/` | 告警、Action Group、Logic App、授权、操作命令及测试 |
| `deployment/grant-model-roles.sh` | APIM／Logic App 共享模型调用身份的资源级授权 |
| `deployment/smoke.py` | 非敏感短提示词的真实 APIM 连通性检查，不改路由 |
| `deployment/azure.py` | 显式选择 MCAPS 的 Azure 管理辅助 |

原 `id-svhwb107-exec` 托管身份保留并复用于 APIM 和 Logic App，已有三个模型资源的调用权限；**保留名称不代表保留执行器服务**。Logic App 的系统身份只用于 APIM 路由管理，两种用途分开。

## 离线测试

Python 3.12，无第三方依赖、不调用 Azure：

```bash
(cd deployment && python3 -m unittest test_monitoring_policy -v)
python3 -m unittest discover -s deployment/monitoring -p 'test_*.py' -v
```

`python3 deployment/smoke.py` 是在线检查，会产生真实模型请求和费用，需 Azure CLI 登录权限。

## 运行边界

APIM 使用 120 秒等待响应头的转发超时，不再读取自定义业务毫秒预算；它不能承诺完整 body 的严格截止时间。移除执行器后不再提供其完整 JSON 缓冲、2MiB 限制、5500ms 总读取／1200ms 空闲控制。调用方仍需业务总截止时间。Logic App HTTP 探测也不具有该执行器预算。

仓库针对 MCAPS Developer 实验环境，不是通用 IaC 或生产 SLA。日志使用 Foundry 精确 endpoint／部署路径归属后端，不是纯模型推理监控。两次短探测不能证明备用持续容量。

09:17 UTC 的[历史闭环](deployment/monitoring/drill-20260915.md)经过旧执行器，不能作为直连后的闭环证明。直连迁移结果见[迁移记录](deployment/monitoring/direct-migration-20260915.md)。当前路由沿用 Sweden 主、East US 2 隔离；不自动回切，不自动恢复被隔离备用。
