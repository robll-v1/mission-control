# Web 冒烟验收清单

> 状态：**全部未执行。** 本工作只读取了 `5476de2b141de1c256212e7a08f25de910b7273a` 上的 App、Settings、API、CSS 与测试；没有启动或停止服务，没有调用模型，也没有产生新的 UI 截图。
> “预期”均来自当前代码，不等于实测结论。执行后必须填写实际结果与证据，不得直接把本清单视为通过记录。

## 1. 执行约定

### 状态取值

- **未执行**：尚未做；本文所有用例的初始状态。
- **通过**：实际结果符合预期，且有本次构建的证据。
- **失败**：实际结果不符合预期；保留原始输出，不修改预期来迁就结果。
- **阻塞**：缺少可调用的模型、端口、浏览器响应覆盖能力等；写明原因，不能算通过。

每项都记录：执行人、日期、代码 SHA、浏览器、实际后端端口、证据文件、实际结果。模型相关用例另记后端和真实模型 ID。

### 隔离前置环境

以下只是给执行者的模板，**本次未运行**。占位目录必须换成新的、可丢弃的路径，不能使用原工作区或真实私人仓库。

```bash
# 在独立 checkout 和新建临时目录中执行；不要照抄到正在使用的服务里。
cd "$AMC_SMOKE"                         # 指向被测版本的独立 checkout
export XDG_CONFIG_HOME="$SMOKE_TMP/xdg" # $SMOKE_TMP 必须是新建隔离目录
export PYTHONPATH="$AMC_SMOKE/backend${PYTHONPATH:+:$PYTHONPATH}"
export AMC_SMOKE_PYTHON="/path/to/python-with-project-dependencies" # 替换为已安装项目依赖的解释器
unset AMC_MODEL
"$AMC_SMOKE_PYTHON" -c 'from pathlib import Path; import app.cli; print(Path(app.cli.__file__).resolve()); print(Path(app.cli._root_dir()).resolve())'
# 先在本机确认模块文件位于 "$AMC_SMOKE/backend/app/cli.py"、root 等于 "$AMC_SMOKE"；不要把这两行含本机路径的输出录入证据。
python3 examples/first-review/make_demo_repo.py "$DEMO"
"$AMC_SMOKE_PYTHON" -m app.cli start --port 18000 --ui-port 15173 --no-browser
```

注意：

1. `python -m app.cli start` 的上述参数已用本基线 help 核对；**启动本身未执行**。不要用裸 `amc`，它可能仍指向全局 editable 安装。使用已有项目依赖的 Python 解释器，并先确认 `app.cli.__file__` 与 `_root_dir()` 都属于 `$AMC_SMOKE`。后端 `18000` 与前端 `15173` 是不同端口；启动前确认未被占用。端口冲突时 CLI 会另选端口，以终端和日志中的实际值为准。
2. 后端数据库 `data/amc.db` 与 `runtime` 使用相对路径（`backend/app/api/app.py:33-36`），所以必须从隔离 checkout 启动，才能用全新的数据库验证“无任务”。不要复用原工作区的数据，也不要停止已有服务。
3. `python -m app.cli start` 会将实际后端端口写入 `VITE_API_TARGET`（`backend/app/cli.py:399-417`）。若手工分别启动前后端，也要保证 `/api` 代理指向被测后端。
4. 使用与启动相同的 checkout、Python 解释器、`PYTHONPATH` 和 `XDG_CONFIG_HOME` 执行停止命令：`"$AMC_SMOKE_PYTHON" -m app.cli stop`。不要用裸 `amc stop`，也不要对无法确认归属的现有服务执行停止操作。
5. 独立的 `XDG_CONFIG_HOME` 只隔离 AMC 配置，不保证不读取私人状态：检测器仍可能读取 `$HOME` 下的 Codex/OpenCode 缓存，后端 CLI 可能复用已有登录。真实模型调用须遵守已确认的账号授权与调用预算；不要记录或展示缓存、令牌、账号及私人路径，证据和画面须脱敏。
6. `$DEMO` 只能由 fixture 生成，目标目录必须事先不存在。不要录入账号、密钥、cookie、请求头或私人路径；导出的 API 证据也要脱敏。
7. 真实运行、取消和第二轮复审需要先确认模型调用权限与预算。检测出模型不代表能调用；无法满足时将相关项标为**阻塞**。
8. 现有 `docs/screenshots/review-*.png`、`settings-*.png` 是历史素材，**不得作为本基线的通过证据**。

