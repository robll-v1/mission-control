# Mission Control 项目设计、迭代设计与开发历程

本文档整理 `mission-control` 的项目定位、核心设计、关键迭代决策与开发历程，作为 README 之外的长期设计记录。它不是面向外部用户的产品文案，而是面向项目维护者的设计总览。

如果想看更正式的图示版说明，请继续阅读 [`architecture-and-sequence-diagrams.md`](architecture-and-sequence-diagrams.md)。

## 1. 项目定位

`mission-control` 的目标不是做一个 SaaS 审查平台，而是做一个 **local-first、以个人日常使用为中心的 AI code review console**。

当前定位可以概括成三句话：

1. **它是 review 过程管理器，不是单纯的模型调用壳。**
2. **它优先服务本地工作流**：既能审查 GitHub PR，也能审查本地 diff。
3. **它的主接口已经收敛为 Web UI + CLI**；MCP 代码保留，但不再是主产品方向。

因此，这个项目的设计重点一直不是“做最花哨的界面”，而是：

- 让 review 能稳定跑完；
- 让上下文足够相关；
- 让结果可读、可追踪、可增量使用；
- 让个人用户能够低摩擦地反复使用。

## 2. 核心设计原则

### 2.1 Local-first

项目从一开始就强调本地运行：

- 后端是本地 FastAPI 服务；
- 前端是本地 React + Vite；
- review 过程围绕本地仓库、worktree、artifact、SQLite 展开；
- 不依赖云端任务编排，也不把仓库内容发给额外第三方系统做二次处理。

### 2.2 “框架管理 review，Agent 负责推理”

项目里最关键的边界是：

- `mission-control` 负责 **任务创建、上下文编译、运行编排、结果解析、历史追踪、增量验证**；
- 底层 Agent CLI（OpenCode / Claude Code / Copilot CLI / Codex CLI）负责 **真正的审查推理**。

这让项目不会和具体模型强绑定，也让“多后端适配”成为自然演化方向。

### 2.3 一个核心，多个入口

虽然曾经探索过 Web UI、CLI、SDK、MCP 多种入口，但长期设计一直是“**共享同一个 review engine**”：

- Web UI：适合可视化任务管理与运行观察；
- CLI：适合个人日常调用与脚本化使用；
- Python SDK：适合作为 agent skill / 程序化入口；
- MCP：技术上打通过，但后续被降级为存档路径。

### 2.4 Structured over vibes

项目迭代过程里，多次选择了“结构化输出”而不是“只看模型自由发挥”：

- findings 解析有固定 severity 约束；
- review history 存结构化 finding；
- 指标采样记录 compile/prompt/LLM/parse 等耗时与大小；
- A/B 验证优先看真实样本和硬指标，而不是凭感觉说“优化了”。

### 2.5 先可用，再证据化优化

项目设计不是先追求理论上最完整，而是先把主链路跑通，再围绕真实痛点做优化：

- 先把启动、CLI、Web、增量 review、配置等主路径补齐；
- 再做 prompt、parser、context、verification 的质量优化；
- 再通过 benchmark 判断哪些优化真的成立。

## 3. 当前架构总览

当前仓库的核心结构如下：

```text
backend/
  app/
    api/           FastAPI 路由与 SSE 流
    adapters/      Agent 后端适配层（opencode / claude-code / copilot / codex）
    core/          execution、context compiler、review policy、worktree 等核心引擎
    services/      diff、artifact、result parsing、review history 等服务
    cli.py         amc start/stop/status/review/init
    sdk.py         ReviewEngine
    mcp_server.py  MCP 服务（保留但已阻塞）
frontend/          React + Vite UI
runtime/           运行期产物（task artifact、worktree、日志等）
data/              SQLite 数据库
```

一个标准 review 流程大致是：

1. 创建 task（PR 模式或 local diff 模式）；
2. 编译上下文（patch、关键词、候选文件、recent commits、snippet 等）；
3. 通过 adapter 调起 Agent CLI；
4. 采集事件流并保存 artifact；
5. 解析 findings，生成 verdict；
6. 将结果写入历史记录，支持后续增量 review / fix verification。

