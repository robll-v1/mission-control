# AGENTS.md — 协作交接文档

本文件是 Claude 和 GPT（Codex 等）共用的项目说明与交接记录。
Codex 会自动读取 `AGENTS.md`；Claude Code 通过 `CLAUDE.md` 中的 `@AGENTS.md` 读取同一份内容。
**开工前读完本文件；收工前更新「当前状态」和「交接日志」。**

## 1. 项目是什么

Mission Control（命令 `amc`）：本地优先的 AI 代码审查控制台。
它不自己调用大模型，而是驱动用户本机已有的 coding agent CLI（opencode / claude-code / codex / copilot）去审查 GitHub PR 或本地改动，把结果解析成结构化问题（严重程度 + `file:line`），并在多轮审查之间追踪。

- 入口：Web UI（FastAPI + React）、CLI `amc review`、Python SDK `app.sdk.ReviewEngine`。MCP 服务已归档，只保证 v1 API 能用。
- 仓库：https://github.com/robll-v1/mission-control（MIT）
- 用户是在读大学生，这个项目同时用于作品集和找实习，所以除了代码质量，**展示效果和可讲述性也很重要**。

## 2. 协作约定

1. **一个任务一个分支一个 agent。** 两个 agent 不同时修改同一批文件。开新分支前先看「当前状态」，确认没有别人正在改的分支。
2. **不直接推 `main`。** 一律走 PR，由用户合并。commit 末尾写各自的 `Co-Authored-By`。
3. **提交前必须本地验证：** 后端跑 `pytest -q`，改了前端还要跑 `npx tsc --noEmit` 和 `npm run build`。
4. **commit 要自洽。** 不能出现「import 了一个还没提交的函数」这种情况（CI 曾因此 9 个 job 全部失败）。拆分 commit 后，在干净 checkout 上跑一遍测试。
5. **不要 `git stash` 用户的改动，也不要 reset 或丢弃不是你自己产生的改动。** 需要切分支时先 `git status` 看一眼，有疑问就问用户。
6. **草稿或演示文件不进 commit。** 现在仓库里的 `BUGFIX_REPORT.md`、`model-selector-demo.html`、`backend/requirements.txt` 都是未跟踪的草稿；其中 `requirements.txt` 的版本约束和 `pyproject.toml` 冲突，以 `pyproject.toml` 为准。
7. **汇报要如实。** 没验证过就说没验证。诊断错了要明确更正，不要悄悄改掉。

## 3. 用户偏好

- 用中文沟通。代码、commit、PR 用英文。
- **UI 里不用系统 emoji**（包括 💡📝🔍 这类）。美化以后统一处理，现阶段保持朴素。`cli.py` 里的 emoji 是历史遗留，还没清理。
- 设置页的标题叫「模型选择」，不要叫「智能模型选择器」。
- 模型列表必须从本机动态读取，不能写死（用户实际在用 GPT-6 系列等新模型）。
- 喜欢直接、真实的评价，不需要客套。

## 4. 仓库地图

```
backend/app/
  cli.py                 amc start/stop/status/review/init/mcp
  sdk.py                 ReviewEngine（CLI 和 MCP 都走这里）
  api/app.py             FastAPI 装配；routes_tasks / routes_config / routes_stream / routes_control / routes_artifacts
  adapters/              每个 agent CLI 一个 adapter；__init__.get_adapter(name, model=, variant=)
  core/config.py         配置分层、resolve_backend_config、save_global_config
  core/backend_detector.py  检测已安装的后端和可用模型
  core/execution.py      ExecutionService：每轮构造 adapter、刷新本地快照、跑 agent
  core/worktree.py       每个任务一个隔离 git worktree
  core/context_compiler.py  组装审查上下文和 prompt
  core/review_policy.py  PATH shim 护栏（防误操作，不是沙箱）
  services/local_review_service.py  本地改动 → 快照 commit + patch
  services/review_history.py        多轮问题追踪（.amc/reviews/）
frontend/src/            App.tsx（主界面）、Settings.tsx（模型选择）、styles.css
backend/tests/           pytest；夹具见 conftest.py
docs/                    设计历程、架构图、screenshots/
```

