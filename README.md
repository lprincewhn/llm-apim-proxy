# llm-apim-proxy

Azure APIM LLM 代理验证项目：由 APIM 选择后端，执行器完整读取响应后再提交给下游，在剩余预算允许时对可恢复错误进行一次跨后端补救。

**当前是验证环境，不是生产就绪版本。** 合成 HTTP 故障验证已完成；截至 2026-09-15 的部署记录，真实 Foundry 调用因托管身份 RBAC 返回 401，监控自动摘除控制器和 Java/Search 全链路尚未完成。既有 8s/15s 指标是设计目标，不是已实现的性能承诺。

## 代码与文档

| 路径 | 内容 |
|---|---|
| `deployment/executor/` | Python/aiohttp 完整响应执行器、Dockerfile、故障注入与单元测试 |
| `deployment/configure_apim.py` | APIM API、预算、路由、订阅与诊断配置 |
| `deployment/deploy_executor.py` | 验证环境 Container App 初始部署 |
| `deployment/config.json`、`deployment/azure.py` | MCAPS 验证环境资源标识与 Azure 管理助手 |
| `deployment/routing.py` | 参考健康状态机，尚非运行中的自动控制器 |
| `deployment/validate*.py` | Azure 在线验证脚本，需授权 |
| `deployment/*results.json` | 2026-09-15 的结果快照，包含失败项 |
| `deployment/grant-required-roles.sh` | 由授权管理员执行的资源级 RBAC 命令 |
| [部署交接说明](deployment/README.md) | 资源、接口、费用、限制和安全清理范围 |
| [设计方案](docs/design.zh-CN.md) | 时延预算、路由、监控和实施方案 |
| [部署结果快照](docs/deployment-report-2026-09-15.zh-CN.md) | 当时的实际部署结果和阻塞 |

## 本地测试

使用 Python 3.12，在仓库根目录执行；不需要 Azure 凭据，也不会调用真实模型：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r deployment/executor/requirements.txt
(
  cd deployment/executor
  ../../.venv/bin/python -m unittest discover -s tests -v
)
(
  cd deployment
  ../.venv/bin/python -m unittest test_routing -v
)
```

执行器配置与启动方式见 [executor README](deployment/executor/README.md)。默认不启用故障接口；Azure 验证环境显式启用了独立、鉴权保护的合成故障入口。

## Azure 操作边界

部署脚本包含现有 MCAPS 实验资源的非秘密标识，**不是任意订阅的一键初始化模板**。先阅读部署交接说明，再执行任何写操作。

- `configure_apim.py` 会重置实验路由至 East US 2 主后端。
- `deploy_executor.py` 会生成新的执行器密钥和只读镜像凭据；不能将它视为无副作用的重复发布命令。
- `validate_extended.py` 会临时修改实验路由并恢复，不得对生产资源直接运行。
- 不提交 Azure/GitHub token、订阅调用密钥、模型 API key 或本地认证目录；实际凭据只在内存或 Azure secret 中处理。
- 不通过关闭 Foundry 本地认证限制来绕过缺失的 RBAC。

本仓库私有，包含指定验证环境的部署上下文。设计、结果文档是历史快照；后续授权和验证结果应明确更新，不能把合成故障成功视为真实模型性能达标。
