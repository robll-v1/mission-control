<h1 align="center">Mission Control</h1>

<p align="center">
  <strong>别让写代码的同一个 Agent 自己给自己打分。</strong>
</p>

<p align="center">
  在 <code>git push</code> 之前，用你本机已有的第二个 Coding Agent CLI（Codex、Claude Code、OpenCode 或 Copilot）隔离交叉审查未提交改动或 GitHub PR，并把每个问题跨轮追踪到真正修复。
</p>

<p align="center">
  <a href="#快速开始"><strong>快速开始</strong></a> &nbsp;&middot;&nbsp;
  <a href="#为什么不直接让当前-agent-自审"><strong>为什么交叉审查</strong></a> &nbsp;&middot;&nbsp;
  <a href="#真实捕获案例"><strong>真实战绩</strong></a> &nbsp;&middot;&nbsp;
  <a href="#工作台预览"><strong>工作台</strong></a> &nbsp;&middot;&nbsp;
  <a href="#明确边界"><strong>能力边界</strong></a> &nbsp;&middot;&nbsp;
  <a href="./README.md"><strong>English</strong></a>
</p>

<p align="center">
  <a href="https://github.com/robll-v1/mission-control/actions/workflows/ci.yml"><img src="https://github.com/robll-v1/mission-control/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  <a href="https://pypi.org/project/agent-mission-control/"><img src="https://img.shields.io/pypi/v/agent-mission-control?style=flat-square&color=2F55D4" alt="PyPI" /></a>
  <a href="https://www.python.org"><img src="https://img.shields.io/badge/python-3.11%2B-3776AB?style=flat-square" alt="Python 3.11+" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-22663E?style=flat-square" alt="MIT License" /></a>
  <a href="#支持的-agent"><img src="https://img.shields.io/badge/agents-Codex_%7C_Claude_Code_%7C_OpenCode_%7C_Copilot-2F55D4?style=flat-square" alt="支持 Codex、Claude Code、OpenCode 与 Copilot" /></a>
</p>

<p align="center">
  <img src="docs/screenshots/hero-explainer.png" alt="Mission Control 工作流：在常用 Agent 中编码，用本机第二个 CLI 隔离交叉审查，并跨轮追踪 Likely fixed 与 Persistent 问题" width="960" />
</p>

---

## 快速开始

推荐通过 **Skill + CLI** 直接嵌进你现有的 Coding Agent 工作流，也可以一行拉起 **本地 Web 工作台**（无需安装 Node.js）。

### 方式 A：Agent Skill（直接嵌进 Claude Code / Codex / Cursor / OpenCode）

```bash
npx skills add robll-v1/mission-control -g
```

装完后，在 `git push` 之前直接对你的编码 Agent 说：

```text
用 mission-control 对我当前未提交改动做一次交叉审查，修掉确认的问题后再复审第 2 轮。
```

内置 Skill（[`skills/mission-control/SKILL.md`](skills/mission-control/SKILL.md)）会自动避开当前宿主、选择本机已装的**第二个** CLI（例如 Claude Code 写代码 → Codex 审查），调用 `amc review --format json`（本机未全局装 `amc` 时自动走 `uvx`），并在修复后自动复审。

### 方式 B：MCP Server（Claude Code / Cursor / Windsurf / Claude Desktop）

```bash
# Claude Code 一行接入
claude mcp add mission-control -- uvx agent-mission-control mcp
```

或添加到 `.mcp.json` / Cursor MCP 配置：

```json
{
  "mcpServers": {
    "mission-control": {
      "command": "uvx",
      "args": ["agent-mission-control", "mcp"]
    }
  }
}
```

提供 7 个正交工具（`check_agents`、`start_review`、`wait_review`、`verify_findings`、`run_blind_jury`、`update_finding`、`abort_review`），直接驱动本机 CLI 并与 `amc start` 共享同一个 SQLite 数据库（随时打开 Web 工作台即可查看 MCP 发起的任务、Diff、2/2 双模型盲审共识与跨模型冷证伪证据）。

### 方式 C：CLI 或本地 Web 工作台（`uvx` 零克隆启动）

```bash
# 终端审查未提交改动（可选 --verify-with auto 用第二个本地 CLI 对发现项做冷证伪）
uvx agent-mission-control review --backend codex --reasoning high --verify-with auto --format json --exit-code

# 或直接运行同快照双模型盲审（2 个不同本地 CLI 并行独立审，仅对 1/2 独有分歧定向冷证伪）
uvx agent-mission-control review --jury --format json --exit-code

# 或直接启动本地 Web 工作台（内置 React 静态包，无需 Node.js）
uvx agent-mission-control start
```

希望常驻安装到本机：

```bash
pipx install agent-mission-control   # 或：pip install agent-mission-control
amc review      # 命令行审查
amc start       # 启动 127.0.0.1 本地工作台（amc stop 停止）
amc doctor      # 不调用大模型的静态探针，检查 CLI 参数与推理档位
```

