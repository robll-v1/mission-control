# 30 秒演示录制计划

> 状态：**拍摄计划，未录制、未执行真实复审。**
> 代码基线：`5476de2b141de1c256212e7a08f25de910b7273a`（草稿 PR #22 的整合基线）。
> 本文只使用合成仓库；30 秒指**剪辑后的成片长度**，不是模型耗时。

## 1. 录制前必须满足的门槛

以下任一项未满足，就只能准备素材，不能把视频标成“验收通过”或对外发布。

1. **非空修复 diff 的真实复审已通过验收。** 此前真实首轮只确认了合成 SQL 注入；旧第二轮实际审查的是空 diff。修正后的 fixture 只做了离线验证，尚未进行真实模型复审。不得沿用旧第二轮充数，也不得预设新一轮的 verdict、finding 或跨轮状态。
2. **本次使用的构建已完成 Web 冒烟。** 按 `docs/web-smoke-checklist.md` 执行创建、运行、取消、结果、375px 和亮暗主题检查；失败项先修复或如实剪掉相应镜头。
3. **画面与运行代码同基线。** 记录实际 commit、浏览器、视口、后端与模型标识。`docs/screenshots/` 中已有的 5 张图只能作历史参考，不能当作本基线的 UI 实测证据。
4. **只使用获准的合成数据与调用预算。** 真实调用前确认使用哪个后端、哪个模型以及允许几轮；检测出的模型 ID 不等于账号有调用权限。

已知事实来源：`docs/release-readiness.md` §3、§8、§10，以及 `AGENTS.md` §9。`147 passed, 1 skipped`、前端 tsc/build 通过是上一轮的**本地验收记录**；本 worktree 未复跑，且该基线当时未 push，不能写成“本轮 CI 通过”。

## 2. 录制环境与隐私规则

- 代码：独立、可丢弃的 checkout，变量统称为 `$AMC_DEMO_ROOT`；不要对正在使用的工作区或服务操作。
- 数据：仅由 `examples/first-review/make_demo_repo.py` 生成 `$DEMO`。目标目录必须不存在，仓库由脚本初始化为 `main`，作者为 `Demo User <demo@example.com>`。
- 配置与账号：独立的 `XDG_CONFIG_HOME` 只隔离 AMC 自己的配置文件，不保证隔离模型检测或后端 CLI 的私人状态。检测器仍可能读取 `$HOME` 下的 Codex/OpenCode 缓存；启动的 Agent CLI 也可能复用本机已有登录。录制前确认调用预算与账号授权；不要打印或录制环境变量、缓存内容或认证信息，所有可见输出都要检查并脱敏。
- 画面：隐藏本机用户名、真实仓库绝对路径、账号名、邮箱、token、cookie、请求头及终端历史。任务详情会显示 `repo_path`，录屏时使用合成路径或后期遮盖。
- 主题：主片用浅色；深色镜头必须在同一任务、同一结果状态下另拍，不能用旧截图拼接。
- 等待：模型等待必须剪短，并在画面标注“等待已剪辑 / wait edited out”。不展示“秒级审查”“提速 N 倍”等结论。

## 3. 已核对 help 的命令

本 worktree 中实际只运行了 `--help`，**没有启动服务、没有执行 review、没有调用模型**。录制时不要直接运行裸 `amc`：它可能仍指向全局安装的 editable 包。使用已有项目依赖的 Python 解释器，并通过 `PYTHONPATH` 指向目标 checkout 的 `backend`。启动前先只读核对模块文件和 `_root_dir()` 都落在目标 checkout；路径核对输出只在本地查看，不录屏或分享。

```bash
cd "$AMC_DEMO_ROOT"
export PYTHONPATH="$AMC_DEMO_ROOT/backend${PYTHONPATH:+:$PYTHONPATH}"
export AMC_DEMO_PYTHON="/path/to/python-with-project-dependencies" # 替换为已安装项目依赖的解释器
"$AMC_DEMO_PYTHON" -c 'from pathlib import Path; import app.cli; print(Path(app.cli.__file__).resolve()); print(Path(app.cli._root_dir()).resolve())'
# 确认第一行位于 "$AMC_DEMO_ROOT/backend/app/cli.py"，第二行等于 "$AMC_DEMO_ROOT" 后，再运行下面的命令。
```

