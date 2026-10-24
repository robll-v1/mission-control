# 发布文案包（Launch Pack）

面向**已经在用 AI 编程 CLI**的开发者，可直接审稿后发布。

- 代码基线：`7a093bd`（`main`，已包含 PR #21 / `f90a46e` 的修复），后端 `98 passed, 1 skipped`。本文源码行号均按该基线标注。
- 本文只写**能从仓库代码或测试里指出位置**的能力。每条承诺都标注证据等级：**已验证** / **实现支持但未实机验证** / **规划**。
- 卖点是**稳定的输入快照、结构化结论、跨轮追踪**。不靠"你的 agent 审不了未提交代码"这类错误对比做钩子——现有 coding agent 本来就能读未提交内容。
- 不含真实用户评价、速度提升、百分比收益。合成仓库首轮结果已实测；修复后的非空 diff 复审仍未验收，不得提前引用其跨轮结果。
- 不重写 README（README 首屏由 #18 负责），不重复截图。

---

## 1. 一句话定位

**English**

> Mission Control turns the coding agent CLI you already run into a pre-push reviewer: one frozen snapshot of your working tree in, structured findings you can track across rounds out.

**中文**

> 用你已经在用的 AI 编程 CLI 审查本地改动：输入是冻结的快照，结论结构化、可跨轮追踪，不动你的 index 和 HEAD。

备选（更强调"本地编排，不是又一个审查 SaaS"）：

> A local review console that drives the agent CLI you already have, instead of selling you another one.

---

## 2. 三个有实现证据的价值点

### 价值点一：复用你已安装的 Agent，由框架负责编排与解析

Mission Control 不自己调模型。它把 prompt 交给本机已有的 agent CLI（OpenCode / Claude Code / Copilot / Codex）执行，自己只负责编排、上下文、解析与追踪。

| 项 | 内容 |
|---|---|
| 代码证据 | `backend/app/adapters/__init__.py`（注册表 `_REGISTRY` + `_auto_register`）、`backend/app/adapters/` 下四个 `*_adapter.py`、`backend/app/core/execution.py:115-121`（按轮构造 adapter） |
| 测试证据 | `backend/tests/test_adapters_preflight.py:19` `test_executable_matches_launched_program`、`:31` `test_known_executables_are_not_the_backend_key` |
| 证据等级 | **已验证（单元层）**：注册表与可执行文件名一致，preflight 报的是程序名而不是后端 key |
| 未验证 | 四个 CLI 的**真实两轮 E2E**未在本机跑完；Windows 上的 CLI 启动只由 CI 矩阵覆盖 |
| 可以怎么说 | "drives the agent CLI you already have"；"auth, model access and quota stay with that CLI" |
| 不要怎么说 | "works with any model" / "every model you have"（模型枚举依赖各 CLI，见 §6）；"no extra cost"、"free"、"no new key"——登录方式、模型权限和订阅额度都由那个 CLI 决定，框架无权承诺 |

### 价值点二：审查的是冻结后的工作区快照，不是一段拼接出来的 diff

本地模式下，框架把真实 index 复制到一个临时 `GIT_INDEX_FILE`，`add -A` → `write-tree` → `commit-tree`（父提交 = HEAD），得到一个快照 commit；worktree 从这个 sha 建。因此 **patch 描述的内容**和 **agent 能打开的文件**是同一个 commit 的两种视图，天然同源。

| 项 | 内容 |
|---|---|
| 代码证据 | `backend/app/services/local_review_service.py:76-167` `capture_local_snapshot`（`GIT_INDEX_FILE` 在 `:91`，复制真实 index 在 `:104-105`，`commit-tree` 在 `:140`）、`backend/app/core/worktree.py:146-161` `_resolve_revision`、`backend/app/core/execution.py:699-741` `_refresh_local_snapshot` |
| 测试证据 | `backend/tests/test_local_snapshot.py:24` `test_snapshot_uses_final_worktree_without_touching_real_index`（断言 `write-tree` 与 `diff --cached --binary` 前后不变）、`:90` `test_worktree_and_compiled_context_use_the_captured_snapshot`、`:157` `test_execution_refreshes_local_snapshot_each_round` |
| 证据等级 | **已验证**：真实 git 仓库上跑过，覆盖暂存后又改动的同一文件、重命名、空格与中文路径、删除文件、被 ignore 但 force-add 的文件 |
| 未验证 | 捕获过程**不是原子事务**：`add -A` 经 `write-tree` 到 `commit-tree` 之间若有并发写入，快照可能新旧混杂。能保证的是**快照生成之后**的一致——patch、快照内容、agent 打开的文件指向同一个 commit。子模块内部未提交内容不在父仓库快照内 |
| 可以怎么说 | "one frozen snapshot of your working tree"；"your index and HEAD are never touched" |
| 不要怎么说 | "reviews exactly what is on disk"（是**捕获那一刻**的磁盘状态）；"sandboxed"（PATH shim 只是护栏，见 §6） |