## 4. 当前主设计已经收敛到什么状态

### 4.1 主入口：Web UI + CLI

这是当前最明确的产品收敛点。

- **Web UI (`amc start`)**：适合看任务、日志、SSE 流、导出结果。
- **CLI (`amc review`)**：适合个人日常审查与 agent/脚本调用。

这两个入口共享同一套核心能力，所以不会出现“两个系统各写一套逻辑”的问题。

### 4.2 审查源：PR 与 local diff 统一

项目后续一个非常关键的设计转折是：**不再把 GitHub PR URL 当成唯一入口**。

统一后的模型是：

- 有 PR URL → PR review；
- 无 PR URL → local diff review。

这让项目从“PR 专用工具”变成了“日常代码审查工作台”。

### 4.3 后端适配：从单一 OpenCode 扩展到多 Agent CLI

在多后端适配后，项目不再绑定单一执行器，而是抽象成 adapter registry：

- `opencode`
- `claude-code`
- `copilot`
- `codex`

这是一项重要的长期设计：框架不依赖某个模型品牌，而依赖“一个能稳定跑审查 prompt 的 agent runtime”。

### 4.4 配置：全局 + 项目两级

后续加上的两级配置系统，解决了个人工具最常见的问题：

- 全局配置保存个人默认偏好；
- 项目配置保存仓库级特殊设置；
- 默认值、全局值、项目值深度合并。

这让工具既能“装一次到处用”，又能对单个仓库做精细调整。

### 4.5 历史与增量 review

项目不是一次性 review runner，而是可持续迭代使用的 review 环境：

- 保存 review history；
- 比较新旧 findings；
- 标记新问题、持续问题、已解决问题；
- 用 diff 范围批量判断旧 finding 是否已被修改。

这使它更适合“开发一批 → review → 修一批 → 再 review”的真实个人节奏。

### 4.6 质量优化已经进入“证据驱动”阶段

当前最新一轮设计，不再是补功能，而是优化质量与效率：

- prompt 结构化与 findings parser 增强；
- adaptive context budget；
- hunk-local snippets；
- batch fix verification；
- review metrics 与 A/B benchmark。

其中已经被真实数据明确验证的收益是：

- **batch fix verification** 显著减少增量验证成本。

而尚未被真实 benchmark 充分证明的部分是：

- **adaptive context + snippets** 在真实样本上尚未稳定证明质量更好，且某些样本更慢。

这也是当前设计成熟的一个标志：项目已经进入“不是做不出来，而是要证明值得保留”的阶段。

## 5. 迭代设计与开发历程

下面按阶段整理项目从最初雏形到当前状态的演进。

### 阶段 0：雏形建立（2026-03-17 ~ 2026-03-19）

代表提交：

- `a50a38c` Initial commit: Agent Mission Control with Linear/Vercel-style frontend
- `d2cb671` Refactor: restructure project, new Linear-style frontend, add review policy
- `2052c51` Update docs, add architecture overview and API reference

这个阶段完成了最早的产品骨架：

- FastAPI + React 的基本结构；
- 任务与审查主链路的初版；
- review policy 机制；
- 文档化的架构概览。

此时项目更像“一个本地 PR review Web 应用”，主方向还偏前后端产品雏形。

### 阶段 1：启动、可靠性与跨平台（2026-04-17）

代表提交：

- `2a47bcb` feat: one-click startup + cross-platform Python CLI
- `388139d` feat: graceful shutdown + stale task recovery
- `5c50191` fix: admin/shutdown kills reloader parent in --reload mode
- `de5633b` ci: add GitHub Actions CI workflow
- `c9d762d` fix: CI frontend type error + robust health check wait

这一阶段的设计目标是：**让它“像工具”而不是“像实验项目”**。

关键成果：

- `make start` / `amc start` 一键启动；
- `amc stop` / `amc status`；
- graceful shutdown；
- stale task recovery；
- CI 建立；
- Windows 兼容路径明确。

