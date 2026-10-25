# CLI Onboarding QA

日期：2026-10-24
执行者：Qoder（`codex/cli-onboarding-qoder` worktree）
基线：`7a093bd`（最新 `main`，包含 `f90a46e` / PR #21）。`backend/app/cli.py` 在 `f90a46e..7a093bd` 之间没有变化。
范围：`amc review` 的首次使用反馈、退出码、stdout/stderr 契约、JSON 机器输出、`cli.py` 文案稳定性。
不在范围：真实 Agent 推理（GLM 负责）、Web UI/后端服务、模型发现、README 与前端文件。

## 1. 测试方法

- 每条用例使用独立的临时目录和真实临时 Git 仓库。
- `XDG_CONFIG_HOME` 指向临时目录，清除 `AMC_MODEL`，避免本机配置和模型污染。
- review 执行使用受控 fake adapter：由 Python 子进程输出固定文本、失败或 sleep；**没有调用任何真实 Agent CLI，也没有网络请求**。
- fake adapter 通过 monkeypatch `app.sdk.get_adapter` 注入；未修改 SDK、执行器或配置层。
- 基线运行方式：`python -m app.cli`，并把 worktree 的 `backend/` 放到 `PYTHONPATH`。worktree 没有独立 venv，使用仓库旁边的测试解释器；这不改变被测代码。

复现环境示例：

```bash
cd /Users/robll/code/amc-cli-qoder
export PYTHONPATH=$PWD/backend
PY=/Users/robll/code/mission-control/.venv/bin/python
export XDG_CONFIG_HOME=/tmp/amc-cli-qa/xdg
unset AMC_MODEL
```

## 2. 基线结果（修复前）

| 场景 | 命令要点 | 实际结果 |
|---|---|---|
| `--help` | `amc --help` | exit 0；stdout 为 usage；stderr 空 |
| `review --help` | `amc review --help` | exit 0；stdout 为 usage |
| 仓库路径不存在 | `amc review --repo /missing` | **exit 1，stderr 裸 `FileNotFoundError` traceback**，stdout 空 |
| 路径不存在 + JSON | `amc review --repo /missing --format json` | **exit 1，traceback**，stdout 空，无法解析 JSON |
| 目录不是 Git 仓库 | `amc review --repo <plain>` | exit 1；错误和 emoji 打在 stdout |
| Git 仓库没有提交 | `amc review --repo <empty>` | **exit 0**；stdout 显示 `Verdict: failed` 和 `could not detect a base branch` |
| 干净仓库（无改动） | `amc review --repo <clean> --format json` | exit 0；**stdout 是警告文字而不是 JSON** |
| 无效后端名 | `--backend not-a-backend` | exit 1；误报为“`not-a-backend` is not on PATH”，未说明后端名无效 |
| 后端未安装 | PATH 中移除 opencode | exit 1；错误打在 stdout |
| 只有未跟踪改动 | fake adapter + `untracked.txt` | exit 0；审查正常执行（此路径原本正确） |
| Agent 失败 | fake adapter exit 3 | **exit 0**；stdout 显示 `Verdict: failed`，stderr 空 |
| 超时 | fake adapter sleep + `--timeout 1` | **exit 0**；stdout 显示 timeout 错误 |
| concerns + JSON + `--exit-code` | fake adapter 输出 1 个 finding | exit 1；**stdout 在 JSON 前混入 `Starting review...` 等进度** |
| concerns + JSON（无 `--exit-code`） | 同上 | exit 0；stdout 同样混有进度 |
| 路径含空格 | 临时仓库 `with space` | exit 0；审查正常执行（此路径原本正确） |

基线期间实际观察到的输出还包含 `✅/❌/⚠️/🔍/📋/🔎/💡` 等系统 emoji，且错误信息默认写 stdout。

## 3. 修复内容（仅 `backend/app/cli.py`）

1. **路径与环境预检查**
   - `--repo` 不存在、不是目录、不是 Git working tree、仓库没有提交时，输出明确原因和下一步提示，不再抛 traceback。
   - `git` 不可用时给出 `git_unavailable` 错误。
   - 裸仓库（`rev-parse --is-inside-work-tree` 输出 `false`）按非工作树处理。

