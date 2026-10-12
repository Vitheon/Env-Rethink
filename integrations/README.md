# 文件地图与事件日志：Codex / Claude Code 接入指南

本接入提供 `workspace_map`、`workspace_search`、`event_search` 三个只读 MCP
工具，以及「准备环境」「使用环境」两个 Skill。客户端启动时自动启动 stdio
服务，退出时关闭连接，无需手工启动 Docker、HTTP 服务或管理端口。

支持 Linux / macOS / WSL，Python 3.11 或 3.12。本文从 **Env-Rethink 仓库根目录**
执行。客户端使用你自己的登录或模型配置；安装、导入、连接自检不会调用模型。
生成语义地图/合成历史是独立流程，见第 4 节。

## 1. 安装接入程序

在已克隆的 Env-Rethink 根目录复制执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
export ENV_RETHINK_REPO="$PWD"
env-rethink-context --help
```

预期显示 `prepare`、`serve`、`doctor`、`client-config` 四个子命令。
不要把 `envgen-kit` 安装到这个虚拟环境：两个包都有 `workspace_env` 模块；
生成器请使用独立虚拟环境。首次检索时 tiktoken 可能联网下载分词表；离线使用前，
在可联网环境完成一次 `doctor` 并保留其 tokenizer cache。

## 2. 先跑一个不需要 API 的完整示例

示例只有两个中文文件和一条明确标记为合成的事件，用于验证接入，不代表研究实验。
`mktemp` 每次创建新目录，可以安全重复执行整段：

```bash
export ENV_RETHINK_DEMO="$(mktemp -d /tmp/env-rethink-demo.XXXXXX)"
python integrations/create_demo.py --output "$ENV_RETHINK_DEMO/example"

env-rethink-context prepare \
  --workspace "$ENV_RETHINK_DEMO/example/workspace" \
  --collection-map "$ENV_RETHINK_DEMO/example/public/map.json" \
  --member-index "$ENV_RETHINK_DEMO/example/public/members.sqlite" \
  --events "$ENV_RETHINK_DEMO/example/public/events.public.jsonl" \
  --bundle "$ENV_RETHINK_DEMO/context"

export ENV_RETHINK_BUNDLE="$ENV_RETHINK_DEMO/context"
env-rethink-context doctor
```

预期：`prepare` 输出 `status: ready`、`files: 2`、`cards: 1`、`events: 1`；
`doctor` 输出 `status: passed`、`transport: stdio`、三个工具名和 `model_calls: 0`。
自检会实际启动服务、完成 MCP 握手，并调用三个工具。

## 3. 在客户端使用示例

以下两种客户端任选一种。仍在刚才已激活虚拟环境、已导出变量的终端操作。
不要同时安装插件和第 6 节的独立 MCP，以免出现重复工具。

### Codex CLI

首次注册本地插件市场并安装：

```bash
codex plugin marketplace add "$ENV_RETHINK_REPO"
codex plugin add env-rethink@env-rethink-local
cd "$ENV_RETHINK_DEMO/example/workspace"
codex
```

这两个插件命令会更新**执行命令的用户自己的 Codex 配置**。共享服务器不要在
合作者的账号配置下执行；必要时只使用第 6 节提供的配置内容进行人工接入。
若旧版 CLI 没有 `plugin` 命令，用第 6 节的 MCP + Skill 方式。

### Claude Code

直接从本地插件目录加载，不需要注册市场：

```bash
cd "$ENV_RETHINK_DEMO/example/workspace"
claude --plugin-dir "$ENV_RETHINK_REPO/plugins/env-rethink"
```

如需持久安装，可在自己的 Claude Code 配置中执行：

```bash
claude plugin marketplace add "$ENV_RETHINK_REPO"
claude plugin install env-rethink@env-rethink-local --scope user
```

### 在对话中验证

发送：

> 使用 workspace-context skill，先查看工作区集合地图，再展开演示项目，查询报表的事件记录，最后读取报表，告诉我利润是多少，并说明日志是否是真实历史。

应看到三个 MCP 工具调用，找到 `交付说明.txt`、`报表.txt`，回答利润为 40，
并说明这是合成演示记录。完成这一步需要客户端的正常模型访问权限。
客户端可能给工具名添加插件前缀，这是正常现象。

每次重新打开终端都需要先激活 venv，并设置 `ENV_RETHINK_BUNDLE` 后再启动客户端。
从桌面图标启动的客户端未必继承这两个设置，建议使用第 6 节的绝对路径配置。

## 4. 换成真实工作区

先确认使用的**确实是 agent 可见的文件目录**，不是包含 metadata/rubric 的整个任务目录。
准备以下三个经过审查的公开产物：

| 参数 | 来源与格式 |
|---|---|
| `--collection-map` | `workspace-bench.workspace-collection-set.v3` JSON |
| `--member-index` | 与该地图配套的 SQLite 成员索引 |
| `--events` | 公开事件 JSONL；不能使用 `canonical.private.jsonl` |

现有 envgen 全覆盖地图（配置 `schema_version: 2`）通常输出
`final/workspace-collection-set.public.json` 和
`final/workspace-collection-map.members.sqlite`。配置版本 2 与地图产物版本 3
不是同一个版本号。历史生成使用最终接受的 `final/events.public.jsonl`。
若任务已有 `services/workspace-env/fixture.json`，按其中 `artifacts` 的文件名
在 `blobs/` 下取这三个文件即可，不要复制整个私有生成目录。

将下面路径换成你的实际路径，然后执行：

```bash
export ENV_RETHINK_WORKSPACE="/absolute/path/to/agent-workspace"
export ENV_RETHINK_MAP="/absolute/path/to/final/workspace-collection-set.public.json"
export ENV_RETHINK_INDEX="/absolute/path/to/final/workspace-collection-map.members.sqlite"
export ENV_RETHINK_EVENTS="/absolute/path/to/final/events.public.jsonl"
export ENV_RETHINK_BUNDLE="/absolute/path/to/context-bundles/run-001"