### UI 响应覆盖的用途与边界

W03–W06、W10A 可用浏览器 DevTools 的响应覆盖（例如 Network → Override content）或本地代理构造响应。它们只验证**前端呈现与错误处理**，不证明真实检测、保存或执行后端通过。浏览器不支持覆盖时记为阻塞，不改产品源码来制造状态。

统一覆盖数据（仅用于对应用例）：

```json
// GET /api/config/detect：来源未知且列表为空
{"backends":[{"name":"opencode","version":"ui-fixture","models":[],"command":"opencode","model_count":0,"grouped_models":null,"models_source":"unknown","models_note":"UI fixture","models_call_verified":false}]}

// GET /api/config/current：已保存模型不在枚举列表中
{"config":{"backend":"opencode","model":"saved-model","variant":null}}

// GET /api/config/detect：一个 CLI 都未检测到
{"backends":[]}
```

## 2. 页面与任务

### W01 无任务空状态

- **前置**：全新的隔离数据库；`GET /api/tasks` 返回 `[]`。
- **步骤**：打开 Web 首页，不选择任务。
- **预期**：左侧计数为 0，显示“还没有审查任务”；主区域显示“选择一个审查任务”；不出现伪造的 verdict 或 finding。依据：`frontend/src/App.tsx:657`、`:669-674`。
- **证据位**：`W01-empty.png`；脱敏后的 `GET /api/tasks` 响应。
- **实际结果**：（待填）
- **执行状态**：**未执行**

## 3. 模型选择与配置

### W02 后端默认模型与已保存配置

- **前置**：至少检测到一个后端；隔离配置中没有旧的个人选择。
- **步骤**：
  1. 打开“模型选择”，确认标题不是“智能模型选择器”。
  2. 选择一个已检测后端，模型选“后端默认模型”，保存。
  3. 仅当检测到至少两个后端时，才改选另一个后端再切回；随后刷新页面并重新进入设置。
- **预期**：默认模型对应空模型值；若模型列表非空，选项提示“不指定，由后端自己决定”，若模型列表为空，则提示“这个后端没有提供可读取的模型列表”。保存后提示“已保存。之后发起的审查会使用 …”；已保存后端显示“当前” pill；执行了跨后端切换时，切回后恢复其已保存模型；刷新后选择仍在，未改动时“保存配置”禁用。依据：`frontend/src/Settings.tsx:97-123`、`:125-145`、`:255`、`:306-307`、`:348-366`；API：`backend/app/api/routes_config.py:85-165`。
- **证据位**：`W02-default.png`、`W02-saved.png`、`W02-reload.png`；脱敏后的 `/api/config/current` 响应。
- **实际结果**：（待填）
- **执行状态**：**未执行**

不得据此声称默认模型一定可调用；配置保存与模型权限是两件事。

### W03 模型来源未知、列表为空，但保留已保存模型

- **前置**：按第 1 节覆盖 detect 为“unknown + 空列表”，current 为 `saved-model`。
- **步骤**：打开“模型选择”，查看来源说明和模型选项；不做真实保存。
- **预期**：显示“未能读取模型列表；仍可选择后端默认模型或保留已配置的模型。”及“列表不代表账号调用权限…”；同时存在“后端默认模型”和 `saved-model` 选项，后者提示“当前配置，不在检测列表中”。依据：`frontend/src/Settings.tsx:46-61`、`:106`、`:286-320`。
- **证据位**：`W03-unknown-source.png`；所用覆盖响应副本。
- **实际结果**：（待填）
- **执行状态**：**未执行**

真实检测侧的对应单元测试包括 `test_opencode_without_any_source_reports_unknown_but_present`、`test_unknown_model_list_does_not_clear_explicit_config`、`test_no_enumeration_source_ever_claims_verified_calls`（`backend/tests/test_backend_detector.py`）。这些单元测试不能替代本 UI 用例。

