# Clean Install and First-Start QA

日期：2026-10-24
执行者：Qoder（`codex/install-smoke-qoder`）
基线：`5476de2b141de1c256212e7a08f25de910b7273a`（`codex/launch-acceptance`）
工作树：`/Users/robll/code/amc-install-qoder`
代码提交：`e4fa875`（bootstrap 生命周期/安装失败处理）、`1a75538`（优雅停止文案）
范围：干净安装、首次启动、`start/health/status/stop`、隔离环境、安装失败与重复启停的离线回归。
不在范围：真实 Agent/AI 调用、Web 业务操作、模型发现、Windows 实机、GLM 独占的 `execution.py` / `routes_control.py` / preflight 测试。

## 1. 安装与隔离方法

- 用 `git archive HEAD` 从准确代码提交生成全新源码副本，不使用工作区未提交文件。
- 在该副本内新建 `.venv`，只按 `pyproject.toml` 执行 `pip install -e .`；未使用遗留 `backend/requirements.txt`。
- `HOME`、`XDG_CONFIG_HOME`、`PIP_CACHE_DIR`、npm cache 全部指向临时目录。
- `AMC_ROOT` 指向临时源码副本，确保 `.venv`、`.run`、`data/`、`runtime/` 都不落到用户 checkout。
- 选择独立端口：后端 `18140`，UI 验收 `18144/18145`；不使用用户默认端口。
- 未执行任何真实 Agent、模型或 GitHub 请求。
- 未对用户工作区或其他 worktree 执行 `amc stop`。

最终验收入口：

```text
/tmp/amc-install-smoke-qoder-v4/src
```

安装返回码（直接读取）：

```text
python3 -m venv .venv                         exit 0
.venv/bin/python -m pip install -U pip        exit 0
.venv/bin/python -m pip install -e .          exit 0
.venv/bin/amc --help                          exit 0
.venv/bin/amc start --help                    exit 0
AMC_ROOT=<src> python -c '... _root_dir()'    -> /tmp/amc-install-smoke-qoder-v4/src
```

默认（editable 安装）与显式 `AMC_ROOT` 都解析到临时源码副本；未解析到用户原 checkout。

## 2. 基线复现（修复前）

基线 `5476de2` 的干净安装本身成功，但后端启动/停止有以下可复现问题：

| 场景 | 实际结果 |
|---|---|
| `amc start --no-ui --no-browser --port 18100` | 后端健康 `{"ok":true}`，`status` exit 0 |
| `amc stop` | exit 0，端口关闭，`.run` 删除 |
| `amc start` 前台进程 | **stop 后 40×0.25s 仍未退出**，只能手动 kill；原因是子进程未被 `Popen.poll()` 回收，`_pid_alive` 对僵尸仍为真 |
| 安装失败（venv/pip/npm） | `check=True` 直接抛 `CalledProcessError`/traceback，且可能留下半成品 `.venv`/`node_modules` |
| 启动失败（健康检查超时） | `_fail` 退出后已启动的 backend 可能继续运行 |
| 重复 start | 未检查已有 live PID，可能启动第二套服务并覆盖 pid 文件 |
| UI 停止 | 只 kill npm leader，vite 子进程可能残留 |
| `amc stop` 优雅退出后 | 后端已退出但打印“No running services found”，与事实不符 |
| `AMC_ROOT` | 无覆盖入口；非 editable 安装时 `_root_dir()` 可能落到 site-packages |

## 3. 修复内容（仅 `backend/app/cli.py`）

- `amc start` / `stop` / `status` 使用 `.run/backend.json`、`.run/frontend.json` 中的 PID、进程启动时间、命令行、源码根目录及 Unix session/process-group 信息识别服务；裸 PID 和旧版 PID 文件不再作为发送信号的凭据。
- 启动前先核对注册服务；活进程无法验证、状态损坏或来源 checkout 不符时拒绝重复启动，并提示手动检查。
- 启动异常时，只清理本次可验证的服务状态与进程；日志以追加方式写入并在 stop/启动失败后保留，未知 `.run` 文件不做递归删除。
- Unix 只有确认命令行、启动时间以及 `session_id == pgid == PID` 后才向该进程组发 SIGTERM；TERM 后再次查身份，只有身份仍匹配才可能发 SIGKILL。身份变化或查询失败时拒绝升级信号。
- Windows 需要 PowerShell/CIM 进程信息和可靠的父进程信息；身份或 `taskkill` 查询失败时保留状态并拒绝兜底粗杀。
- HTTP `/api/admin/shutdown` 仅在 backend 状态有效、`ports.json` 端口与状态一致，且本机监听 PID 查询确认该端口归 backend PID 时调用；缺少 `lsof` / PowerShell TCP 查询能力时跳过 HTTP，继续走已验证信号路径。
- `_ensure_venv()` / `_ensure_frontend_deps()` 捕获安装失败，返回 exit 1 并给出可行动提示；失败时删除本次生成的半成品 venv/node_modules。
- 已有 `.venv` 缺少 `uvicorn` 时给出明确错误，不继续启动。
- Node 存在但 npm 缺失时只警告并跳过前端，不再先启动后端再失败退出。
- 增加 `AMC_ROOT` 源码根覆盖，并校验 `pyproject.toml`。
- `amc stop` 把成功优雅退出的 backend 计入“已停止”，文案改为 “exited after graceful shutdown”。
- `_info/_ok/_warn/_eprint` 增加 flush，后台运行时启动日志不会滞留在缓冲区。

