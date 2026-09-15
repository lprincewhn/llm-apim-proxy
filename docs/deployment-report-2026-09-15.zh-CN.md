# MCAPS APIM 部署与验证结果

日期：2026-09-15。**已部署隔离验证环境并通过故障切换验证；真实模型调用受 RBAC 阻塞，尚未完成整套生产方案验收。**

## 已创建与复用

| 项目 | 结果 |
|---|---|
| 订阅 | MCAPS-Hybrid-REQ-138015-2025-wangfrancis，`10564893-ecc3-4a6d-b505-53bcbe89dd8e` |
| 新资源组 | `rg-svhwb107-apim-lab` |
| 新 APIM | `apim-svhwb107-0915`，East US 2，Developer，仅用于验证 |
| 入口 | `https://apim-svhwb107-0915.azure-api.net` |
| 完整响应执行器 | Container App `exec-svhwb107`，已运行，完整读取 body 后才返回 |
| 诊断 | 独立 Log Analytics、APIM/Container Apps 诊断、聚合时延告警规则 |
| East US 2 chat | 复用 `svhw-openai-eastus2/gpt-5.1`，未修改 |
| Sweden chat | 在现有 `svhw2-swedencentral` 内新增独立部署 `svhwb107-gpt51`，GlobalStandard capacity 10 |
| Embedding | 复用 `svhw2-westus3/text-embedding-3-small`，未修改 |

没有改动已有 `svhw-foundry` APIM，也没有修改生产 Foundry 的本地认证开关或原有模型部署。当前订阅没有发现 Canada East 的对应 Foundry 资源，因此没有宣称部署三地域等价环境。GlobalStandard 的资源位置也不能直接证明推理发生在该区域。

App Service B1 因区域额度为 0 创建失败，执行器改部署到独立 Container Apps 环境。身份使用新建的 `id-svhwb107-exec`；执行器访问密钥与镜像只读凭据保存在 Azure secret 中，不在附件中。

## 实际验证

以下网络耗时由本次运行所在机器观察，包含访问 East US 2 的网络时延，不是模型 P95，也不是知识库全链路压测。

| 场景 | 实测结果 |
|---|---|
| 正常合成响应经 APIM | HTTP 200，1 次尝试，约 1.03s |
| 上游发出 HTTP 200 头和部分 body 后停顿 | 执行器触发 body 超时，APIM 第二次尝试成功；HTTP 200，2 次尝试，约 1.74s |
| 429/500/503 | APIM 改选第二候选，2 次尝试返回 200，约 0.99–1.01s |
| 400/401/403 | 保持原状态码，不换后端，1 次尝试 |
| 持续滴流 | 执行器 2 秒总 deadline 生效，504/`total_timeout`；外部观测约 2.69s |
| 响应头一直不返回 | 执行器总 deadline 生效，504，外部观测约 2.68s |
| body 超时、截断 JSON、超大响应 | 分别返回明确 504/502；不返回部分成功正文 |
| 缺 APIM subscription key / 执行器密钥 | HTTP 401 |
| 更新 APIM 路由至 Sweden，再恢复 East US 2 | 已观察后续合成请求的候选标识切换，并完成恢复 |

两组云端检查共 **17 项通过、7 项真实模型集成检查失败**；执行器本地容器测试 40 项通过，参考路由状态机 4 项通过。JSON 原始结果附在源码包中。

**故障场景使用执行器内部真实 HTTP 合成上游，不调用 Foundry。** 其“Sweden”响应头只表示 APIM 的候选选择，不能当作 Sweden 模型推理成功的证据。配置更新验证也不是告警自动触发切换的证明。

## 明确阻塞

执行器访问三个真实模型后端均得到：

```json
{"status":401,"code":"PermissionDenied","message":"Principal does not have access to API/Operation."}
```

当前部署身份尝试授予角色时，Azure 返回 `AuthorizationFailed`，缺少 `Microsoft.Authorization/roleAssignments/write`。East US 2 现有资源禁止本地 key 认证，本次没有关闭这个安全设置。

需要有权限的 Owner/User Access Administrator，为下面的新托管身份，在三个资源上分别授予 **Cognitive Services OpenAI User**：

- 托管身份对象 ID：`8f1e45f4-0ac1-400d-b403-87ab38dac147`
- 资源：`svhw-openai-eastus2`、`svhw2-swedencentral`、`svhw2-westus3`
- 三个资源均在 MCAPS 订阅的 `jump-server_group`；只需资源级作用域，不需要整个订阅的 Owner。

源码包 `deployment/grant-required-roles.sh` 提供精确命令，但**尚未执行成功**。授权传播后应再次运行真实 chat/embedding 检查。

## 尚未完成的部分

1. 真实 GPT-5.1/embedding 成功响应、代表性输入下的 P95/P99、故障后剩余容量及质量评测。
2. 原有 Java 意图/知识库与 Azure AI Search 的整轮接入。因此不能宣布“知识库 ≤8s、意图+知识库 ≤15s”已经达标。
3. 告警到自动摘除/恢复的持久化控制器。当前只有诊断、聚合时延告警和参考状态机，**没有挂接自动改路由的 action group/定时任务**；不把它冒充为已运行的自动容灾。

APIM 原生 Requests 指标已看到本次请求样本；截至交付前，Log Analytics 的网关日志查询仍为空，因此日志入库链路也尚未作为验收通过项。

## 使用与费用

新 API 为 `POST /llm/intent`、`/llm/rewrite`、`/llm/generate`、`/llm/embedding`。密钥在新 APIM 的 Subscriptions → `SVHWB107 validation client` 获取，不在评论发送。接口限定非流式 JSON；`X-Remaining-Budget-Ms` 只允许缩短阶段预算。真实模型调用在授权前仍会失败。

资源会保留并持续产生可能的费用，包括 Developer APIM、常驻 Container App、ACR 和日志；Developer 没有生产 SLA。镜像使用的只读 ACR token 有效期 30 天，长期保留需轮换。

若不再保留验证环境，仅删除 `rg-svhwb107-apim-lab`，并单独删除新增的 `svhw2-swedencentral/svhwb107-gpt51` 部署；**不要删除已有 Foundry 资源或 `jump-server_group`**。源码和操作说明已附包，当前工作目录无 Git 远端，因此没有创建 PR。