2. **退出码契约**
   - `0`：审查完成且没有 concerns（或没有改动，见边界）。
   - `1`：审查完成，有 concerns 且指定了 `--exit-code`。
   - `2`：配置/路径/后端/执行错误、Agent 失败、超时、未预期异常。
   - `130`：用户 Ctrl+C 中断。
   - 错误不再因为未指定 `--exit-code` 而返回 0。

3. **stdout/stderr 契约**
   - `amc review` 的进度、警告、错误说明全部写到 stderr。
   - `--format json` 时 stdout 只有一份可解析 JSON；markdown 模式 stdout 只有最终报告。
   - JSON 通过 UTF-8 字节写入 stdout，避免 Windows 代码页导致编码错误。

4. **JSON 结构**
   - 正常审查：新增 `status: "reviewed"`；失败时 `status: "failed"` 并保证有 `error` 对象。
   - 预检查错误：`status: "error"`，`error.code` 为 `repo_not_found`、`not_a_git_repo`、`no_commits`、`unknown_backend`、`backend_unavailable`、`invalid_pr_url`、`unexpected_error` 等。
   - 无改动：`status: "no_changes"`，包含 `base`，exit 0。
   - 失败/超时的 `summary` 会回退为 `error.message`，便于机器读取。

5. **后端选中与安装提示**
   - 无效后端名（不在 adapter registry）与“后端有效但可执行文件未安装”分开报错。
   - 安装提示使用实际的后端描述、可执行文件名和已检测到的后端列表。
   - `amc start` 不再只检查 opencode，而是报告实际检测到的所有后端。

6. **首次配置与 emoji**
   - `cli.py` 内系统 emoji 全部替换为 `[OK]/[!]/[x]` 等 ASCII 标记。
   - `amc init` 没有可用后端时 exit 1，并给出安装与重试提示。
   - 配置文件写入失败时给出路径和 OS 错误，不再裸 traceback。

7. **Codex 验收补修（P2）**
   - 验收复现：`--rounds 0` 时 SDK 返回 `rounds_executed=0`、`verdict=inconclusive`、`error=no_result`，旧 JSON 却是 `status=reviewed`，而 CLI 已 exit 2。
   - `--rounds` / `--timeout` 必须是正整数；`0` 或负数分别返回 `invalid_rounds` / `invalid_timeout`，exit 2，JSON 仍可解析。
   - `_report_to_json` 改为 `report.error` 优先决定 `status`：SDK 返回 `inconclusive + error(no_result)` 时输出 `status=failed`，与 CLI 的 exit 2 保持一致。
   - 新增 inconclusive+error 的单元与 CLI 回归，不再只覆盖 `verdict=failed`。

## 4. 修复后实测

| 场景 | 结果 |
|---|---|
| `--help` / `review --help` | exit 0；usage 正常 |
| 路径不存在（markdown） | exit 2；stderr 说明路径和 `--repo PATH`；无 traceback |
| 路径不存在（JSON） | exit 2；stdout 为 `status=error, code=repo_not_found` 的 JSON；stderr 人类可读 |
| 非 Git 目录（JSON） | exit 2；`code=not_a_git_repo` |
| 无提交仓库（JSON） | exit 2；`code=no_commits` |
| 无改动（JSON） | exit 0；`status=no_changes`、`base=main`；stderr 提示 |
| 只有未跟踪改动 | exit 0；`status=reviewed`、`rounds_executed=1`，证明未跟踪文件进入审查 |
| 无效后端名（JSON） | exit 2；`code=unknown_backend`，stderr 列出支持的后端 |
| 后端未安装 | exit 2；`code=backend_unavailable`，stderr 给出安装提示 |
| Agent 失败（JSON） | exit 2；`status=failed`、`error.code=review_failed` |
| 超时（JSON） | exit 2；`error.code=timeout`，`summary` 含超时信息 |
| inconclusive + error（SDK no_result） | exit 2；`status=failed`、`verdict=inconclusive`、`error.code=no_result` |
| `--rounds 0` / 负数（JSON） | exit 2；`code=invalid_rounds` |
| `--timeout 0` / 负数（JSON） | exit 2；`code=invalid_timeout` |
| clear + `--exit-code` | exit 0 |
| concerns + `--exit-code` | exit 1；JSON 含 finding |
| concerns + JSON（无 `--exit-code`） | exit 0；JSON 可直接 `json.loads` |
| 路径含空格 / 非 ASCII 路径 + 中文 finding | exit 0；JSON 解析成功，中文 summary 保留 |
| markdown 模式 | stdout 只有报告，stderr 只有进度；无系统 emoji |