env-rethink-context prepare \
  --workspace "$ENV_RETHINK_WORKSPACE" \
  --collection-map "$ENV_RETHINK_MAP" \
  --member-index "$ENV_RETHINK_INDEX" \
  --events "$ENV_RETHINK_EVENTS" \
  --bundle "$ENV_RETHINK_BUNDLE"
env-rethink-context doctor
cd "$ENV_RETHINK_WORKSPACE"
```

然后启动已安装插件的 Codex 或 Claude Code。bundle 必须位于 workspace 外部，
且必须是尚不存在的新目录。原文件不会被修改或复制成另一套 full workspace；
导入仅复制公开地图/事件、重建检索索引。每个服务进程拥有独立的查询状态和审计目录。

### 尚未生成地图和日志

使用插件中的 `prepare-workspace-context` Skill，或按
[envgen 指南](../envgen/README.md)配置原有生成器。语义地图和合成历史需要可用的
模型连接、runner 和匹配的运行时镜像。新 Skill 会引导这些步骤，不会替你虚构历史。

生成器配置仍存在 Codex CLI `0.144.5` 的版本约束；共享 agent 镜像默认版本可能不同。
按 [runner 契约](../envgen/docs/RUNNER_CONTRACT.md)匹配配置和镜像后再运行。
本接入的三个只读工具不要求你的日常 Codex 客户端使用该旧版本。

只需要元数据快照日志时，可以在生成器的独立虚拟环境运行
`envgen/scripts/generate_workspace_event_log.py`。它不需要模型，但不能替代真实历史
或提供推断出的审批链。目录地图、语义地图、合成历史、真实 trace 历史应准确区分。

## 5. 日常使用与更新

常用请求：

- “用地图找出销售相关集合，然后查看里面有哪些文件。”
- “查一下这份文件的公开历史，再核对哪个版本适用于当前任务。”
- “帮我检查当前工作区的 Env-Rethink 接入。”

文件改变后，重新生成相关公开产物，用新 bundle 目录重新 `prepare`、`doctor`，
修改 `ENV_RETHINK_BUNDLE` 并重启客户端。服务不会自动监听工作区或推断新历史。

bundle 结构：

```text
context-bundles/run-001/
├── connection.json        # 绑定的 workspace 与历史来源类型
├── fixture.json           # 供现有 runtime 使用的产物描述
├── blobs/                 # 公开地图、成员索引、公开日志
└── sessions/              # 每次启动的独立状态及私有查询审计
```

`sessions/` 会随会话增长；确认相关客户端退出后可以按会话目录清理。它们不属于
工作区文件，也不会通过 MCP 工具提供。stdio 工具没有网络监听端口。

## 6. 不使用插件市场：MCP + 项目 Skill

接入程序可以打印带**绝对 Python 路径和 bundle 路径**的命令，适合不继承终端
环境的客户端，也适合已有自定义 MCP 配置的用户：

```bash
env-rethink-context client-config --client codex
# 或
env-rethink-context client-config --client claude
```

复制输出的 `command` 值执行即可；命令本身已经正确引用空格和中文路径。
输出中的 `mcpServers` 也可供其他支持 stdio 的客户端使用。该子命令仅打印配置，
不修改任何用户配置。Codex 的 `mcp add` 使用当前用户的配置；Claude 命令使用
当前项目的 local scope。若已有同名 `env_rethink` 服务，先检查旧配置，不要覆盖。

在工作区安装使用 Skill（不存在时才复制）：

```bash
# Codex
mkdir -p .agents/skills
test -e .agents/skills/workspace-context || \
  cp -R "$ENV_RETHINK_REPO/plugins/env-rethink/skills/workspace-context" .agents/skills/