### W04 未安装任何受支持 CLI

- **前置**：仅将 detect 覆盖为 `{"backends":[]}`。
- **步骤**：打开“模型选择”。
- **预期**：显示“没有检测到可用的 Agent CLI”和四个安装提示，不显示模型列表或保存栏。依据：`frontend/src/Settings.tsx:33-38`、`:198-218`。
- **证据位**：`W04-no-backend.png`。
- **实际结果**：（待填）
- **执行状态**：**未执行**

如未来要在真实无 CLI 环境复测，应另建隔离环境；真实“缺少 CLI 则后端缺席”的单元证据是 `test_missing_cli_means_backend_absent`，不能用本响应覆盖冒充端到端安装验证。

### W05 已保存后端当前未检测到

- **前置**：detect 返回至少一个后端，但**不包含** current 中保存的后端；current 使用 `{"config":{"backend":"saved-away","model":"saved-model","variant":null}}`。
- **步骤**：打开“模型选择”，观察顶部警告与保存按钮。
- **预期**：显示“已保存的后端「saved-away」当前没有检测到。请先安装它，或在上方改选其他后端。”；由于该后端不在检测结果中，保存按钮不可用，直到改选一个已检测后端。依据：`frontend/src/Settings.tsx:268-272`、`:360-366`。
- **证据位**：`W05-saved-backend-missing.png`；detect/current 覆盖响应。
- **实际结果**：（待填）
- **执行状态**：**未执行**

### W06 配置加载失败

- **前置**：让 `/api/config/detect` 返回非 2xx（或阻断请求），current 可正常返回。
- **步骤**：打开“模型选择”；记录错误；随后恢复响应并刷新页面。
- **预期**：加载骨架结束后显示 `role="alert"` 的错误 banner；非 2xx 时文案为“无法读取配置，请确认后端服务正在运行”；不展示可操作的后端/模型/保存栏。恢复并刷新后应重新加载。依据：`frontend/src/Settings.tsx:90-102`、`:165-196`。网络级失败文案依浏览器而异，不要求逐字一致。
- **证据位**：`W06-config-load-error.png`；Network 状态；`W06-config-recovered.png`。
- **实际结果**：（待填）
- **执行状态**：**未执行**

## 4. 创建本地审查

### W07 创建本地审查

- **前置**：W02 已保存本次要用的后端/模型；`$DEMO` 已生成且含未提交的合成 SQL 注入；未启动任何 review。
- **步骤**：点“新建审查”→“本地变更”→填 `$DEMO`→基准分支留空→“创建审查”。
- **预期**：弹窗关闭并选中新任务；标题以 `[Local Review]` 开头（可能附带最近提交信息），来源显示 `本地变更 · main`（fixture 初始化为 `main`；留空时按 `main` → `master` → `origin/main` → `origin/master` 顺序自动探测，见 `backend/app/services/local_review_service.py:24-37` 的 `resolve_base_branch`），状态/阶段为“待审查”；“开始首轮审查”可用、“中止”禁用；还没有轮次和结构化结论。`context/local_diff.patch` 出现在产物中。依据：`frontend/src/App.tsx:317`（来源文案）、`:490-524`、`:942-1024`，`backend/app/api/routes_tasks.py:115-165`，`backend/app/services/local_review_service.py:177-219`。
- **证据位**：`W07-created.png`；脱敏后的 task detail JSON（含 `local_snapshot_sha`、`review_rounds`、artifacts）。
- **实际结果**：（待填）
- **执行状态**：**未执行**

补充校验：创建阶段只生成快照和上下文，**不等于已调用模型**。若路径不存在或基准分支未知，应显示后端返回的错误；这类负面用例如执行，另建记录，不覆盖本项结果。

## 5. 运行、取消与失败

### W08 真实运行与运行中取消