所有 JSON 场景都用 `json.loads(stdout)` 实际验证通过；stdout 中没有 `Starting review` 等进度文本。

## 5. 新增回归测试

`backend/tests/test_cli_onboarding.py` 共 23 项，覆盖：

- `--help` / `review --help`（含正整数约束）。
- 路径不存在、非 Git 目录、无提交仓库、无改动、只有未跟踪改动。
- 无效后端名与未安装后端的区分。
- Agent 失败、超时、`--exit-code` 的 0/1/2 语义。
- `inconclusive + error(no_result)` 的 status/JSON/退出码。
- `--rounds` / `--timeout` 为非正数时 JSON 可解析且 exit 2。
- JSON stdout 纯净性、中文/空格路径、markdown 无 emoji。
- 无效 PR URL、`amc init` 无后端时 exit 1。
- `cli.py` 源码不含系统 emoji 的防回归检查。

实际执行（从 worktree 运行，使用工作区外的测试解释器）：

```bash
cd /Users/robll/code/amc-cli-qoder
PYTHONPATH=backend /Users/robll/code/mission-control/.venv/bin/python -m pytest -q backend/tests/test_cli_onboarding.py
# 23 passed in 21.42s；退出码 0

PYTHONPATH=backend /Users/robll/code/mission-control/.venv/bin/python -m pytest -q
# 121 passed, 1 skipped in 41.16s；退出码 0
```

## 6. 未修项与边界

- **argparse 语法错误**：未知参数、非法 `--format` 值等仍由 argparse 处理，表现为 exit 2、stderr usage、stdout 为空；这条路径不会输出 JSON error 对象，因为参数解析发生在 CLI 逻辑之前。
- **真实 Agent 未验证**：所有 review 结果来自 fake adapter；真实 OpenCode/Claude/Codex/Copilot 的行为由 GLM 的实测覆盖。
- **Windows 未实际执行**：本机是 macOS。代码使用 `subprocess` 参数列表、`Path`、`shutil.which` 和 UTF-8 字节输出；测试覆盖含空格/非 ASCII 的路径，但没有 Windows 运行时证据。
- **无改动仍 exit 0**：保留原有“没有东西可审”语义；JSON 用 `status=no_changes`、`passed=true` 表示“无 finding 可报告”，不等同于“代码已审且通过”。`--exit-code` 也不会把它变成失败。
- **inconclusive 且无 error 时**：未指定 `--exit-code` 仍 exit 0；只有 `verdict=failed`、超时或显式 `error` 才 exit 2。没有改变 SDK 的 finding/verdict 定义。
- **Agent 非零退出没有 SDK error 对象**：`ReviewEngine.review` 返回 `verdict=failed` 但没有 `report.error`；CLI 在 JSON 中补 `review_failed` 并 exit 2。未修改 SDK。
- **未验证**：交互式 `amc init` 的完整选择流程（只测了“无后端”分支）；`amc start` 的启动/停止；真实 Web/SSE；模型发现；子模块快照。
- **未改变**：PR 模式的 GitHub 网络错误仍以 `create_failed` 形式经 CLI exit 2 报出；`--rounds` / `--timeout` 现在由 CLI 拒绝非正数，不再依赖 SDK 行为。

## 7. 结论

CLI 首次使用的路径/仓库/后端/执行失败现在都有可行动提示，错误退出码不再是 0；`--format json` 的 stdout 可被直接解析，进度和错误说明在 stderr；`cli.py` 不再输出系统 emoji。真实 Agent、Windows 运行时和 Web 端仍是本轮未覆盖的边界。