| 用途 | 录制时命令（占位符需替换） | help 核对结果 |
|---|---|---|
| 创建合成仓库 | `cd "$AMC_DEMO_ROOT" && python3 examples/first-review/make_demo_repo.py "$DEMO"` | 已核对；`target` 必须不存在；脚本以相对路径调用，需在 checkout 根目录执行 |
| 应用修复 | `python3 examples/first-review/make_demo_repo.py "$DEMO" --apply-fix` | 已核对；修复后保留非空工作区 diff（help 原文：re-review round has a genuine (non-empty) diff） |
| 启动 Web | `cd "$AMC_DEMO_ROOT" && PYTHONPATH="$AMC_DEMO_ROOT/backend${PYTHONPATH:+:$PYTHONPATH}" "$AMC_DEMO_PYTHON" -m app.cli start --port 18000 --ui-port 15173 --no-browser` | 已核对 `start` 参数；后端 `18000` 与前端 `15173` 是独立端口。启动前确认两个端口未被占用；CLI 可能另选端口，需记录实际值 |
| 停止 Web | 在同一 checkout、同一 Python 环境执行 `cd "$AMC_DEMO_ROOT" && PYTHONPATH="$AMC_DEMO_ROOT/backend${PYTHONPATH:+:$PYTHONPATH}" "$AMC_DEMO_PYTHON" -m app.cli stop` | 使用与启动相同的显式入口和 checkout；只停止本次隔离 checkout 的服务。若无法确认服务归属，先不要执行 stop |
| 可选 CLI 镜头 | `cd "$AMC_DEMO_ROOT" && PYTHONPATH="$AMC_DEMO_ROOT/backend${PYTHONPATH:+:$PYTHONPATH}" "$AMC_DEMO_PYTHON" -m app.cli review --repo "$DEMO" --backend <backend> --model <verified-model> --format json` | 已核对 `review` 的 `--repo`、`--backend`、`--model`、`--format {markdown,json}`；不是本轮必拍镜头 |
| 可选完整复审 | `cd "$AMC_DEMO_ROOT" && PYTHONPATH="$AMC_DEMO_ROOT/backend${PYTHONPATH:+:$PYTHONPATH}" "$AMC_DEMO_PYTHON" -m app.cli review --repo "$DEMO" --no-incremental` | 已核对；只有明确需要“替换历史结论”时才使用 |

补充事实：`python -m app.cli start` 会把实际后端端口写入前端的 `VITE_API_TARGET`（`backend/app/cli.py:399-417`）。后端数据库与 runtime 使用相对路径（`backend/app/api/app.py:33-36`），所以必须从隔离 checkout 启动；不要在原工作区运行上述命令。XDG 配置隔离也不会清除已有 CLI 登录；终端输出、模型名称、账号信息和错误文本录制前都要检查脱敏。

## 4. 30 秒分镜

字幕是拟稿，不是实测结果。涉及具体 finding 的画面必须在录制当天用真实输出替换；若未复现 `notes.py:24`，不得硬写该行号。