## 4. 最终安装验收（准确代码提交）

### 4.1 后端模式

```text
amc start --no-ui --no-browser --port 18140   -> 健康 200 {"ok":true}
amc status                                    -> exit 0，backend running/responding
amc stop                                      -> exit 0，backend exited after graceful shutdown
amc start 进程                                -> exit 0，自动退出
端口 18140                                    -> 已释放
.run                                          -> 已删除
临时源码残留进程                              -> none
```

启动输出包含 `Mission Control is running!`，stderr 只有无 token 的警告；无 traceback。

### 4.2 UI 模式

```text
amc start --no-browser --port 18144 --ui-port 18145
```

```text
npm install（临时副本）                       -> 成功，frontend/node_modules 仅存在于临时副本
backend /api/health                           -> 200 {"ok":true}
frontend /                                    -> 200
frontend /api/health（Vite proxy）            -> 200 {"ok":true}
amc status                                    -> exit 0，backend + frontend running/responding
amc stop                                      -> exit 0，backend graceful + frontend stopped
amc start 进程                                -> exit 0
端口 18144 / 18145                            -> 均释放
.run                                          -> 已删除
临时源码残留进程                              -> none
```

源码 checkout 未生成 `frontend/node_modules`，`frontend/package-lock.json` 无改动；npm 安装仅发生在临时副本。

## 5. 离线 bootstrap 回归

新增 `backend/tests/test_cli_bootstrap.py`，使用临时源码根、fake `Popen` / `subprocess.run`，不启动真实服务：

1. `AMC_ROOT` 有效路径 / 无效路径。
2. venv 安装失败时 exit 1、清理半成品 `.venv`。
3. 已存在但不完整的 `.venv` 被明确拒绝。
4. npm 安装失败时 exit 1、清理半成品 `node_modules`。
5. 已有 live PID 时拒绝重复 start。
6. 无 Node、无 Agent CLI 时跳过前端并正常启动后端。
7. 健康检查或前端启动失败时，只回收已验证的本次服务状态并保留日志。
8. 优雅 shutdown 后 stop 文案与清理正确。
9. 旧 PID 文件和损坏/空签名状态拒绝信号；旧记录的 PID 即使存活也不能代替身份凭据。
10. PID 复用、checkout 根目录不符、身份查询超时、TERM 后身份变化时拒绝误发信号或升级到 SIGKILL。
11. 端口监听归属不匹配时不调用 HTTP shutdown；健康检查失败后保留日志与已有文件。
12. 正常启动/停止生命周期用 fake Popen、进程身份和信号验证；测试不启动服务或杀真实进程。

实际执行（直接读取退出码）：

```bash
cd /Users/robll/code/amc-install-qoder
PYTHONPATH=backend /Users/robll/code/mission-control/.venv/bin/python -m pytest -q \
  backend/tests/test_cli_onboarding.py backend/tests/test_cli_bootstrap.py
# 33 passed in 33.94s；退出码 0

PYTHONPATH=backend /Users/robll/code/mission-control/.venv/bin/python -m pytest -q
# 157 passed, 1 skipped in 68.83s；退出码 0
```

## 6. 未验证项与边界

- **Windows 实机未运行**：`taskkill /T`、`.venv/Scripts/*.exe`、非 Unix 的 `os.getpgid` 分支未实测。
- **非 editable 安装未实测**：本轮按 README 使用 `pip install -e .`；`pip install .` 后的默认根路径仍依赖 `AMC_ROOT` 覆盖。
- **真实 Agent/AI 未调用**：无模型推理、无真实审查 E2E。
- **并发重复 start 竞态**：已在 pid 文件写入后拒绝重复启动；两个 start 在 PID 写入前的极端竞态未覆盖。
- **stale/foreign PID**：PID 复用、根目录不符、坏状态、命令不匹配和身份查询失败均拒绝信号；无法验证时需手动检查。
- **实际安装失败**：venv/npm 失败路径用 mock 覆盖；本轮真实 PyPI/npm 安装成功，未人为断网。
- **启动失败清理**：健康检查失败路径为单元 mock；未在真实端口上故意制造 30s 健康失败。
- **交互式 `amc init`**、Web 业务操作、模型发现、子模块快照不在本轮范围。