### 价值点三：结论是结构化的，而且下一轮不会替你"宣布修好"

在 **SDK 增量路径**（`incremental=True` 且存在本地历史）下，finding 带 severity；只有 agent 提供且可解析的位置才会有 `file:line`，并不保证每条都有。跨轮状态为 `new` / `persistent` / `unverified`：再次被提到是 `persistent`，没被提到才是 `unverified`——后者不等于 resolved。Web 界面不走这条聚合逻辑，只看最新一轮。

| 项 | 内容 |
|---|---|
| 代码证据 | `backend/app/services/review_result_service.py:173-205` `_parse_findings`、`backend/app/core/models.py:43-50` `ReviewResult`、`backend/app/services/review_history.py:185-238` `compare_findings`、`backend/app/sdk.py:494-520`（unverified 并入报告并可把 `clear` 抬成 `concerns`） |
| 测试证据 | `backend/tests/test_result_service.py:22` `test_parses_severity_path_and_line`、`backend/tests/test_sdk_and_db.py:135` `test_incremental_clear_keeps_unmatched_prior_finding`、`backend/tests/test_local_snapshot.py:71` `test_unmatched_findings_remain_unverified_across_incremental_rounds` |
| 证据等级 | **已验证（SDK/增量路径）**：`incremental=True` 且存在本地历史时成立 |
| 边界 | Web 界面显示的是**最新一轮**的结构化结论，不做跨轮聚合，不要和 SDK 的增量聚合混着讲；`resolved` 目前恒为空（`check_code_modified` 无调用方），所以**不能**说"自动标记已修复" |
| 可以怎么说 | "tracked as new / persistent / unverified — a finding is not marked fixed just because the model stopped mentioning it" |
| 不要怎么说 | "automatically verifies your fixes" |

---

## 3. X 主帖草稿（两个角度）

两条都是纯 ASCII、不含链接，X 的计数规则下等同于原始长度；字符数为**脚本实测**（从本文提取 `>` 引用块后 `len()`）。上限 280。

### 草稿 A · 角度：输入一致性（pre-push）

> Reviewing uncommitted work is a consistency problem. While you edit, the diff, files the agent opens, and code on disk can be three different states.
>
> Mission Control freezes one snapshot and runs your agent CLI on it.

- 字符数：**218 / 280**
- 主张对应：价值点二
- 钩子说明：不写"agent 审不了未提交代码"——它本来就能读。这里说的是**同一份输入**的问题，不是能力有无的问题。
- 注意：不配"提升 N 倍"之类的图；如果要配图，用现有 `docs/screenshots/review-*.png`，不新拍。

### 草稿 B · 角度：编排已有 CLI + 结构化结果

> You already run a coding agent CLI. Mission Control turns it into a reviewer: it drives that CLI against a frozen snapshot and returns severity plus a parseable file:line when the agent supplies one.
>
> Login, model access and quota stay with that CLI.

- 字符数：**250 / 280**
- 主张对应：价值点一 + 价值点三
- 注意：不要再补"no new key / no cost / works with any model"。后半句已经把归属讲清楚了，加绝对化承诺会和事实冲突（`direct_api` 直连模式还要自己配 key）。

---

## 4. 短线程（4 条，每条 ≤280 字符）

**1/4** — 224 字符

> Reviewing uncommitted work is a consistency problem, not a reading problem. While files keep changing, git diff shows one slice and the agent may open a different state from disk.
>
> So Mission Control freezes the input first.

**2/4** — 263 字符

> How the freeze works:
> 1. copy your real index to a temp GIT_INDEX_FILE
> 2. add -A, write-tree, commit-tree (parent = HEAD)
> 3. create a worktree at that snapshot
>
> Your index, HEAD and files are untouched. The patch and the files the agent opens are the same commit.

**3/4** — 192 字符

> In incremental local reviews (SDK), findings include a parseable file:line when the agent supplies one. Statuses are new / persistent / unverified. Not mentioned again = unverified, not fixed.

**4/4** — 215 字符

> Try it on a repo you are already working in:
> amc review
>
> It drives the agent CLI you already have. OpenCode, Claude Code, Codex and Copilot adapters are in the repo; limits and current gaps are documented there too.

线程注意：
- 第 2 条是纯实现描述，`copy ... GIT_INDEX_FILE` 对得上 `local_review_service.py:91`，可以放心发。
- 第 3 条只说"不会被标为已修复"，没说"自动验证修复"——因为 `resolved` 还没接线。
- Qoder 的 CLI 自动化验收已通过（23 项 focused tests）；这不代表真实完整修复后复审已验收。不要加"5 分钟上手""比 X 快"这类没测过的说法。

