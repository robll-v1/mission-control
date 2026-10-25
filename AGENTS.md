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
- **UI 里不用系统 emoji**（包括 💡📝🔍 这类）。美化以后统一处理，现阶段保持朴素。本轮 `cli.py` 的历史 emoji 已清理。
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
pytest -q                                  # 后端测试；数量以当前提交的 CI 为准
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
  - codex 的模型来自本地 cache/config；opencode 优先查询 CLI，再读取本地 catalog cache。来源只证明枚举，不证明账号可调用；无法枚举时保留后端默认和显式配置模型。
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

## 9. 当前状态（2026-10-25 更新）

- Codex 本轮审核：PR #22 (`codex/launch-acceptance` / `5476de2`) 已存在；准确 head 的 CI run `40105840872`，9 个后端矩阵与 Frontend 全部成功，代码审核通过。发布门槛仍按下文区分，不将 CI 通过等同于真实模型 E2E。
- WorkBuddy 新文档 `codex/demo-plan-workbuddy` 基于 #22；演示分镜与 Web 清单全部标注尚未执行。Codex 补正明确 checkout 入口、配置隔离范围与模型默认提示后提交审查；需先合并 #22。
- GLM `codex/preflight-cancel-glm` / `d688729`：独立全量 `153 passed, 1 skipped`，但额外复现准备期间取消被 INGESTING 覆盖，以及最后取消检查后仍启动 worker 两处竞态，暂不就绪。
- Qoder `codex/install-smoke-qoder` / `bf1ed4c`：独立全量 `157 passed, 1 skipped`，但发现陈旧 PID 可扩大为误杀整个无关进程组、启动失败删除诊断日志，暂不就绪。
- 远端 `feat/mission-console-ui` / `1935782`：TypeScript 检查报 `ImportMeta.env` 缺失类型，且 Settings 切回后端未恢复已保存模型；暂不就绪。远端 `fix/review-pipeline-integrity` / `f1f0c09` 的新增部分：构建虽 exit 0 但有 CSS 语法告警，seed 结果 6 findings 被读取为计数 0，暂不就绪。
- 原工作区由他人切回本地 `main` / `b3f3cbc`（落后远端）；Codex 未切换或快进。现有四份 stash（含原两份）与 `.workbuddy/` 保留。本轮验证使用隔离工作树/临时导出，不启动真实 Agent。

- PR #17、#18、#19、#20、#21 均已合并；本轮整合基线 `main` / `7a093bd`。
- 当前分支 `codex/launch-acceptance`：Codex 统一验收 GLM、Qoder、WorkBuddy 的独立工作，尚未合并。GLM 源提交 `0587e1f`，Qoder 源提交 `ad90dab`，WorkBuddy 源提交 `f088429`。
- GLM：去除模型硬编码兜底，保留来源/未知原因，缓存与 TOML 解析容错；修正本机 opencode v2.0.14 参数和错误详情；补合成示例与真实调用记录。
- Qoder：CLI 错误可操作、JSON stdout 纯净、失败退出码非零、非正轮数/超时拒绝；清理 CLI emoji。`argparse` 语法错误仍走标准 stderr，JSON 语法错误封装不在本轮范围。
- WorkBuddy：发布文案及快照/并发/增量设计文章。Codex 已删改夸大表述，并在设置页显示模型来源和调用权限未验证提示。
- 最终整合工作区后端全量 `pytest -q`：`147 passed, 1 skipped`，34.04s，退出码 0；前端 `npx tsc --noEmit` 与 `npm run build` 通过。Qoder CLI focused 测试 `23 passed`，演示 fixture focused 测试 `4 passed`；独立 reviewer 接受 fixture 防覆盖修复。
- 上述本地验收对应的最终提交为 `5476de2`，已推送并开 PR #22；该准确 head 的 CI 结论见本节顶部。
- 真实模型首轮已识别合成 SQL 注入；第二轮虽真实调用，但原示例恢复为 HEAD 导致空 diff，**有效修复 diff 复审未验收**。后补示例已离线验证，不允许额外调用或冒充 E2E 通过。
- 发布判断：工程补强可进入 PR 审查和小范围试用；正式演示推广前，需完成非空修复 diff 的真实复审、干净环境安装与 Web 操作流程验证，再录约 30 秒剪辑演示。
- 已知边界：150 秒 preflight 阶段不可取消；opencode 旧版本未验证；SDK 增量历史仍基于 HEAD 而非上一轮未提交快照；Web 只看最新一轮，不能宣称有 SDK 增量聚合；子模块内部未提交内容不在父仓库快照中；PATH shim 不是沙箱。
- 原工作区未由 Codex 切换或修改；他人继续工作后的 HEAD 为 `5055c69`（含截图与 `seed_demo_simple.py` 提交），另有 `frontend/src/styles.css` 未提交改动，均未纳入本轮验收。两份原 stash 的 hash 保持不变；原未跟踪文档/草稿及其他代理 worktree 保留供追溯。

## 10. 交接日志

每次收工追加一条，最新的放在最上面。格式：日期 · 谁 · 分支/PR · 做了什么 · 没做完或没验证的 · 下一步建议。

- 2026-10-25 · Codex · 分支就绪审核 / #22 · 核对全部远端与三方新交付，独立重跑 GLM 153、Qoder 157 项测试并发现测试外阻塞；核对 #22 准确 head 的全部 CI 成功；补正 WorkBuddy 两文档的执行隔离说明 · 新 UI、取消修复与安装修复仍需补修，真实复审/Web E2E 未验收 · 仅为通过审核的内容开 PR，不合并分支。

- 2026-10-24 · Codex（协调 GLM/Qoder/WorkBuddy）· `codex/launch-acceptance` · 整合模型来源、CLI 边界与发布文案；本地后端全量 `147 passed, 1 skipped`（34.04s，exit 0）、前端 tsc/build 通过；CLI focused 23 项、fixture focused 4 项通过，fixture 补修获独立 reviewer ACCEPT · 未 push，故无 CI 结论；真实非空修复 diff 复审、干净安装、Web E2E 未验收 · 先小范围试用，完成发布门槛后再录演示。

- 2026-10-24 · Codex · `docs/readme-first-screen` / HEAD `316116a` · 4 个只读 subagent 分域探索，父代理和独立 reviewer 复跑临时复现；确认上述遗留问题，未改业务代码或用户数据 · 未做真实 Agent/Web E2E 与 Windows 路径攻击验证 · 优先修 diff 解析遗漏和并发启动；细节见本轮审查报告。
- 2026-10-24 · Claude · #18 · README 首屏重写已推送，PR 已开，CI 全部通过 · 还没在 GitHub 上看实际渲染效果 · 用户合并 #18 后，从待办第 1 项开始。
- 2026-10-23 · Claude · #17 · 修复 review 中的 P1 和 P2 问题；CI 失败的原因是拆分 commit 时漏提交了依赖文件，补交 `935a507` 后修复 · — · —
- 2026-10-23 · GPT（推测）· #17 · `2005d9c` 把 MCP 固定在 v1 API，并补了注册测试 · — · —