# Claude Code；只使用 Codex 的话不需要这一段
mkdir -p .claude/skills
test -e .claude/skills/workspace-context || \
  cp -R "$ENV_RETHINK_REPO/plugins/env-rethink/skills/workspace-context" .claude/skills/
```

项目 Skill 只提供使用指引。现有 `AGENTS.md` / `CLAUDE.md` 无需覆盖。
复制 Skill 或安装插件后重新开一个会话。

## 7. 常见问题

| 现象 | 处理 |
|---|---|
| `env-rethink-context: command not found` | 激活安装接入程序的 venv；桌面启动改用第 6 节 |
| `Set ENV_RETHINK_BUNDLE` | 在启动客户端前设置变量，或用带 `--bundle` 的配置 |
| bundle 已存在 | 使用新目录；刷新完成前保留旧 bundle |
| map member missing | workspace 路径或相对目录结构不匹配；确认后重新导入 |
| different generations | 地图和索引不是同一批产物；重新选择配套文件 |
| 非 v3 地图验证失败 | 使用全覆盖生成流程的最终 v3 产物，不要手工修改格式号 |
| PUBLIC event 验证失败 | 误用了私有/旧格式/不完整日志，改用最终公开事件文件 |
| 首次 tokenizer 下载失败 | 联网完成一次 doctor，或事先配置可用的 tiktoken cache |
| doctor 成功但 agent 不调用 | 检查插件/Skill 是否加载；明确要求使用 workspace-context |

自动检查包括 JSON/schema、路径、成员数量、地图与索引配对以及 MCP 调用。
它不能判断合成历史是否真实，也不能证明文本没有语义层面的答案泄露；研究产物
仍应通过原有审查和 artifact gate。与任务 agent 同用户运行的本地插件并非容器
安全边界，benchmark 继续使用严格隔离的 runner。

## 8. 维护与测试

```bash
python -m pip install -e '.[test]'
python -m pytest integrations/tests -q
```

若两个客户端已安装，可使用临时配置目录检查原生安装与连接，不修改实际账号：

```bash
python integrations/validate_clients.py \
  --codex codex --claude claude --bundle "$ENV_RETHINK_BUNDLE"
```

只装了一个客户端时省略另一个参数。Codex 通过 app-server 枚举已加载的插件工具；
Claude 通过 `mcp list` 检查连接。此过程不创建模型会话。已验证的客户端版本为
Codex CLI 0.154.0、Claude Code 2.1.187。

`.mcp.json` 中的 `env_vars` 用于 Codex 显式继承 `ENV_RETHINK_BUNDLE`；
Claude Code 会继承启动环境。不要将其改成 `env: {"ENV_RETHINK_BUNDLE":
"${ENV_RETHINK_BUNDLE}"}`：已测试的 Codex 版本会把该值当成字面路径。

插件清单在 `plugins/env-rethink/`，Codex/Claude 的市场清单分别在
`.agents/plugins/marketplace.json` 和 `.claude-plugin/marketplace.json`。
共享工具实现仍来自 `workspace_eval/src/workspace_env/`；本次没有重写搜索逻辑，
也没有为日常客户端启用实验专用的强制 hook。

原生插件规范参考：[Codex](https://developers.openai.com/plugins/build/plugins)、
[Claude Code](https://code.claude.com/docs/en/plugins-reference)。
