# llm-apim-proxy

**真实 Azure Monitor 告警驱动 APIM 后续请求切换**，不是请求内重试。

APIM 记录后端时延／错误，Azure Monitor 告警经 Action Group 调用 Logic App；控制器确认备用模型健康后，通过 APIM 管理 API 更新路由。配置传播至网关后，**新请求**才转向备用后端。当前失败请求不会自动重投，也不会自动回切。

## 方案与运行状态

完整设计见 [告警驱动切换方案](docs/monitoring-failover.zh-CN.md)，部署与运维见 [部署说明](deployment/README.md) 和 [控制器操作手册](deployment/monitoring/README.md)。

2026-09-15 09:17 UTC，实验环境已完成真实错误告警 → 两次真实 Sweden GPT 探测 → ETag 路由更新 → 新请求 Sweden `200 / OK` 的闭环。[演练记录](deployment/monitoring/drill-20260915.md)及[原始脱敏结果](deployment/monitoring/drill-20260915.json)保留在仓库。

演练结束时：两个告警启用，`switchEnabled=true`；`primary=sweden`、`enabled=[sweden]`、`version=2`。East US 2 因演练被隔离，需人工确认健康后重新启用，**当前没有已启用备用**。这是时间点记录，不代替在线状态查询。

## 仓库范围

| 路径 | 用途 |
|---|---|
| `deployment/monitoring/` | 告警、Action Group、Logic App、管理权限脚本、控制器测试及真实闭环记录 |
| `deployment/configure_apim.py`、`llm-policy.xml` | 只转发一次的业务 API 和不记录正文的诊断配置 |
| `deployment/executor/` | APIM 调用和控制器探测实际依赖的模型执行器；不选备用、不改路由、不重试 |
| `deployment/deploy_executor.py`、`config.json`、`azure.py` | 实验执行器部署、后端白名单及 Azure 管理辅助 |
| `deployment/grant-required-roles.sh` | 执行器访问三个现有模型资源的最小范围授权 |
| `docs/monitoring-failover.zh-CN.md` | 当前唯一主方案 |

旧的请求内补救演示、独立模拟路由状态机、手动切路验证脚本及过时报告已移除。合成上游只作为执行器的**本地测试夹具**保留，不作为线上服务能力或闭环证据。`lab-validation` 是为兼容已部署调用方保留的 APIM 订阅 ID，不是演示 API。

## 本地验证

Python 3.12，在仓库根目录：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r deployment/executor/requirements.txt
(cd deployment/executor && ../../.venv/bin/python -m unittest discover -s tests -v)
(cd deployment && ../.venv/bin/python -m unittest test_monitoring_policy -v)
python3 -m unittest discover -s deployment/monitoring -p 'test_*.py' -v
```

这些命令不调用 Azure 或真实模型。部署脚本会改变云端资源，必须先读操作手册；凭据、回调 URL 和本地认证目录不得提交。

## 边界

本仓库针对 MCAPS Developer 规格实验环境，不是通用一键 IaC 或生产可用性承诺。已通过的是**受控超时引发的真实错误告警闭环**，不是自然发生的 Foundry 区域故障。时延告警独立触发、备用持续容量、Java/Search 整体业务链及 8s/15s 性能目标均未验收。