## 7. 清理

- 所有 `amc start`/`amc stop` 都限定在临时源码副本的 `.run`，未触碰用户服务。
- 两次后端启停和一次 UI 启停后：端口均释放、`.run` 删除、临时源码下无残留进程。
- 临时安装目录（`/tmp/amc-install-smoke-qoder*`）在验收完成后删除；清理证据为上述端口/进程/.run 检查。

## 8. 后续身份与日志边界复核（2026-10-26）

上面的真实安装记录对应旧提交 `1a75538`，不表示本节未提交修复在真实服务环境中已通过。新增离线覆盖只用 mock `Popen`、身份探测、端口所有者和信号；没有启动服务、发送真实信号或调用 Agent。

- 服务状态必须包含非空签名，并与当前 checkout、服务类型、端口及实际命令行吻合；旧 `.pid`、空/损坏 JSON、root 不符或身份查询失败均不会作为发信号依据。
- 健康检查、前端启动或进程身份登记失败后只尝试回收本次创建且可证明的子进程/服务组；日志和 `.run` 中的其他文件会保留。重复启动不会截断旧日志。
- `stop` 只移除确认已停止或已过期的服务状态和端口文件，不递归删除 `.run`；无法验证的状态会保留供人工检查。
- 当前实现复验：CLI focused `48 passed`；全量后端 `172 passed, 1 skipped`，未运行真实服务、未发真实信号。真实 Windows、端口工具缺失/权限限制、PID 在同一秒内复用，以及 SIGTERM 后身份查询失败的行为未做实机验证；遇到不可验证状态会选择拒绝信号。
- 原工作区、Codex 整合 worktree、GLM/WorkBuddy worktree 和 stash 均未修改。

## 9. Current-main migration and lifecycle acceptance (2026-11-12)

Base: `main` / `5f1c7c23d081879382af1e29fb9de1cc0699d112`.
The CLI lifecycle/bootstrap candidate and its tests were migrated from the old
`amc-install-qoder` worktree onto this base. The original worktree and its
uncommitted changes were preserved. PR #25's non-empty-model `amc init`
behaviour is retained; the old CLI file was not used to overwrite that fix.

The first real isolated backend + Vite shutdown failed despite the offline
suite passing: a TERM-exited child briefly lost its readable process identity
before its parent reaped it. Stop treated that transient state as a permanent
failure. A regression test reproduced the failure before the correction.
Stop now waits within its existing bounded interval without sending another
signal; SIGKILL still requires a fresh complete identity match.

Two consecutive real start/status/stop cycles then passed on macOS arm64:

- Each cycle used this checkout's code, an isolated XDG config, and fresh
  localhost backend/UI ports. The installed Python environment was reused
  through a symlink; frontend dependencies were copied locally. There was no
  package download or clean-environment installation in this acceptance.
- Backend `/api/health`, the Vite page, and its `/api/health` proxy returned
  HTTP 200. Status verified both service identities.
- Stop and the foreground start process both exited 0. Both ports closed,
  service JSON/PID/ports state was removed, and backend/frontend logs remained.
- No real review agent or model was called. No browser W08/W09 check was run.

Focused verification (65 passed, exit 0; Python 3.14.7):

```bash
PYTHONPATH=backend PYTHONDONTWRITEBYTECODE=1 /Users/robll/code/mission-control/.venv/bin/python -m pytest -q -p no:cacheprovider backend/tests/test_cli_bootstrap.py backend/tests/test_cli_onboarding.py backend/tests/test_config_layers.py
```

Windows process/CIM/taskkill paths remain mock-only, with no new native Windows
or CI result claimed. Two simultaneous starts can still pass the pre-registration
check because there is no cross-process launch lock; this acceptance does not
claim that race is fixed. The execution service's probe/review descendant-tree
cleanup is a separate workstream, not part of this CLI patch.

Final full suite on the migrated candidate: `207 passed, 1 skipped`
(Python 3.14.7, 34.81s, exit 0). A separate real-process canary check also
confirmed that legacy PID-only records, corrupt JSON and mismatched start
identity each make stop exit 1 while the canary stays alive and all state/log
bytes remain unchanged. The harness reaped its own canary afterward.

After copying the three candidate files byte-for-byte to the primary checkout's
`codex/cli-lifecycle-safety` branch, the full suite was repeated there:
`207 passed, 1 skipped`, 34.10s, exit 0. No commit, push or new CI run is claimed.