- **前置**：W07 创建成功；本次模型已确认可调用且有预算；先另跑一轮允许完成的审查，确认 Web 能收到真实结果。取消测试不得挤占未批准的调用。
- **步骤**：开始一轮审查；等待 start 请求完成且状态进入“审查中”；点击“中止”；持续观察至 worker 收尾。
- **预期**：运行中“开始”禁用、“中止”可用；成功取消后任务为“已中止”，运行按钮恢复可用；最终状态不能被迟到的 worker 改写成 failed/completed。取消轮不得因半截输出显示“未发现明显问题”；若展示其结果，应为“结论不明确”。依据：`frontend/src/App.tsx:526-546`、`:569-570`、`:703-721`，`backend/app/core/execution.py:224-269`。
- **证据位**：`W08-running.png`、`W08-aborted.png`；run/task JSON；事件中含 `control.accepted`。
- **实际结果**：（待填）
- **执行状态**：**未执行**

若本轮太快而来不及取消，记为**阻塞**，不要为了截图反复消耗额度。单元回归证据：`test_abort_survives_worker_finalisation`、`test_abort_during_result_parsing_is_not_lost`、`test_cancelled_output_cannot_certify_clear`；它们不是本次 Web 实测。

### W09 preflight 边界

- **前置**：同 W08，但只观察，不假设可以取消。
- **步骤**：点击开始后，在 start 请求仍 pending、任务尚未进入 running 时观察按钮和任务状态；等请求返回后再记录。
- **预期**：preflight 在创建 Run 和设置 running 之前同步执行（`backend/app/core/execution.py:115-121`、`:183-222`），所以这段时间“中止”可能仍禁用；不能把它判成“运行中取消失败”。已知 preflight 最长等待 150 秒且没有取消路径。若 preflight 抛错，API 返回 409，通常还没有新 Run；不要预设任务一定进入 failed。
- **证据位**：`W09-preflight-timing.png`；请求起止时间、任务/轮次 JSON。
- **实际结果**：（待填）
- **执行状态**：**未执行**

### W10 启动失败与执行失败

**W10A：确定性的 UI 错误呈现（无模型）**

- **前置**：将 `POST /api/tasks/{id}/start` 覆盖为 HTTP 409，body 为 `{"detail":"synthetic start failure"}`。
- **步骤**：点击开始；等待超过 4 秒的任务列表自动刷新周期。
- **预期**：页面显示 `synthetic start failure` 错误 banner，刷新不会把它静默清掉；不得显示成功或新结论。依据：`frontend/src/App.tsx:120-137`、`:430-441`、`:526-535`。
- **证据位**：`W10A-start-409.png`；覆盖响应。
- **实际结果**：（待填）
- **执行状态**：**未执行**

**W10B：真实失败（仅在实际发生时记录）**

- **前置**：不通过破坏用户环境来制造失败；若获批的隔离运行自然失败，保留原始输出。
- **步骤**：记录失败发生在 preflight、准备阶段还是 worker 执行阶段，再核对 UI。
- **预期**：准备阶段失败应为 failed / `review_prepare_failed`；worker 失败应为 failed / `review_failed`。preflight 失败遵循 W09，不强行要求相同状态。依据：`backend/app/core/execution.py:121-168`、`:247-269`，`backend/app/api/routes_control.py`。
- **证据位**：`W10B-failure.png`；原始事件与 run JSON。
- **实际结果**：（待填）
- **执行状态**：**未执行**

## 6. 结果、路径与轮次语义

### W11 finding 路径与缺失位置

- **前置**：已有本次真实首轮结果；不要用旧 JSON 冒充本次输出。
- **步骤**：打开“审查结果”，逐条核对 severity、`path:line`、summary/detail；与实际 patch 中的文件对照。若某条没有 path，观察其呈现。
- **预期**：有路径时显示为 `path:line`，缺路径时显示“未指明位置”；路径应相对仓库且可被核对，不能把模型文本未验证地当成真实位置。此前真实首轮曾报到 `notes.py:24` 的 SQL 注入，但新一轮不保证复现，必须记录实际值。渲染依据：`frontend/src/App.tsx:801-829`。
- **证据位**：`W11-findings.png`；原始结果 JSON；对应 diff。
- **实际结果**：（待填）
- **执行状态**：**未执行**

### W12 Web 最新一轮与 SDK 增量的边界