这个阶段决定了项目以后不会依赖 Docker 或复杂部署，而会坚持本地优先、轻部署、低摩擦。

### 阶段 2：Agent Skill 层形成（2026-04-20）

代表提交：

- `ca7749b` feat: add Agent Skill layer (SDK, CLI review, MCP server)
- `ba3ea88` docs: rewrite README with bilingual support (EN + ZH)

这轮是项目设计上的第一次大扩展：

- 从 Web UI 扩展到 CLI 和 Python SDK；
- 加入 `amc review`；
- 加入 MCP server；
- README 重写为双语，并把 Agent Skill 提到主叙事。

这一步把项目从“Web 工具”升级成“**review engine + 多入口壳层**”。

### 阶段 3：增量 review 与模型选择（2026-04-20）

代表提交：

- `351eac4` feat: intelligent incremental review system
- `7d61f71` feat: model selection priority chain (--model > $AMC_MODEL > .amc.yaml)

这一阶段的重点是贴合真实开发流程：

- 增量 review history；
- finding 对比与状态分类；
- 修复验证；
- 明确模型选择优先级链。

这标志着项目从“能 review 一次”迈向“能连续陪你做多轮开发”。

### 阶段 4：多后端适配与个人工具化（2026-04-20）

代表提交：

- `f214637` feat: multi-backend adapters (claude-code, copilot, codex)
- `53551b2` feat: add 'amc init' interactive setup command
- `a56638d` fix: preserve user cwd in amc review (critical path bug)
- `60bd465` feat: two-level config (global + project)
- `29eba1f` feat: preflight checks with friendly error messages
- `027b4e3` docs: complete user journey in README (EN + ZH)

这一轮把项目真正推向“个人长期工具”：

- 支持多个 agent CLI；
- `amc init` 降低首次配置成本；
- 两级配置提高复用性；
- 预检与错误提示更友好；
- 修复了 `cmd_review` 中影响实际使用的 cwd 关键路径 bug。

这一阶段非常重要，因为它把“功能完整”进一步变成了“日常可用”。

### 阶段 5：MCP 深挖与 direct API 绕路（2026-04-20）

代表提交：

- `dd3bc87` feat: add progress logging and get_review_status to MCP server
- `5669ad3` feat: non-blocking review_code MCP tool (fixes timeout)
- `f251552` feat: direct API mode for MCP — bypasses nested subprocess issue
- `df227e9` feat: model transparency + UX improvements for MCP

这是一个“技术上很有价值，但产品方向后来收缩”的阶段。

当时的核心问题是：MCP 嵌套 subprocess 的执行模式存在天然摩擦，因此做了几轮修补：

- 非阻塞 MCP review；
- 进度与状态接口；
- direct API mode；
- 更清晰的模型透明度。

这轮工作没有白做，因为它留下了：

- 更清楚的 adapter / execution 边界；
- 对 nested subprocess 问题的第一手经验；
- 一个可保留的 direct API 思路。

### 阶段 6：产品方向回收，MCP 归档，UI 改版被撤回（2026-04-20）

代表提交：

- `29c6ab4` docs: mark MCP as blocked, Web UI + CLI as primary interfaces
- `5a109ab` feat: premium UI redesign — Awwwards-inspired dark theme
- `4007e5d` Revert "feat: premium UI redesign — Awwwards-inspired dark theme"

这是项目设计上一次很关键的“收敛”：

1. **MCP 从主方向降级为归档代码**  
   不是因为完全做不通，而是因为它不再值得继续作为日常主接口投入。

2. **一次重 UI 改版被快速撤回**  
   这说明项目迭代标准已经很明确：如果变动不能带来真实价值，宁可立刻回滚，也不保留“看起来很努力”的改动。

从这一阶段开始，项目主叙事彻底回到 **Web UI + CLI**。

### 阶段 7：审查质量优化（2026-04-20 ~ 2026-04-21）

代表提交：

- `147b28c` feat: optimize review prompts and findings parser
- `530e0b6` feat: optimize review context and verification pipeline

这是当前最近、也是最成熟的一轮工程化优化。

#### 7.1 Prompt 与 parser 优化

