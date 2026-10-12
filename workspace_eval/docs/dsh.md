# DeepSeek Harness (DSH)

本接入使用 DeepSeek 官方 harness，不是用 Codex/Claude Code 调 DeepSeek 模型。
有两条入口，共用同一个非交互 worker 与 Cordis 配置：

- `agentkit.build_harness("dsh", ...)`：在调用方管理的容器中执行；hardening 可用 `--agent dsh`。
- Workspace evaluation 的 `agent.harness: DeepSeekHarness`（或 `dsh`）：直接在现有严格隔离任务容器内执行，不启动第二层 Docker。

Judge、任务提示、输入物化和容器 allowlist 不变。没有复制旧实验数据、环境改造代码或私有网关代理。

## 版本与范围

固定 `deepseek-harness-sdk==0.1.0rc7` 及同版本原生 runtime。DSH 上游仍处于快速变化的预览阶段；此 PR 复用已验证的最小组合，**不声称使用最新版或代表全部 DSH 默认能力**。

配置来自 [官方 minimal agent 的固定提交](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/examples/jsonrpc-agent/minimal.cordis.yml)，原文与 MIT 许可证保存在 `src/agents/dsh_runtime/`。
原生工具是 `bash` 与 `str_replace_editor`；无自动压缩、skills 加载或 workspace_env MCP。
若启用了 workspace_env MCP，本 adapter 明确报错，不会悄悄将 env 条件当作 bare 执行。
任务的 AGENTS.md 仍由公共 runner 按现有流程加入 prompt。

当前支持 DeepSeek 原生 Chat Completions 与兼容其流式工具/推理字段的网关。不是任意 OpenAI/Anthropic 模型的通用适配器：

- `wire_api: chat_completions`（也接受 `chat` / `openai-chat`）；不是 Responses。
- base URL 精确取服务商给出的 API 根路径，runtime 追加 `/chat/completions`。
- reasoning effort：`off`、`low`、`high`、`max`，默认 `high`；不把 `medium/xhigh` 静默映射为别的档位。
- 默认上下文声明 1,000,000，最大输出 32,768；它们是配置值，不代表所有服务商实际额度。使用服务商支持的值。
- 不改 envgen 的 Codex 专有生成链、不新增 DSH judge，也不改变原来 `selftest --agent both` 的含义。

## Workspace evaluation

先按 [Workspace evaluation README](../README.md) 准备任务、Docker 与实验 YAML。
从仓库根目录重新构建镜像；旧镜像没有新 SDK，不能直接复用：

```bash
docker compose -f workspace_eval/docker/docker-compose.yaml build
```

Dockerfile 用 `uv sync --frozen --group dsh` 安装固定 SDK。SDK/runtime 位于镜像内的 Python 环境，不在会被代码挂载遮盖的目录中。

在已有实验 YAML 中替换 **agent** 块对应字段（judge 配置保持原样）：

```yaml
agent:
  harness: DeepSeekHarness
  model: deepseek-v4-pro
  model_id: deepseek-v4-pro  # 改成服务商实际模型 ID
  base_url: ${DEEPSEEK_BASE_URL}
  api_key_env: DEEPSEEK_API_KEY
  provider_type: deepseek
  auth_type: bearer
  wire_api: chat_completions
  reasoning_effort: high
  max_output_tokens: 32768
  dsh_context_tokens: 1000000
  timeout_seconds: 3600
  attempts: 1
```

凭据使用原有 env 文件/环境变量。不要在 YAML、提交或终端历史中放密钥：

```bash
read -r -p 'API base URL: ' DEEPSEEK_BASE_URL
read -r -s -p 'API key: ' DEEPSEEK_API_KEY
echo
export DEEPSEEK_BASE_URL DEEPSEEK_API_KEY
```

继续按原实验入口运行；`dsh` 会被标准化为 `DeepSeekHarness`，动态加载 `src/agents/deepseekharness.py`。
这里的权限边界是原来的严格 Docker 容器，**不是** minimal profile 内的 `danger-full-access`。
不要在含私有文件的宿主工作目录直接调用这个原生 harness 来做不可信任务。

## agentkit / hardening

重建与题目镜像匹配的 agent base；SDK 安装到 `/opt/tb-agent/dsh`，随 overlay 复制。
**源镜像与目标镜像需要相同 Python minor 版本及兼容 glibc**，因为 SDK 的 Pydantic 包含本地扩展。
Workspace 的 bookworm 基座与 Terminal-Bench 基座不可随意混用。

```python
from agentkit import build_harness, resolve_gateway

gateway = resolve_gateway(
    protocol="openai-chat", base_url=base_url, api_key=api_key,
    model=model_id, reasoning_effort="high",
)
# runtime 是按原流程创建/隔离的一次性 DockerRuntime。
result = await build_harness("dsh", gateway=gateway, timeout=3600).run(runtime, prompt)
```

SDK worker/配置随本次运行上传，不依赖宿主全局 DSH 配置。`dsh.log` 随 hardening 日志回收。
`extra_cli_args` 不适用于 SDK；提供时明确报错。

## 验证（无需模型凭据）

仓库根目录执行；`uv` 可参考项目已有安装说明。`--frozen` 使用已提交的锁文件，不改源地址或版本：

```bash
uv sync --project workspace_eval --frozen --group test --group dsh
uv run --project workspace_eval --frozen --no-sync python -m pytest -q \
  workspace_eval/tests/test_dsh.py workspace_eval/tests/test_run_experiment.py agentkit/tests
DSH_TEST_RUNTIME=1 uv run --project workspace_eval --frozen --no-sync python -m pytest -q \
  workspace_eval/tests/test_dsh.py
```

第一条测试命令默认跳过需要 runtime/Docker 的测试；第二条启动真实 SDK 与**本机模拟 HTTP 接口**，完成工具写文件、两轮流式返回、token 统计、脱敏和超时清理。不访问付费模型。

如已构建含 SDK 的 agentkit 镜像，还可将 `DSH_TEST_IMAGE` 设为该镜像名，运行同一个 `test_dsh.py`。
额外的 Docker 测试会在一次性容器中分别验证 agentkit 与 Workspace 原生适配器，测试结束自动停止容器。
该测试使用 `network=host` 连接本机模拟接口，适用于 Linux Docker；不是正式实验网络配置。

## 结果与错误

保持 runner 的 `status / paths / errorMessage / trace / metrics / durationMs` 契约。
`raw/dsh-events.jsonl` 逐条写出事件，超时也保留已完成的轨迹；`raw/stderr.txt` 保存启动错误。
没有 usage 时记为 `null`，不伪造为零消耗。没有最终成功事件、非零退出、截断或取消均不能记为 `ok`。

SDK rc7 的 notification wait 没有完整总时限；因此由父进程执行 wall-clock timeout，先触发 SDK 清理，再终止残留进程组。
原来的严格容器生命周期仍是最后一道隔离/清理边界。诊断记录应当按私有实验日志管理：任务输入与输出本身可能包含敏感内容，密钥脱敏不等于全部内容匿名化。

本地模拟接口通过只能证明接线与工具执行正常，不能证明某个真实服务商/模型一定兼容。正式实验前请用目标端点做一次小任务验证；不要把基础设施错误当成模型零分。