| 时间 | 画面与动作 | 中文字幕 | English subtitle | 事实证据 / 拍摄门槛 |
|---|---|---|---|---|
| 0.0–2.5s | 标题卡；背景为本次新拍的本地变更任务，不露私人路径 | 提交前，把本地改动冻结成一致的审查输入 | Freeze local changes into one consistent review input | 快照机制：`backend/app/services/local_review_service.py` 的 `capture_local_snapshot`；只承诺快照生成后的 patch/文件一致，不承诺捕获过程原子事务 |
| 2.5–6.0s | 打开“模型选择”，选择本次已实测可调用的后端/模型并保存；短暂展示模型来源说明 | 复用你已有的 Agent CLI | Use the agent CLI you already have | `frontend/src/Settings.tsx:90-102`（detect/current）、`:125-145`（保存）；界面固定提示“列表不代表账号调用权限”（`:286-289`）。不得拍成 any model / free |
| 6.0–10.0s | “新建审查”→“本地变更”→填 `$DEMO`→创建；展示“待审查” | 新建本地审查，只使用合成仓库 | Create a local review on a synthetic repo | `frontend/src/App.tsx:490-524`、`backend/app/api/routes_tasks.py:115-165`；创建只编译上下文，不等于已经调用模型 |
| 10.0–13.0s | 点击“开始首轮审查”，保留短暂运行态后跳剪；角标“等待已剪辑 / wait edited out” | 发起审查，等待片段已剪辑 | Start the review; the wait is edited out | `frontend/src/App.tsx:526-535`、`backend/app/api/routes_control.py` 的 start 路由。preflight 可能耗时，不能把等待剪成速度证明 |
| 13.0–18.0s | 展示本次首轮真实结果，滚动到一条带路径的 finding；若复现合成注入，可聚焦 `notes.py` 的实际行 | 结构化结果：严重程度 + 文件:行号 | Structured findings: severity + file:line | 渲染：`frontend/src/App.tsx:749-829`。此前仅真实验证过合成 SQL 注入；本次 finding 以新记录为准，禁止预写 verdict 或 `notes.py:24` |
| 18.0–23.0s | 终端执行 `--apply-fix`；展示 `git -C "$DEMO" diff -- notes.py` 非空，再点“发起下一轮审查” | 修复后，先确认第二轮有非空 diff | After the fix, confirm round two has a non-empty diff | fixture：`examples/first-review/make_demo_repo.py`；离线防护测试 `backend/tests/test_demo_fixture.py`。**非空 diff 的真实复审未验收前，此镜头只能空拍命令，不能展示结论** |
| 23.0–27.0s | 展示第二轮真实结果，再切到“审查轮次”证明有两轮；不添加“已修复”标签 | Web 展示最新一轮；结果以本次实测为准 | Web shows the latest round; results come from this run | `latestReviewResult`：`frontend/src/App.tsx:423-428`；轮次卡片：`:863-917`。Web 不做 SDK 的 unverified 增量聚合 |
| 27.0–30.0s | 结尾三词卡：Consistent input / Structured findings / Your CLI；小字注明 synthetic demo、edited for length | 一致输入，结构化结果，复用已有 CLI | Consistent input. Structured findings. Your CLI. | 不含耗时、准确率、用户数、修复率；不暗示离线推理、任意模型可用或强沙箱 |

### 可选替换镜头（不增加时长）

- 若需要展示 JSON/退出码，只能用本次真实 `amc review` 输出替换 23.0–27.0s，并明确标注 **CLI / SDK**。不要把 SDK 报告中的 `unverified` 剪到 Web 画面旁，造成 Web 也聚合的误解。
- 若要展示深色主题，用同一结果页切换主题替换结尾背景；先确认对比度和finding路径没有溢出。主题切换与持久化见 `frontend/src/App.tsx:399-410`、`:597-604`。

## 5. 拍摄执行单

### A. 拍摄前

- [ ] 记录代码 SHA、后端/CLI 版本、真实模型、调用预算、执行人与日期。
- [ ] 重新生成 `$DEMO`，确认只有合成文件；保存首轮前的 `git status` 与 diff。
- [ ] 首轮真实运行完成并保存原始结果；若首轮失败，记录失败，不补写“预期 finding”。
- [ ] `--apply-fix` 后确认 `notes.py` 的 diff 非空；第二轮真实运行完成并保存原始结果。
- [ ] Web 清单通过；所有截图/录屏均在当前构建重新采集。
- [ ] 复查画面中没有私密路径、账号、密钥、cookie、真实私人仓库名。

### B. 素材登记（当前全部为空）

| 素材 | 建议文件名 | 必须记录的事实 | 当前状态 |
|---|---|---|---|
| 首轮原始结果 | `round-1.json` / `round-1-web.png` | 实际 verdict、findings、模型、SHA、时间 | **未执行** |
| 修复后 diff | `after-fix.diff` | 非空、仅合成仓库 | **未执行** |
| 第二轮原始结果 | `round-2.json` / `round-2-web.png` | 实际 verdict、findings、非空 diff 对应关系 | **未执行，禁止预测** |
| 设置页 | `settings-current-light.png` | 模型来源与“枚举不等于权限”提示可见 | **未录制** |
| 30 秒主片 | `mission-control-demo-30s.mp4` | 总时长 ≤30s、等待剪辑已标注 | **未录制** |
| 深色对照 | `review-current-dark.png` | 同任务同结果、当前 SHA | **未录制** |

### C. 剪辑红线

1. 不把旧空 diff 第二轮剪成“修复后复审通过”。
2. 不把 30 秒成片或跳剪后的等待描述成模型速度。
3. 不把“检测到模型”描述成“账号可调用”；只有真实跑通的模型才能说本次已验证。
4. 不把 Web 最新轮次结果描述成 SDK 的 new / persistent / unverified 聚合。
5. 不说“自动确认已修复”“完全离线”“零成本”“任意模型”“强安全沙箱”。
6. 若实际结果与分镜不一致，改字幕和镜头以符合实际；不要改原始结果。