目标是让审查结果：

- 更稳定；
- 更少误报；
- 更好解析；
- 对 severity 和 summary 更一致。

#### 7.2 Context 与 verification 优化

这一轮进一步把“优化”拆成了可验证包：

- baseline metrics
- adaptive context budget
- hunk-local snippets
- batch fix verification
- A/B validation

真实结论不是“所有优化都成立”，而是更细的判断：

- **成立**：batch verification 明显更快，且结果一致；
- **未完全成立**：adaptive context + snippets 机械上工作正常，但还没有被真实 benchmark 证明一定带来更高质量，且一些样本更慢。

这轮工作的价值不只是代码本身，更在于它把项目带入了“**benchmark 驱动判断保留与否**”的阶段。

## 6. 被保留、被降级、被回滚的设计决策

### 6.1 被保留的设计

- 本地优先（local-first）
- Web UI + CLI 双主入口
- Python SDK 作为内核能力暴露
- 多后端 adapter
- PR / local diff 双模式
- review history + incremental review
- 结构化 findings 与 metrics

### 6.2 被降级的设计

- **MCP**：代码保留，但不再作为主接口维护

原因不是单点 bug，而是综合判断后，主路径收益不足。

### 6.3 被回滚的设计

- **高级视觉 UI 改版**

原因很直接：不符合项目当前优先级，也没有带来你想要的实质性收益。

### 6.4 暂缓推进的设计

- 二次 finding verifier
- 更重的 repo snapshot cache
- 更进一步的 prompt/context 膨胀式优化

这些都不是做不了，而是当前证据不足，不值得优先做。

## 7. 当前状态判断

截至 `530e0b6`，项目可以这样理解：

### 已经成立的部分

- 主链路可用；
- Web UI + CLI 可日常使用；
- 个人项目场景下已经足够长期使用；
- 架构边界已经比较清楚；
- 核心设计不需要再大改。

### 仍然存在的边界

- GitHub PR 路径在未配置 token 时可能遇到 API rate limit；
- dev 模式下 `uvicorn --reload` 仍可能出现重载期假活问题；
- context/snippet 优化是否真正提升质量，还需要更多真实样本证明。

### 最重要的判断

这个项目当前已经不是“还没想清楚要做什么”，而是一个 **已经收敛成型的个人 review workstation**。  
后续更应该围绕真实使用摩擦继续迭代，而不是为了“更完整”去重做设计。

## 8. 关键里程碑速查表

| 日期 | 提交 | 里程碑 |
|---|---|---|
| 2026-03-17 | `a50a38c` | 初始版本落地，建立前后端骨架 |
| 2026-04-17 | `2a47bcb` | 一键启动与跨平台 CLI 成形 |
| 2026-04-17 | `388139d` | graceful shutdown 与崩溃恢复补齐 |
| 2026-04-20 | `ca7749b` | SDK / CLI / MCP Agent Skill 层完成 |
| 2026-04-20 | `351eac4` | intelligent incremental review 落地 |
| 2026-04-20 | `f214637` | 多后端 adapter 完成 |
| 2026-04-20 | `53551b2` | `amc init` 完成，配置体验改善 |
| 2026-04-20 | `60bd465` | 两级配置系统完成 |
| 2026-04-20 | `29c6ab4` | MCP 被正式降级，Web UI + CLI 成为主接口 |
| 2026-04-20 | `147b28c` | prompt 与 findings parser 优化 |
| 2026-04-21 | `530e0b6` | review context / verification pipeline 优化 |

## 9. 文档结论

如果用一句话概括这段开发历程：

> `mission-control` 从“本地 PR review Web 应用”逐步演变成了“以个人日常使用为中心的、本地优先的 AI review console”，并且已经完成从功能堆叠到架构收敛、再到证据驱动优化的转变。

因此，今天再看这个项目，最重要的不是“它还有没有地方可以继续加”，而是：

- 主设计已经成立；
- 主要能力已经可用；
- 未来迭代应继续围绕真实使用摩擦和真实 benchmark，而不是重新发明方向。