<details>
<summary>30 秒合成演示仓库体验 / 从源码安装</summary>

```bash
# 生成含合成 SQL 注入的极简演示仓库
python3 examples/first-review/make_demo_repo.py /tmp/amc-demo-notes
amc review --repo /tmp/amc-demo-notes --format json --exit-code

# 应用修复（保持未提交）并换另一个 Agent 与推理档位跑第 2 轮
python3 examples/first-review/make_demo_repo.py /tmp/amc-demo-notes --apply-fix
amc review --repo /tmp/amc-demo-notes --backend codex --reasoning high
```

贡献者从源码安装：

```bash
git clone https://github.com/robll-v1/mission-control.git
cd mission-control
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
cd frontend && npm ci && npx tsc --noEmit && npm test && npm run build
```

</details>

---

## 为什么不直接让当前 Agent 自审？

| 在聊天框里喊一句「帮我 review 一下」的痛点 | Mission Control 的解法 |
|---|---|
| **同模型盲区。** 刚写完代码的 Agent 往往会顺着自己的假设合理化缺陷。 | **每轮自由跨 Agent。** 任意一轮都能切换 Agent、模型与推理档位（例如 Claude Code 写完，交给 Codex `high` 审）。 |
| **跨轮丢账、假修复。** 第二轮模型漏提了某个老问题，人就误以为已经修好。 | **SQLite 跨轮状态机。** 漏提只记为 `Unverified`，仅当两轮快照间该处代码确有变动才标 `Likely fixed`，系统永不自动标 `Resolved`。 |
| **审查对象和磁盘不一致。** 只看 `git diff` 容易漏掉未跟踪文件或未暂存修改。 | **零侵入本地快照。** 复制真实 index 到临时 `GIT_INDEX_FILE` 生成快照 commit，在隔离 worktree 中运行审查，绝不碰你的工作区和 `HEAD`。 |

---

## 真实捕获案例