## 5. 常用命令

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
pytest -q                                  # 后端测试（目前 74 passed, 1 skipped）
cd frontend && npm ci && npx tsc --noEmit && npm run build
amc start            # 启动后端 + 前端并打开浏览器；amc stop / amc status
amc review           # 审查当前仓库的本地改动
```

CI（`.github/workflows/ci.yml`）：后端在 ubuntu/macos/windows × Python 3.11–3.13 上运行，包括 pytest、导入检查、CLI 帮助和健康检查（非 Windows）；前端运行 tsc 和 build。**Windows 也在矩阵里**，涉及路径、编码、进程的改动要注意。

## 6. 不明显但重要的设计

- **配置分层：** 内置默认 < 全局 `~/.config/amc/config.yaml`（遵循 `XDG_CONFIG_HOME`）< 仓库 `.amc.yaml` < 显式参数。模型优先级：显式 `--model` > `AMC_MODEL` > 配置文件。
  - `resolve_backend_config(repo_path, backend=, model=)` 是唯一的解析入口，Web、SDK、CLI 都要用它，不要各自再实现一套。
  - 显式指定了 backend 时，不能继承其他 backend 的 model。
- **保存配置：** `/api/config/save` 先读出全局配置，只更新 `backend.default` 和所选 backend 的 model/variant，然后原子写入（临时文件 + `os.replace`）。**不能整份覆盖**，否则会丢掉用户的其他配置。
- **后端检测：**
  - codex 的模型来自 `~/.codex/models_cache.json`；opencode 的模型来自 `~/.local/share/opencode/opencode.db` 中的 `models-dev:catalog`。
  - 检测会阻塞，所以 `routes_config` 里的路由是同步 `def`，由 FastAPI 放进线程池执行。**不要改成 `async def`**，否则会卡住事件循环。
- **本地快照：** `capture_local_snapshot` 复制真实 index 到一个临时 `GIT_INDEX_FILE`，然后执行 `add -A`、`write-tree`、`commit-tree`，生成一个父提交为 HEAD 的快照 commit。
  - 全程不碰用户的 index、HEAD 和工作区。
  - 排除 `.amc/reviews` 和运行时生成的路径。
  - worktree 从 `task.local_snapshot_sha` 创建，保证 agent 读到的 patch 和它能打开的文件一致。
  - 每一轮都会重新拍快照（`ExecutionService._refresh_local_snapshot`）。
- **多轮追踪：** 问题分为 new / persistent / unverified。上一轮的问题如果这一轮没被提到，状态是 **unverified，不是 resolved**。只有 `incremental=False` 的完整审查才能清空历史。
- **adapter：** 在 `ExecutionService` 里通过 `adapter_factory` 按次构造，不再是启动时就固定。准备阶段失败时要清理 worktree，并把阶段标为 `review_prepare_failed`，以便重试。
- **MCP：** 依赖固定为 `mcp>=1.0.0,<2`，因为 v2 改了 FastMCP 的 API。

## 7. 测试约定

- `git_repo` 夹具：两次提交的临时仓库，分支为 `main`，第二条提交说明里有非 ASCII 字符。
- `isolated_config` 夹具：把 `XDG_CONFIG_HOME` 指到临时目录，并清除 `AMC_MODEL`。**凡是会读配置的测试都要用它**，否则开发者本机的配置会污染测试结果。
- 测试不能依赖网络或真实的 agent CLI，需要时用 monkeypatch 替换 adapter 或 `call_llm`。

## 8. 环境注意事项

- Claude 运行的 Bash 沙箱**没有网络**，pip、`gh`、`git push` 需要在用户的终端里执行。GPT 那边的环境可能不同，以实际情况为准。
- 用户的网络连 GitHub 偶尔会出现 `SSL_ERROR_SYSCALL`，通常是暂时的，重试即可。

## 9. 当前状态（2026-10-24 更新）

审查补充（2026-10-24，Codex，只读源码审查）：
- 基线 HEAD `316116a`，后端 `75 passed, 1 skipped`；未修改业务代码。
- 优先待修：本地 diff 的空格路径/删除内容未进入 prompt；同任务并发 start 可创建两轮。
- 其他已复现：PR/local 历史串用、abort 最终变 failed、损坏 YAML 保存时被覆盖、并发配置保存丢更新、重启恢复遗漏 VALIDATING 且 Run/CheckRun 未收尾。
- 能力边界：Web 完整轮次结果与 SDK 增量聚合语义不同；部分模型列表仍是固定值；子模块未提交内容不在父仓库快照内；PATH shim 不是强制沙箱。

已合并到 `main`：
- #17：后端和模型自动检测、设置页（模型选择）、配置真正作用于 Web 审查、本地快照一致性、unverified 状态修复、MCP 固定在 v1。

进行中：
- **#18** `docs/readme-first-screen`：重写 README 首屏（一句话定位、截图、三个差异点、竞品对比表），中英文都改了。CI 全部通过。**等用户在 GitHub 上看过亮色和暗色两种主题的渲染效果后再合并。**

本地遗留（等用户确认后再清理）：
- `git stash` 中有两条：`stash@{0}` 是 #17 的工作，已全部提交；`stash@{1}` 是更早的整体备份。
- 三个未跟踪的草稿文件（见第 2 节第 6 条）。
- 本地分支 `fix/p1-3-local-snapshot` 已合并，可以删除。

待办（按投入产出排序）：
1. 给设置页（模型选择）截图，加进 README。
2. 录一段约 30 秒的 GIF：选模型 → 发起审查 → 看结果。
3. UI 视觉美化，并清理 `cli.py` 里的 emoji。
4. 写一篇技术文章：快照一致性和多后端适配，用于面试。

## 10. 交接日志

每次收工追加一条，最新的放在最上面。格式：日期 · 谁 · 分支/PR · 做了什么 · 没做完或没验证的 · 下一步建议。

- 2026-10-24 · Codex · `docs/readme-first-screen` / HEAD `316116a` · 4 个只读 subagent 分域探索，父代理和独立 reviewer 复跑临时复现；确认上述遗留问题，未改业务代码或用户数据 · 未做真实 Agent/Web E2E 与 Windows 路径攻击验证 · 优先修 diff 解析遗漏和并发启动；细节见本轮审查报告。
- 2026-10-24 · Claude · #18 · README 首屏重写已推送，PR 已开，CI 全部通过 · 还没在 GitHub 上看实际渲染效果 · 用户合并 #18 后，从待办第 1 项开始。
- 2026-10-23 · Claude · #17 · 修复 review 中的 P1 和 P2 问题；CI 失败的原因是拆分 commit 时漏提交了依赖文件，补交 `935a507` 后修复 · — · —
- 2026-10-23 · GPT（推测）· #17 · `2005d9c` 把 MCP 固定在 v1 API，并补了注册测试 · — · —
