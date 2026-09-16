# llm-apim-proxy

**APIM 直连 Foundry，真实 Azure Monitor 告警驱动后续请求切换。** 不使用模型执行器，不在当前请求中重试或换后端。

业务：`客户端 → APIM → Foundry`

控制：`APIM GatewayLogs → Azure Monitor → Action Group → Logic App 直接探测备用 Foundry → ETag 更新 APIM 路由 → 新请求转向备用`

完整说明见 [面向客户的中文方案介绍](docs/monitoring-failover.zh-CN.md)、[部署手册](deployment/README.md)及[控制器操作手册](deployment/monitoring/README.md)。中文方案包含业务定位、接入边界、最新真实切换实测和未验收目标。

对外使用 **网关根路径通配反向代理**：GET／POST／PUT／PATCH／DELETE／HEAD／OPTIONS 的任意路径直接转发，不再逐个登记接口或限定 `/openai/deployments/...`。普通请求只替换目标主机，路径、业务查询参数、body 和 SSE 响应透传。客户端用 `api-key` 头携带 APIM 订阅密钥。

当前上游仍是已有 Foundry 资源。路径能透传不等于上游实现该 API，也不代表 Azure／OpenAI／Anthropic 协议自动互转；`/v1/messages` 等不受上游支持的路径会返回上游错误。所有路径统一走当前主上游，没有部署名映射或 embedding 固定后端特例。East US 2 和 Sweden 的聊天部署名统一为 `gpt-5.1`，URL 或 body 均使用这个实际部署名。

另提供 `deployment/configure_proxy.py` 注册 `llm-proxy` 后端
（`https://proxy.svhw.tech`，secret Named Value 提供上游 `api-key`，
`/v1/responses` 使用 `gpt-5.1`）。**仅注册资源，不改变当前业务路由或自动切换池**；
接入边界见[部署手册](deployment/README.md#optional-registered-session-proxy-backend)。

## 组件

| 路径 | 用途 |
|---|---|
| `deployment/configure_apim.py`、`llm-policy.xml` | APIM 命名后端、托管身份、单次转发策略、GatewayLogs |
| `deployment/backends.py`、`config.json` | 校验两个上游地址；部署名仅供健康探测、日志归因和连通性调用，不用于 APIM 改写 |
| `deployment/monitoring/` | 告警、Action Group、Logic App、授权、操作命令及测试 |
| `deployment/grant-model-roles.sh` | APIM／Logic App 共享模型调用身份的资源级授权 |
| `deployment/smoke.py` | 非敏感短提示词的真实 APIM 连通性检查；不直接写路由，但负向请求可能触发告警 |
| `deployment/azure.py` | 显式选择 MCAPS 的 Azure 管理辅助 |

原 `id-svhwb107-exec` 托管身份保留并复用于 APIM 和 Logic App，复用两个当前上游的模型权限；**保留名称不代表保留执行器服务**。Logic App 的系统身份只用于 APIM 路由管理，两种用途分开。

## 离线测试

Python 3.12，无第三方依赖、不调用 Azure：

```bash
(cd deployment && python3 -m unittest test_monitoring_policy test_configure_proxy -v)
python3 -m unittest discover -s deployment/monitoring -p 'test_*.py' -v
```

`python3 deployment/smoke.py` 是在线检查，会产生真实模型请求和费用，需 Azure CLI 登录权限。**执行前须禁用告警与自动切换，并确认无在途切换**；脚本包含可计入错误率的负向 404 请求，不会自行检查这些前置条件。

## 运行边界

APIM 使用 120 秒等待响应头的转发超时，不再读取自定义业务毫秒预算；它不能承诺完整 body 的严格截止时间。移除执行器后不再提供其完整 JSON 缓冲、2MiB 限制、5500ms 总读取／1200ms 空闲控制。调用方仍需业务总截止时间。Logic App HTTP 探测也不具有该执行器预算。

仓库针对 MCAPS Developer 实验环境，不是通用 IaC 或生产 SLA。日志使用 Foundry 精确 endpoint／部署路径归属后端，不是纯模型推理监控。两次短探测不能证明备用持续容量。

09:17 UTC 的[历史闭环](deployment/monitoring/drill-20260915.md)经过旧执行器，不能作为直连后的闭环证明。最新[直连删除演练](deployment/monitoring/drill-20260916.md)已取得真实 404 告警自动切换证据：首次 404 到备用首个 200 约 7 分 25.8 秒，告警后约 21.33 秒；不是保证 SLA。截至 2026-09-16 00:08 UTC 的交付记录，Sweden 主、East US 2 备用已人工启用，version 9，两条告警及自动切换开启。本次文档更新未重新读取线上状态；仍不自动回切或恢复隔离后端。