---

## 5. 中文对应说明

给中文社区（朋友圈、即刻、V2EX、掘金）用的同一套话术，不逐字翻译英文帖。

**定位**

> 用你已经在用的 AI 编程 CLI 审查本地改动：输入是冻结的快照，结论结构化、可跨轮追踪。

**三个价值点（口语版）**

1. **复用已有的 Agent CLI。** 它自己不调模型，而是驱动你本机装的 OpenCode / Claude Code / Copilot / Codex；登录方式、模型权限和订阅额度仍然由那个 CLI 决定，框架只管编排和结果解析。
2. **审的是冻结下来的工作区。** 已暂存、未暂存、未跟踪的改动会被打成一个快照提交，Agent 在独立 worktree 里读；你的 index、HEAD、工作区不被碰，prompt 里的 patch 和它能打开的文件来自同一个 commit。
3. **结论结构化，跨轮不乱报喜（仅指 SDK 增量审查）。** 在 `incremental=True` 的本地审查里，每条问题有严重程度；agent 提供且可解析位置时才有 `file:line`。跨轮状态为 new / persistent / unverified：再次被提到是 persistent，没被提到才是 unverified，而不是"已修复"。Web 界面只看最新一轮，不走这套聚合。CLI 的 Markdown / JSON / 退出码行为有自动化测试覆盖并已通过；真实完整修复后复审仍未验收。

**中文帖的一条红线**：不要写"自动修复验证""零误报""任意模型都能用""完全免费"。当前 `resolved` 状态没有接线；模型可用性和额度跟着各 CLI 走。

---

## 6. 能力状态表（发布前逐条核对）

| 能力 | 代码位置 | 状态 | 发布时怎么说 |
|---|---|---|---|
| 驱动已有 agent CLI | `backend/app/adapters/__init__.py` | 已验证（单元）；真实 E2E 未验收 | "drives the CLI you already have" |
| 未提交改动进入审查（快照） | `local_review_service.py:76-167` | 已验证 | "a frozen snapshot of your working tree" |
| 索引/HEAD/工作区不被修改 | 同上 + `test_local_snapshot.py:24` | 已验证 | "your index and HEAD are never touched" |
| patch 与可打开文件同源 | `worktree.py:146-161` + `execution.py:699-741` | 已验证 | "the patch and the files are the same commit" |
| 严重程度与可选位置 | `review_result_service.py:173-205` | 已验证（位置仅在 agent 提供且可解析时出现） | "severity, plus a parseable file:line when the agent provides one" |
| new / persistent / unverified | `review_history.py:185-238`、`sdk.py:494-520` | 已验证（SDK 增量） | 文案本身必须带 "in incremental local reviews"，不能只靠后文备注补 |
| Markdown / JSON / 退出码 | `cli.py:1008-1023`（行号按 `7a093bd` 基线） | CLI 自动化测试 23 项通过；真实完整修复后复审仍未验收 | 可说 CLI 格式/退出码有自动化覆盖，不可暗示修复后多轮 E2E 已完成 |
| Web 实时进度（SSE） | `backend/app/api/routes_stream.py` | 实现支持，未做 UI E2E | 可提，不承诺延迟指标 |
| 模型枚举与来源说明 | `backend/app/core/backend_detector.py` | 已实现并经单元测试；枚举结果不代表账号调用权限 | 只说“本机可枚举模型”，不推断权限或可调用性 |
| 跨轮 `resolved` | `review_history.py:295`（无调用方） | **未接线**（属规划） | 不许提"自动标记已修复" |
| pre-commit 集成 | 无 | **规划** | 不提，先看试用反馈 |
| PATH shim 护栏 | `backend/app/core/review_policy.py:38-77` | 已验证 | 只能叫"guard-rail"，不能叫 sandbox |
| Windows 真实路径/编码 | CI 矩阵覆盖 | 实现支持，未实机 | 不单独作为卖点 |
| 子模块内未提交内容 | 快照只覆盖父仓库 | **不覆盖** | 被问到要直说 |
| 模型/账号/额度 | 归各 CLI 与用户订阅 | **框架不承诺** | 只说"stays with your CLI"，不写 any model / free / no cost |

---

## 7. 演示案例（只引用已实测的首轮）

首轮已在合成仓库用 `opencode/glm-5.3-flash-free` 实测：模型报告 `notes.py:24` 存在由 f-string SQL 插值导致的注入风险。该位置是这次 agent 实际提供且解析出的结果；不能据此承诺每条 finding 都有位置。详见 [release-readiness.md](release-readiness.md)。

修复后的第二轮**尚未完成有效的非空 diff 复审验收**，因此不在发布文案中写跨轮结果、`unverified` 结果或 finding 数量。修复后的 fixture 只做过离线校验，仍需先重新实测再决定是否录制这段流程。