在开发 Mission Control 自身的 [PR #35](https://github.com/robll-v1/mission-control/pull/35)（P6 工作台）时，使用第二个 Agent 做交叉审查在合入前直接抓出了 **4 个真实 P1 级安全与并发缺陷**：

| 类别 | 合入前抓出的真实问题 | 对应修复与测试验证 |
|---|---|---|
| **并发竞态** | `DELETE /api/tasks/{id}` 检查活跃状态后即放锁，与并发 `start_task` 及后台 `ValidationService` 存在竞态。 | 统一按 task ID 的互斥占位（启动/验证/删除共享）并补齐屏障式并发回归测试。 |
| **Git 安全** | 仓库预检触发的 `git status` 可能执行仓库内配置的 `core.fsmonitor` 外部命令并写索引锁。 | 强制追加 `--no-optional-locks` 并禁用 `core.fsmonitor`、`core.hooksPath`、`diff.external` 与 LFS 过滤器。 |
| **Windows 安全** | 白名单编辑器名（`code`）在 Windows 上会解析为 `code.cmd`，导致 `cmd.exe` 解析来自仓库的文件名。 | Windows 强制要求绝对路径原生 `.exe`，macOS 强制 `open -t`，全平台 `shell=False`。 |
| **状态诚实** | 运行进度条把所有后端都标成「只读沙箱」，而实际仅 Codex 启用了 `--sandbox read-only`。 | 仅当当前轮后端为 Codex 时展示沙箱标识，并在准备阶段记录真实待启动配置。 |

---

## 工作台预览

<p align="center">
  <img src="docs/screenshots/workbench-hero.png" alt="Mission Control 工作台全貌：多轮卡片、跨轮 Finding 状态、内联代码片段与一键操作" width="920" />
</p>

| 每轮自由切换 Agent、模型与推理强度 | 精确快照与 Diff 视图 | 本机动态检测与静态自检 |
|---|---|---|
| <img src="docs/screenshots/workbench-popover.png" alt="RunRoundPopover：按轮切换 Agent、模型、推理强度、输出语言、附言与关注点" /> | <img src="docs/screenshots/workbench-changes.png" alt="Changes 标签页展示 notes.py 参数化查询修复的统一 Diff" /> | <img src="docs/screenshots/workbench-settings.png" alt="模型选择页展示本机检测到的 CLI、各模型推理档位及 Check agent 静态检查" /> |
| 同一审查内，任意一轮都可单独改 Agent、模型、推理档位、输出语言、附言或关注点，无需重建任务。 | 查看已暂存、未暂存与未跟踪文件的真实统一 Diff，并可直接通过受控白名单在编辑器打开 `file:line`。 | 从本机缓存、CLI 帮助和配置文件动态读取模型与推理档位，绝不写死；内置不消耗额度的 `Check agent` 静态检查。 |

---

## 支持的 Agent

| Agent | CLI | 模型发现来源 | 推理强度（Reasoning） | 只读沙箱 |
|---|---|---|---|---|
| **Codex CLI** | `codex` | 本机 `~/.codex/models_cache.json` / 配置 | 按模型读取档位（`-c model_reasoning_effort=...`） | 支持（`--sandbox read-only`） |
| **Claude Code** | `claude` | 本机 CLI / 配置 | 从 `claude --help` 解析（`--effort ...`） | 否（仅 PATH 护栏） |
| **OpenCode** | `opencode` | 本机 CLI / catalog 缓存 | 兼容历史 `provider/model#variant` 别名 | 否（仅 PATH 护栏） |
| **Copilot CLI** | `gh copilot` | Agent 默认 / 配置 | CLI 未暴露该参数 | 否（仅 PATH 护栏） |

推理强度默认始终为 **`Model default`**（`null`，不向 CLI 传参）；切换 Agent 时不会把上一个 Agent 的模型或档位串给下一个 Agent。

---

## 与同类工具对比

| | Mission Control | [CodeRabbit](https://www.coderabbit.ai) | [GitHub Copilot Review](https://docs.github.com/en/copilot) | [PR-Agent](https://github.com/The-PR-Agent/pr-agent) |
|---|---|---|---|---|
| **运行位置** | 100% 运行在你本机 | 云端托管（PR Bot + CLI） | GitHub / VS Code | 自托管，偏 CI 场景 |
| **模型与 Agent** | 复用本机任意已装 CLI；支持每轮切换 | 平台选定 | 平台选定 | 自备 API Key |
| **额外费用** | 除现有 Agent 外无任何额外订阅 | 订阅制 | Copilot 套餐 | 按 API 用量计费 |
| **未提交本地改动** | 支持（暂存/未暂存/未跟踪快照） | 支持（CLI） | 支持（VS Code） | 偏 PR 审查 |
| **跨轮问题状态** | `New / Persistent / Unverified / Likely fixed` + 人工 `Resolved / Dismissed` | PR 评论串 | PR 评论串 | PR 评论 |
| **协议** | MIT 开源 | 闭源 | 闭源 | 开源 |

<sub>整理自公开文档，如有出入欢迎提交 PR 修正。</sub>

---

## CLI、SDK 与配置

```bash
# CLI
amc review [PR_URL] [--repo PATH] [--backend codex|claude-code|opencode|copilot] [--model MODEL] [--reasoning LEVEL] [--verify-with auto|BACKEND] [--jury [--jury-backends A,B]] [--focus TEXT] [--format json] [--exit-code]
amc start [--port 8000] [--no-browser]   # amc status / amc stop / amc doctor / amc init [--global]
```

```python
from app.sdk import ReviewEngine

engine = ReviewEngine(language="en", backend="codex", reasoning="high")
report = engine.review("/path/to/repo")
for f in report.findings:
    print(f"[{f.severity}] {f.path}:{f.line} — {f.summary}")
```

<details>
<summary>配置分层（<code>.amc.yaml</code>）、存储路径与架构文档</summary>

配置优先级：`命令行参数（--model / --reasoning）> 环境变量（AMC_MODEL / AMC_REASONING）> 仓库 .amc.yaml > 全局 ~/.config/amc/config.yaml > Agent 默认`。

```yaml
repo:
  base_branch: main

backend:
  default: codex
  codex:
    model: gpt-6.1-terra
    reasoning: high

validation:
  default_mode: standard
  checks:
    tests:
      command: pytest -q
      modes: [standard, full]
      required: true
```

全局配置位于 `~/.config/amc/config.yaml`（遵循 `XDG_CONFIG_HOME`）。SQLite 数据库、产物与短路径隔离 worktree（`<data_dir>/wt/<task_id>`）保存在系统应用数据目录（macOS `~/Library/Application Support/amc`、Linux `~/.local/share/amc`、Windows `%APPDATA%\amc`），可通过 `AMC_DATA_DIR` 覆盖。

延伸阅读：[架构与时序图](docs/architecture-and-sequence-diagrams.md) &middot; [本地快照设计](docs/local-review-design.md) &middot; [项目设计历程](docs/project-design-history.md)

</details>

---

## 明确边界

- **列出模型不等于账号一定可调用**：模型与推理档位来自本机缓存、帮助信息或配置文件，只有真实调用才能验证账号权限与额度。
- **PATH shim 是护栏而非系统沙箱**：目前仅 Codex CLI 启用 `--sandbox read-only`，界面也仅在当前轮使用 Codex 时展示沙箱标签。
- **未再提及不等于已修复**：后续轮次未再报出的问题只记为 `Unverified`（或在对应位置代码发生变动时标为 `Likely fixed`），系统绝不会自动标为 `Resolved`。
- **严格本地回环安全**：Web 服务默认仅监听 `127.0.0.1` 并校验精确 `Host`/`Origin`；`Open in editor` 仅允许白名单原生可执行文件打开仓库根目录内的常规文件；仓库预检禁用 `core.fsmonitor` 等仓库配置外部命令并关闭可选索引锁写入。

## 许可证

[MIT](LICENSE)
