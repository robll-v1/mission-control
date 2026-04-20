# Mission Control

[![CI](https://github.com/robll-v1/mission-control/actions/workflows/ci.yml/badge.svg)](https://github.com/robll-v1/mission-control/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

> **Local-first, AI-powered code review console.**
> Use it as a Web UI, a CLI tool, or an AI agent skill (MCP).

**[English](#english)** | **[中文](#中文)**

---

<a id="english"></a>

## Quick Start

```bash
# One command — installs deps, starts backend + UI, opens browser
make start

# Or cross-platform (works on Windows too)
pip install -e .
amc start
```

The system automatically:
1. Creates a venv and installs dependencies (first run)
2. Installs frontend packages if Node.js is available
3. Finds free ports (no conflicts)
4. Starts backend API + frontend dev server
5. Opens your browser after health check passes

### Management

```bash
amc stop       # graceful shutdown (aborts active reviews)
amc status     # show running services
```

### Options

```bash
amc start --port 9000       # custom backend port
amc start --no-ui           # backend only
amc start --no-browser      # skip browser open
```

## What It Does

Mission Control automates code review by:
1. Ingesting a GitHub PR (or local git diff)
2. Compiling context from your repo (keyword extraction + file scoring)
3. Running structured review rounds through an AI agent ([OpenCode](https://github.com/opencode-ai/opencode))
4. Extracting structured findings (severity / file / line)
5. Enforcing review policy (blocks dangerous commands)

### Features

| Category | Highlights |
|----------|-----------|
| **Review** | Multi-round review, context-aware prompts, structured findings |
| **Safety** | Static review policy (blocks shell/docker/npm), preflight checks |
| **Reliability** | Graceful shutdown, crash recovery, idle timeout |
| **Output** | Verdict (clear/concerns/failed), severity ratings, exportable summaries |
| **Streaming** | Real-time SSE events for live progress |
| **Agent Skill** | MCP server, CLI, Python SDK — no browser needed |

## Prerequisites

| Tool | Required | Notes |
|------|----------|-------|
| Python 3.11+ | ✅ | Backend runtime |
| [OpenCode](https://github.com/opencode-ai/opencode) | ✅ | AI review agent |
| Node.js 18+ | Optional | Frontend UI |
| `GITHUB_TOKEN` | Recommended | Higher API rate limits |

## Architecture

```
backend/
  app/
    api/           FastAPI routes + SSE streaming
    adapters/      Agent backends (opencode)
    core/          Engine, execution, context compiler, worktree, review policy
    services/      Diff, artifact store, result extraction
    cli.py         CLI entry point (amc start/stop/status/review/mcp)
    sdk.py         Python SDK (ReviewEngine)
    mcp_server.py  MCP tool server
frontend/          React + Vite UI
runtime/           Generated: worktrees, task artifacts
data/              SQLite database (auto-created)
```

## Agent Skill

Mission Control works as an **AI agent skill** — your coding agent calls it for code review without a browser or server.

### CLI (`amc review`)

```bash
# Review local uncommitted changes
amc review

# Review a GitHub PR
amc review https://github.com/org/repo/pull/123

# JSON output + exit code for CI
amc review --format json --exit-code

# Focused review
amc review --focus "security" --rounds 2
```

Full options:
```
amc review [PR_URL] [OPTIONS]
  PR_URL              GitHub PR URL (omit for local diff)
  --repo, -r PATH     Repository path (default: cwd)
  --base, -b BRANCH   Base branch (default: auto-detect)
  --rounds N          Max rounds (default: 1)
  --format FMT        markdown | json
  --focus, -f TEXT    Review focus area
  --timeout SEC       Per-round timeout (default: 600)
  --exit-code         Exit 1 if concerns found
```

### MCP Server (`amc mcp`)

Start as stdio MCP tool server:

```bash
amc mcp
```

Agent configuration (Claude Desktop / OpenCode / etc.):

```json
{
  "mcpServers": {
    "mission-control": {
      "command": "amc",
      "args": ["mcp"],
      "cwd": "/path/to/mission-control"
    }
  }
}
```

| MCP Tool | Description |
|----------|-------------|
| `review_code` | Run code review (PR or local diff) |
| `get_review_findings` | Get findings from recent review |
| `abort_review` | Cancel running review |

### Python SDK

```python
from app.sdk import ReviewEngine

engine = ReviewEngine(language='en')
report = engine.review('/path/to/repo')

if report.passed:
    print("All clear!")
else:
    for f in report.findings:
        print(f"[{f.severity}] {f.path}:{f.line} — {f.summary}")
```

### Review Modes

| Mode | Trigger | Reviews |
|------|---------|---------|
| **PR mode** | Provide PR URL | GitHub pull request diff |
| **Local diff** | No URL | `git diff` against base branch |

Both modes produce identical structured output.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check |
| GET | `/api/tasks` | List tasks |
| POST | `/api/tasks` | Create task |
| GET | `/api/tasks/{id}` | Task detail |
| GET | `/api/tasks/{id}/stream` | SSE event stream |
| POST | `/api/tasks/{id}/start` | Start review round |
| POST | `/api/tasks/{id}/abort` | Abort review |
| POST | `/api/tasks/{id}/validate` | Run validation |
| POST | `/api/tasks/{id}/export-summary` | Export summary |
| POST | `/api/admin/shutdown` | Graceful shutdown |

## Configuration (`.amc.yaml`)

```yaml
repo:
  path: .
  base_branch: main

backend:
  default: opencode
  opencode:
    model: ''
    variant: ''

context:
  include_recent_commits: 8
  candidate_files_limit: 12

execution:
  idle_timeout_sec: 180

validation:
  default_mode: standard
  checks:
    build: { command: '' }
    test: { command: '' }
```

## Reliability

- **Preflight check**: verifies AI backend before each round; fails fast with clear error
- **Graceful shutdown**: `amc stop` → aborts active tasks → SIGTERM → fallback force-kill
- **Crash recovery**: on restart, stale `running` tasks are marked `FAILED` with audit event
- **Idle timeout**: kills unresponsive agent processes after 180s

## Cross-Platform

| Feature | macOS/Linux | Windows |
|---------|-------------|---------|
| `make start` | ✅ | ❌ |
| `amc start/stop/status` | ✅ | ✅ |
| `amc review` / `amc mcp` | ✅ | ✅ |
| Process management | SIGTERM / killpg | taskkill /T |

## Notes

- Set `GITHUB_TOKEN` or `GH_TOKEN` for GitHub API access
- Task artifacts stored in `runtime/tasks/<task-id>/`
- Worktrees auto-cleaned after review completes
- Logs: `.run/backend.log`, `.run/frontend.log`

---

<a id="中文"></a>

# Mission Control（中文文档）

> **本地优先、AI 驱动的代码审查控制台。**
> 可作为 Web UI、CLI 工具，或 AI Agent 技能（MCP）使用。

## 快速开始

```bash
# 一键启动（自动安装依赖、启动服务、打开浏览器）
make start

# 跨平台方式（Windows 也支持）
pip install -e .
amc start
```

系统自动完成：
1. 创建虚拟环境并安装依赖（首次运行）
2. 安装前端依赖（如有 Node.js）
3. 自动寻找可用端口
4. 启动后端 API + 前端开发服务器
5. 健康检查通过后打开浏览器

### 管理命令

```bash
amc stop       # 优雅停机（先中止活跃的审查任务）
amc status     # 查看运行中的服务
```

### 启动选项

```bash
amc start --port 9000       # 自定义后端端口
amc start --no-ui           # 仅后端
amc start --no-browser      # 不自动打开浏览器
```

## 功能概述

Mission Control 自动化代码审查流程：
1. 接入 GitHub PR（或本地 git diff）
2. 从仓库编译上下文（关键词提取 + 文件评分）
3. 通过 AI Agent（[OpenCode](https://github.com/opencode-ai/opencode)）执行结构化审查
4. 提取结构化发现（严重程度 / 文件 / 行号）
5. 执行审查策略（阻止危险命令）

### 核心特性

| 类别 | 亮点 |
|------|------|
| **审查** | 多轮审查、上下文感知、结构化发现 |
| **安全** | 静态审查策略（阻止 shell/docker/npm）、预检查 |
| **可靠性** | 优雅停机、崩溃恢复、空闲超时 |
| **输出** | 判定（通过/关注/阻止）、严重程度分级、可导出摘要 |
| **实时** | SSE 事件流，实时查看审查进度 |
| **Agent 技能** | MCP 服务器、CLI、Python SDK — 无需浏览器 |

## 环境要求

| 工具 | 必需 | 说明 |
|------|------|------|
| Python 3.11+ | ✅ | 后端运行时 |
| [OpenCode](https://github.com/opencode-ai/opencode) | ✅ | AI 审查 Agent |
| Node.js 18+ | 可选 | 前端 UI |
| `GITHUB_TOKEN` | 推荐 | 提高 API 限额 |

## Agent 技能

Mission Control 可作为 **AI Agent 技能** 使用 — 编码 Agent 调用它获取代码审查反馈，无需浏览器或服务器。

### CLI 审查 (`amc review`)

```bash
# 审查本地未提交的更改
amc review

# 审查 GitHub PR
amc review https://github.com/org/repo/pull/123

# JSON 输出 + 退出码（适合 CI）
amc review --format json --exit-code

# 聚焦审查
amc review --focus "security" --rounds 2
```

完整选项：
```
amc review [PR_URL] [选项]
  PR_URL              GitHub PR URL（省略则为本地 diff 模式）
  --repo, -r PATH     仓库路径（默认：当前目录）
  --base, -b BRANCH   基准分支（默认：自动检测）
  --rounds N          最大轮数（默认：1）
  --format FMT        markdown | json
  --focus, -f TEXT    审查重点
  --timeout SEC       每轮超时（默认：600秒）
  --exit-code         有问题时退出码为 1
```

### MCP 服务器 (`amc mcp`)

以 stdio 模式启动 MCP 工具服务器：

```bash
amc mcp
```

Agent 配置示例（Claude Desktop / OpenCode 等）：

```json
{
  "mcpServers": {
    "mission-control": {
      "command": "amc",
      "args": ["mcp"],
      "cwd": "/path/to/mission-control"
    }
  }
}
```

| MCP 工具 | 描述 |
|----------|------|
| `review_code` | 执行代码审查（PR 或本地 diff） |
| `get_review_findings` | 获取最近审查的发现 |
| `abort_review` | 取消正在运行的审查 |

### Python SDK

```python
from app.sdk import ReviewEngine

engine = ReviewEngine(language='en')  # 'zh' 用于中文输出
report = engine.review('/path/to/repo')

if report.passed:
    print("审查通过！")
else:
    for f in report.findings:
        print(f"[{f.severity}] {f.path}:{f.line} — {f.summary}")
```

### 审查模式

| 模式 | 触发条件 | 审查内容 |
|------|----------|----------|
| **PR 模式** | 提供 PR URL | GitHub Pull Request diff |
| **本地 diff** | 不提供 URL | `git diff` 对比基准分支 |

两种模式输出格式完全一致。

## API 接口

| 方法 | 路径 | 描述 |
|------|------|------|
| GET | `/api/health` | 健康检查 |
| GET | `/api/tasks` | 任务列表 |
| POST | `/api/tasks` | 创建任务 |
| GET | `/api/tasks/{id}` | 任务详情 |
| GET | `/api/tasks/{id}/stream` | SSE 事件流 |
| POST | `/api/tasks/{id}/start` | 开始审查轮次 |
| POST | `/api/tasks/{id}/abort` | 中止审查 |
| POST | `/api/tasks/{id}/validate` | 执行验证 |
| POST | `/api/tasks/{id}/export-summary` | 导出摘要 |
| POST | `/api/admin/shutdown` | 优雅停机 |

## 配置文件 (`.amc.yaml`)

```yaml
repo:
  path: .                        # 仓库路径
  base_branch: main              # 基准分支

backend:
  default: opencode              # Agent 后端
  opencode:
    model: ''                    # 模型覆盖
    variant: ''                  # 变体覆盖

context:
  include_recent_commits: 8      # 上下文近期 commit 数
  candidate_files_limit: 12      # 最大候选文件数

execution:
  idle_timeout_sec: 180          # Agent 空闲超时（秒）

validation:
  default_mode: standard         # standard | full
  checks:
    build: { command: '' }       # 如 'make build'
    test: { command: '' }        # 如 'go test ./...'
```

## 可靠性

- **预检查**：每轮审查前验证 AI 后端可用，失败则快速报错
- **优雅停机**：`amc stop` → 中止活跃任务 → SIGTERM → 兜底强杀
- **崩溃恢复**：重启时自动将滞留的 `running` 任务标记为 `FAILED`
- **空闲超时**：180 秒无响应自动终止 Agent 进程

## 跨平台支持

| 功能 | macOS/Linux | Windows |
|------|-------------|---------|
| `make start` | ✅ | ❌ |
| `amc start/stop/status` | ✅ | ✅ |
| `amc review` / `amc mcp` | ✅ | ✅ |
| 进程管理 | SIGTERM / killpg | taskkill /T |

## 备注

- 设置 `GITHUB_TOKEN` 或 `GH_TOKEN` 以访问 GitHub API
- 任务产物存储在 `runtime/tasks/<task-id>/`
- 审查完成后自动清理 worktree
- 日志：`.run/backend.log`、`.run/frontend.log`