```text
案例编号：DEMO-001（首轮已实测；修复复审未验收）
仓库：合成演示仓库（新建，不引用任何真实用户代码）
输入：未提交改动将参数化查询改为 f-string SQL 插值
执行：首次 `amc review`；修复后第二轮需要有真实非空 diff 并重新验收
实际结果：首轮为 `concerns`；agent 报出 `notes.py:24` SQL 注入风险
跨轮状态：第二轮有效修复复审尚未验收，不作结论
采集时间/基线：见 [release-readiness.md](release-readiness.md)
```

跨轮状态口径：**再次被提到是 `persistent`，没被提到才是 `unverified`**。不得把此前空 diff 轮次的状态当成有效修复复审结果；不要预设第二轮一定出现某种 finding 或 `unverified`，也不要预设哪条会被"解决"——`resolved` 目前恒为空。

填充规则：
1. 只填**实际跑出来**的内容，命令、时间、基线 sha 一起记录。
2. 修复后先确认审查输入确有非空 diff，再记录实际结果；等待与剪辑过程需在视频中压缩或标注，30 秒是成片长度，不代表真实运行耗时。
3. 演示只用于说明流程，不用于证明准确率。

---

## 8. 试用反馈的三个问题

定向问，别问"你觉得怎么样"。

1. **识别环节**：你平时用哪个 agent CLI？`amc init --global` 有没有正确列出它，以及你实际在用的模型？如果列错了，列出来的是什么？
2. **结论质量**：这次 review 报出的 finding 里，有几条是你认为"确实值得改"的？`file:line` 指对了吗？有没有把没改动的代码报成问题？
3. **增量语义**：在 SDK 增量审查里，上一轮没被再次提到的问题会显示为 `unverified`；这对你的工作流是帮助还是噪音？你更希望它默认怎样呈现？

补充（可选，问修复意愿）：你会在 `git commit` 前跑它，还是只在开 PR 前跑？这个差别决定我们要不要做 pre-commit 集成。

---

## 9. 发布前自查清单

- [ ] 每条能力都能在 §6 找到代码位置，且措辞与"状态"列一致。
- [ ] 两条主帖 + 四条线程的字符数用脚本复算（从文档提取 `>` 块后 `len()`，纯 ASCII）。
- [ ] **没有**"你的 agent 审不了未提交代码"这类错误对比；现有 coding agent 本来就能读未提交内容。
- [ ] **没有** "any model" / "every model" / "no new key" / "no cost" / "free" 这类不归框架管的绝对承诺。
- [ ] 没有出现具体耗时、准确率、用户数量、用户评价。
- [ ] 没有出现"自动修复验证""零误报""完全离线""强沙箱"。
- [x] CLI 相关的 Markdown / JSON / 退出码自动化测试已通过（Qoder focused 23 项）；不将其表述为真实修复后复审 E2E。
- [ ] 演示段落仍是占位，或已由验收记录替换。
- [ ] 本文引用的路径与测试名均在 `7a093bd` 上存在（测试名以 `grep '^def test_'` 的实际输出为准，不按含义推测）。

---

## 附：证据核对记录（2026-10-24，基线 `7a093bd`）

| 引用 | 核对方式 | 结果 |
|---|---|---|
| `test_executable_matches_launched_program`（`test_adapters_preflight.py:19`） | `grep -n '^def test_'` | 函数名与行号一致 |
| `test_known_executables_are_not_the_backend_key`（`:31`） | 同上 | 一致 |
| `test_snapshot_uses_final_worktree_without_touching_real_index`（`test_local_snapshot.py:24`） | 同上 + 读函数体 | 一致；函数体确实断言 `write-tree` 与 `diff --cached --binary` 前后相同 |
| `test_worktree_and_compiled_context_use_the_captured_snapshot`（`:90`） | 同上 | 一致 |
| `test_execution_refreshes_local_snapshot_each_round`（`:157`） | 同上 | 一致 |
| `test_parses_severity_path_and_line`（`test_result_service.py:22`） | 同上 | 一致 |
| `test_incremental_clear_keeps_unmatched_prior_finding`（`test_sdk_and_db.py:135`） | 同上 | 一致 |
| `test_unmatched_findings_remain_unverified_across_incremental_rounds`（`test_local_snapshot.py:71`） | 同上 | 一致 |
| `cli.py:1008-1023`（`init` / `review` / `--format` / `--exit-code`，按 `7a093bd` 基线） | 读行内容 + Qoder CLI focused tests | 参数存在；CLI 自动化验收 23 项通过；未覆盖真实完整修复后复审 |
| `check_code_modified` 无调用方 | 全仓 grep | 只有定义（`review_history.py:295`），无调用点 |