- **前置**：非空修复 diff 的真实第二轮已经按发布门槛验收；保存两轮原始结果。
- **步骤**：分别查看“审查结果”和“审查轮次”；对比两轮的 verdict/findings。若另跑 CLI/SDK 增量，单独保存其报告并标注入口。
- **预期**：Web 主结果区展示最新一轮结果，轮次页保留各轮自己的卡片；不得要求 Web 出现 SDK 的 new / persistent / unverified 聚合标签，也不得把“没再提到”自动显示成“已修复”。依据：`frontend/src/App.tsx:423-428`、`:749-829`、`:863-917`；`backend/app/services/review_result_service.py` 的 `latest_result` 与 `backend/app/sdk.py` 的 incremental 分支是两条不同路径。
- **证据位**：`W12-latest-web.png`、`W12-rounds.png`；两轮原始 JSON；可选的 SDK 报告须独立命名。
- **实际结果**：（待填，**禁止预测第二轮结果**）
- **执行状态**：**未执行**

## 7. 视口与主题

### W13 375px 移动视口

- **前置**：已有任务详情、finding 和设置页数据；浏览器 DevTools 设为 **375 × 812**，DPR 可记录但不作要求。
- **步骤**：
  1. 首页开关任务列表；选择任务；切换四个 tab。
  2. 打开新建审查弹窗，检查两个来源、输入框与底部按钮。
  3. 打开设置页，滚动到模型列表和保存栏。
  4. 在控制台记录 `document.documentElement.scrollWidth`。
- **预期**：无非预期横向溢出（通常 `scrollWidth <= 375`）；侧栏以抽屉/遮罩形式开关；标题、tabs、finding 路径、弹窗按钮、设置保存栏不重叠且可操作。900px 和 560px 断点依据：`frontend/src/styles.css:1025-1067`、`:1339-1344`。**CSS 没有专门的 375px 断点**，所以必须以实际画面为准，不能仅凭断点存在判通过。
- **证据位**：`W13-375-home.png`、`W13-375-modal.png`、`W13-375-settings.png`；scrollWidth 记录。
- **实际结果**：（待填）
- **执行状态**：**未执行**

### W14 亮色与暗色主题

- **前置**：同一任务、同一结果、同一视口；不要混用旧截图。
- **步骤**：用顶栏按钮切换亮/暗主题；检查 verdict、severity chips、finding、错误 banner、设置选项和保存栏；刷新页面。
- **预期**：`html[data-theme]` 与 `localStorage['amc-theme']` 同步切换，刷新后保持；两种主题文字与控件均可读、状态颜色可区分、无内容消失。依据：`frontend/src/App.tsx:399-410`、`:597-604`。无保存值时初始主题还会参考 `prefers-color-scheme`，不要把它误认为用户选择已持久化。
- **证据位**：`W14-light.png`、`W14-dark.png`、`W14-dark-reload.png`。
- **实际结果**：（待填）
- **执行状态**：**未执行**

## 8. 汇总表（交付时逐项更新）

| 用例 | 覆盖点 | 当前状态 | 证据 |
|---|---|---|---|
| W01 | 无任务空状态 | 未执行 | 无 |
| W02 | 后端默认模型与已保存配置 | 未执行 | 无 |
| W03 | 来源未知、空列表、保留已保存模型 | 未执行 | 无 |
| W04 | 未安装任何 CLI | 未执行 | 无 |
| W05 | 已保存后端未检测到 | 未执行 | 无 |
| W06 | 配置加载失败 | 未执行 | 无 |
| W07 | 创建本地审查 | 未执行 | 无 |
| W08 | 真实运行与运行中取消 | 未执行 | 无 |
| W09 | preflight 不可取消边界 | 未执行 | 无 |
| W10A/B | 启动错误与真实失败分阶段 | 未执行 | 无 |
| W11 | finding 路径与缺失位置 | 未执行 | 无 |
| W12 | Web 最新轮 vs SDK 增量 | 未执行；非空 diff 的第二轮真实复审未验收 | 无 |
| W13 | 375px 移动视口 | 未执行 | 无 |
| W14 | 亮暗主题与持久化 | 未执行 | 无 |

验收完成的定义：每项都有本次构建的实际结果和证据；阻塞项有明确原因；失败项有独立问题记录。代码审阅、单元测试数量、旧截图和“预期符合代码”都不能替代这些实测。
