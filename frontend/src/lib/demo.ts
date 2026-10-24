/* ============================================================
   Demo dataset — DEV-ONLY fallback.
   ------------------------------------------------------------
   When the backend API is unreachable during local development
   (vite dev server started without `amc start`), the console
   serves this dataset instead of erroring out, so the UI can be
   previewed and iterated on without the Python backend.
   It is tree-shaken from production builds (guarded by
   import.meta.env.DEV at the call site) and the UI always shows
   a visible "演示数据 DEMO" badge while it is active.
   ============================================================ */

import type { EventRecord, ReviewRound, Task, TaskDetailResponse } from './types'

const now = Math.floor(Date.now() / 1000)
const min = 60
const hour = 60 * min
const day = 24 * hour

let eventSeq = 0
const ev = (
  kind: string,
  level: EventRecord['level'],
  agoSec: number,
  payload: Record<string, unknown>,
): EventRecord => ({
  id: `demo-ev-${++eventSeq}`,
  kind,
  level,
  seq: ++eventSeq,
  ts: now - agoSec,
  payload,
})

const finding = (
  severity: string,
  path: string,
  line: number,
  summary: string,
  detail: string,
) => ({ severity, path, line, summary, detail })

/* ── Mission 1 — running, local diff ───────────────────────── */

const task1Events: EventRecord[] = [
  ev('task.created', 'info', 12 * min, { source_type: 'local_diff', repo_path: '~/code/mission-control' }),
  ev('git.base_detected', 'info', 11.5 * min, { base: 'main', head: 'a3f9c21' }),
  ev('git.diff_loaded', 'info', 11 * min, { files: 6, insertions: 214, deletions: 87 }),
  ev('context.snapshot_ready', 'info', 10 * min, { artifacts: 4 }),
  ev('review.round_started', 'info', 9.8 * min, { round_index: 1, backend: 'claude-code' }),
  ev('reviewer.output', 'info', 9 * min, {
    text: '正在读取 backend/app/pipeline/context.py 的上下文构建逻辑…',
  }),
  ev('reviewer.tool_call', 'info', 8.4 * min, { tool: 'read_file', target: 'backend/app/pipeline/context.py' }),
  ev('reviewer.output', 'info', 6 * min, {
    text: '发现快照收集逻辑未过滤 .workbuddy/ 与 node_modules/，准备写入 findings。',
  }),
  ev('reviewer.output', 'info', 2 * min, { text: '继续核对 diff 中暂存区与工作区的一致性…' }),
]

const task1Rounds: ReviewRound[] = [
  {
    id: 'demo-round-1',
    backend: 'claude-code',
    round_index: 1,
    review_note: '首轮：重点核对本地快照过滤规则',
    review_revision: 'a3f9c21',
    status: 'running',
    backend_session_id: 'sess-9f2c',
    exit_code: null,
    started_at: now - 9.8 * min,
    ended_at: null,
    review_result: null,
  },
]

/* ── Mission 2 — completed PR, concerns ────────────────────── */

const task2Findings = [
  finding(
    'critical',
    'backend/app/api/mcp_routes.py',
    212,
    'MCP 工具注册未校验 schema 版本，v1 客户端可能收到未知字段',
    'register_tool() 直接接受调用方传入的 schema dict，未检查 schema_version 字段。当 v1 客户端携带旧版 schema 注册时，服务端会原样透传，导致后续 tool_call 阶段出现字段缺失型 500。建议在入口处拒绝缺失或非 "1" 的 schema_version，并返回 422。',
  ),
  finding(
    'high',
    'backend/app/pipeline/context.py',
    88,
    '上下文快照包含生成路径，可能泄漏本地绝对路径',
    'collect_snapshot() 的排除列表只覆盖了 .git/ 与 __pycache__/，未排除 .workbuddy/、dist/ 与 node_modules/。快照文件会随 export-summary 一并落盘，存在把本机目录结构写进分享摘要的风险。',
  ),
  finding(
    'high',
    'frontend/src/App.tsx',
    157,
    'SSE 重连缺少指数退避，后端抖动时会形成请求风暴',
    'EventSource onerror 后直接 close() 且无重试逻辑，依赖 4s 轮询兜底。后端短暂不可用时，每个打开的标签页都会同时重试 /api/tasks，建议加 1s→30s 指数退避 + 抖动，并在 UI 上暴露连接状态。',
  ),
  finding(
    'medium',
    'backend/app/review/runner.py',
    45,
    '审查超时后未清理子进程，存在僵尸进程风险',
    'run_review() 使用 subprocess.Popen 启动后端 CLI，超时分支只调用了 wait() 而未 terminate()/kill()。超时场景下子进程会继续持有管道，导致后续轮次的 stdout 读取阻塞。',
  ),
  finding(
    'low',
    'docs/README.md',
    12,
    '快速开始缺少 Windows 平台的 venv 激活说明',
    'Path B 只给出 macOS/Linux 的 source .venv/bin/activate，Windows 用户按文档操作会在 Scripts\\activate 处卡住。建议补一行 PowerShell 命令。',
  ),
]

const task2Result = {
  verdict: 'concerns' as const,
  summary:
    '本轮发现 5 个问题：1 个严重（MCP schema 版本未校验，影响 v1 客户端兼容性）、2 个高（快照路径泄漏、SSE 重连风暴）、1 个中（超时未回收子进程）、1 个低（文档缺 Windows 说明）。严重与高危问题建议合并前修复；SSE 退避属于体验优化，可排期处理。',
  findings: task2Findings,
  severity_counts: { critical: 1, high: 2, medium: 1, low: 1 },
  finding_count: 5,
  source_event_seq: 42,
  supersedes_round_index: 1,
}

const task2Events: EventRecord[] = [
  ev('task.created', 'info', 2 * day, { source_type: 'pull_request', pr: 'robll-v1/mission-control#18' }),
  ev('git.pr_fetched', 'info', 2 * day - 3 * min, { head_sha: '7c1e0a4', base_ref: 'main' }),
  ev('git.diff_loaded', 'info', 2 * day - 2 * min, { files: 11, insertions: 486, deletions: 132 }),
  ev('context.snapshot_ready', 'info', 2 * day - 90, { artifacts: 5 }),
  ev('review.round_started', 'info', 2 * day - 80, { round_index: 1, backend: 'claude-code' }),
  ev('reviewer.output', 'warning', 2 * day - 40, {
    text: '第 1 轮在 8 分钟后超时，未产出结构化结论，将自动进入第 2 轮。',
  }),
  ev('review.round_failed', 'warning', 2 * day - 30, { round_index: 1, exit_code: 124 }),
  ev('review.round_started', 'info', 2 * day - 20, { round_index: 2, backend: 'claude-code' }),
  ev('reviewer.output', 'info', 2 * day - 12 * min, { text: '第 2 轮聚焦 MCP 路由与快照收集两个高风险区域。' }),
  ev('validator.result', 'info', 2 * day - 6 * min, { findings: 5, verdict: 'concerns' }),
  ev('review.round_done', 'info', 2 * day - 5 * min, { round_index: 2, duration: '14m 22s' }),
  ev('task.completed', 'info', 2 * day - 4 * min, { verdict: 'concerns' }),
]

const task2Rounds: ReviewRound[] = [
  {
    id: 'demo-round-2a',
    backend: 'claude-code',
    round_index: 1,
    review_note: '首轮全量审查（超时）',
    review_revision: '7c1e0a4',
    status: 'failed',
    backend_session_id: 'sess-31ab',
    exit_code: 124,
    started_at: now - 2 * day + 20,
    ended_at: now - 2 * day + 30,
    review_result: null,
  },
  {
    id: 'demo-round-2b',
    backend: 'claude-code',
    round_index: 2,
    review_note: '第 2 轮聚焦 MCP 路由与快照收集两个高风险区域',
    review_revision: '7c1e0a4',
    status: 'completed',
    backend_session_id: 'sess-77f0',
    exit_code: 0,
    started_at: now - 2 * day + 40,
    ended_at: now - 2 * day + 54,
    review_result: task2Result,
  },
]

/* ── Mission 3 — completed PR, clear ───────────────────────── */

const task3Result = {
  verdict: 'clear' as const,
  summary: '文档改动共 3 个文件，仅涉及 README 快速开始章节与两张截图引用。未发现事实性错误、失效链接或泄漏的内部信息。',
  findings: [],
  severity_counts: {},
  finding_count: 0,
  source_event_seq: 18,
}

const task3Events: EventRecord[] = [
  ev('task.created', 'info', 3 * day, { source_type: 'pull_request', pr: 'robll-v1/mission-control#15' }),
  ev('git.diff_loaded', 'info', 3 * day - min, { files: 3, insertions: 42, deletions: 12 }),
  ev('review.round_started', 'info', 3 * day - 50, { round_index: 1, backend: 'opencode' }),
  ev('validator.result', 'info', 3 * day - 3 * min, { findings: 0, verdict: 'clear' }),
  ev('review.round_done', 'info', 3 * day - 2 * min, { round_index: 1, duration: '3m 05s' }),
  ev('task.completed', 'info', 3 * day - min, { verdict: 'clear' }),
]

const task3Rounds: ReviewRound[] = [
  {
    id: 'demo-round-3',
    backend: 'opencode',
    round_index: 1,
    review_note: '文档类改动，快速核对',
    review_revision: 'b2d4e19',
    status: 'completed',
    backend_session_id: 'sess-0c41',
    exit_code: 0,
    started_at: now - 3 * day + 50,
    ended_at: now - 3 * day + 53,
    review_result: task3Result,
  },
]

/* ── Mission 4 — failed ────────────────────────────────────── */

const task4Events: EventRecord[] = [
  ev('task.created', 'info', 5 * hour, { source_type: 'pull_request', pr: 'robll-v1/mission-control#19' }),
  ev('git.pr_fetched', 'error', 4.8 * hour, { error: 'GitHub API 限流：403 rate limit exceeded' }),
  ev('git.pr_fetched', 'error', 4.7 * hour, { error: 'GitHub API 限流：403 rate limit exceeded' }),
  ev('review.round_failed', 'error', 4.6 * hour, { reason: '无法拉取 PR diff，上下文为空', exit_code: 2 }),
  ev('task.failed', 'error', 4.6 * hour, { reason: 'ingest 阶段连续失败 2 次' }),
]

const task4Rounds: ReviewRound[] = [
  {
    id: 'demo-round-4',
    backend: 'claude-code',
    round_index: 1,
    review_note: '拉取 PR #19 diff',
    review_revision: null,
    status: 'failed',
    backend_session_id: null,
    exit_code: 2,
    started_at: now - 4.8 * hour,
    ended_at: now - 4.6 * hour,
    review_result: null,
  },
]

/* ── Mission 5 — waiting_human ─────────────────────────────── */

const task5Result = {
  verdict: 'concerns' as const,
  summary:
    '设置页改造整体结构清晰，发现 3 个问题：模型搜索未做防抖（高频输入会触发大量重渲染）、后端卡片选中态依赖边框颜色单一信号、保存成功提示 3s 自动消失可能被错过。建议处理前两个后再合入。',
  findings: [
    finding(
      'high',
      'frontend/src/Settings.tsx',
      78,
      '模型搜索输入未做防抖，过滤逻辑在每次按键时全量执行',
      'filteredModels 直接由 modelSearch 派生，没有 debounce。当后端模型列表超过 200 项时，每次输入都会触发一次全量 filter + React 重渲染，低端机上可感知卡顿。建议加 150–200ms 防抖。',
    ),
    finding(
      'medium',
      'frontend/src/Settings.tsx',
      186,
      '后端卡片选中态仅靠边框颜色传达，色盲用户无法区分',
      '.backend-card.selected 只改变了 border-color 与 background。建议补充勾选图标或左侧色条，保证不依赖颜色单一通道。',
    ),
    finding(
      'low',
      'frontend/src/Settings.tsx',
      64,
      '保存成功提示 3 秒后自动消失，慢操作用户可能错过',
      'setTimeout 3000 后清除 saveSuccess。建议改为手动关闭，或延长至 6s 并提供关闭按钮。',
    ),
  ],
  severity_counts: { high: 1, medium: 1, low: 1 },
  finding_count: 3,
  source_event_seq: 31,
}

const task5Events: EventRecord[] = [
  ev('task.created', 'info', 26 * hour, { source_type: 'pull_request', pr: 'robll-v1/mission-control#21' }),
  ev('git.diff_loaded', 'info', 26 * hour - min, { files: 4, insertions: 180, deletions: 96 }),
  ev('review.round_started', 'info', 26 * hour - 50, { round_index: 1, backend: 'codex' }),
  ev('validator.result', 'info', 26 * hour - 8 * min, { findings: 3, verdict: 'concerns' }),
  ev('review.round_done', 'info', 26 * hour - 7 * min, { round_index: 1, duration: '9m 41s' }),
  ev('task.handoff', 'warning', 26 * hour - 6 * min, { reason: '等待人工确认是否处理 findings 后再进入下一轮' }),
]

const task5Rounds: ReviewRound[] = [
  {
    id: 'demo-round-5',
    backend: 'codex',
    round_index: 1,
    review_note: '设置页交互与可访问性专项',
    review_revision: 'e5b8d02',
    status: 'completed',
    backend_session_id: 'sess-5a19',
    exit_code: 0,
    started_at: now - 26 * hour + 50,
    ended_at: now - 26 * hour + 60,
    review_result: task5Result,
  },
]

/* ── Mission 6 — completed local diff, clear ────────────────── */

const task6Events: EventRecord[] = [
  ev('task.created', 'info', 40 * min, { source_type: 'local_diff', repo_path: '~/code/mission-control' }),
  ev('git.base_detected', 'info', 39 * min, { base: 'main', head: '9d0f331' }),
  ev('git.diff_loaded', 'info', 39 * min, { files: 2, insertions: 24, deletions: 6 }),
  ev('review.round_started', 'info', 38 * min, { round_index: 1, backend: 'opencode' }),
  ev('validator.result', 'info', 34 * min, { findings: 0, verdict: 'clear' }),
  ev('review.round_done', 'info', 34 * min, { round_index: 1, duration: '3m 52s' }),
  ev('task.completed', 'info', 34 * min, { verdict: 'clear' }),
]

const task6Result = {
  verdict: 'clear' as const,
  summary: 'Makefile 启动脚本改动仅涉及端口探测逻辑，已核对边界条件与错误分支。未发现问题。',
  findings: [],
  severity_counts: {},
  finding_count: 0,
  source_event_seq: 12,
}

const task6Rounds: ReviewRound[] = [
  {
    id: 'demo-round-6',
    backend: 'opencode',
    round_index: 1,
    review_note: 'Makefile 端口探测改动',
    review_revision: '9d0f331',
    status: 'completed',
    backend_session_id: 'sess-2e7b',
    exit_code: 0,
    started_at: now - 38 * min,
    ended_at: now - 34 * min,
    review_result: task6Result,
  },
]

/* ── Task list ─────────────────────────────────────────────── */

const artifactsFor = (files: [string, string][]) =>
  files.map(([relative_path, path]) => ({ relative_path, path }))

export const DEMO_TASKS: Task[] = [
  {
    id: 'a3f9c218-4b2d-4e1a-9c7e-1d2f3a4b5c01',
    title: '本地快照过滤规则重构 · mission-control',
    description:
      '重构 collect_snapshot() 的排除列表：把 .workbuddy/、dist/、node_modules/ 纳入默认排除，并支持 .amcignore 覆盖。\n关注点：排除规则与 git 语义的一致性、符号链接处理。',
    repo_path: '~/code/mission-control',
    backend: 'claude-code',
    source_type: 'local_diff',
    status: 'running',
    current_stage: 'review_in_progress',
    source_url: null,
    review_focus: '排除规则与 git 语义的一致性、符号链接处理',
    pr_number: null,
    pr_owner: null,
    pr_repo: null,
    pr_head_sha: 'a3f9c21',
    pr_base_ref: 'main',
    latest_review_result: null,
    updated_at: now - 2 * min,
    last_run_id: 'demo-round-1',
  },
  {
    id: '7c1e0a42-8f3b-4c5d-9e6f-0a1b2c3d4e02',
    title: 'Constrain MCP to the supported v1 API',
    description: 'PR #18：将 MCP 工具注册收敛到 v1 API，补充 schema 版本校验与回归测试。',
    repo_path: '~/code/mission-control',
    backend: 'claude-code',
    source_type: 'pull_request',
    status: 'completed',
    current_stage: 'review_done',
    source_url: 'https://github.com/robll-v1/mission-control/pull/18',
    review_focus: '',
    pr_number: 18,
    pr_owner: 'robll-v1',
    pr_repo: 'mission-control',
    pr_head_sha: '7c1e0a4',
    pr_base_ref: 'main',
    latest_review_result: task2Result,
    updated_at: now - 2 * day + 4 * min,
    last_run_id: 'demo-round-2b',
  },
  {
    id: 'b2d4e190-1a2b-4c3d-8e9f-4a5b6c7d8e03',
    title: 'Update README quick start',
    description: 'PR #15：更新 README 快速开始章节，补充截图与 make start 说明。',
    repo_path: '~/code/mission-control',
    backend: 'opencode',
    source_type: 'pull_request',
    status: 'completed',
    current_stage: 'review_done',
    source_url: 'https://github.com/robll-v1/mission-control/pull/15',
    review_focus: '',
    pr_number: 15,
    pr_owner: 'robll-v1',
    pr_repo: 'mission-control',
    pr_head_sha: 'b2d4e19',
    pr_base_ref: 'main',
    latest_review_result: task3Result,
    updated_at: now - 3 * day + min,
    last_run_id: 'demo-round-3',
  },
  {
    id: 'e5b8d026-9c8d-4e7f-8a9b-1c2d3e4f5a04',
    title: 'feat/settings-polish：设置页模型搜索与分组',
    description: 'PR #21：设置页支持模型搜索、按类型分组展示，并记忆上次选择。',
    repo_path: '~/code/mission-control',
    backend: 'codex',
    source_type: 'pull_request',
    status: 'waiting_human',
    current_stage: 'handoff',
    source_url: 'https://github.com/robll-v1/mission-control/pull/21',
    review_focus: '交互细节与可访问性',
    pr_number: 21,
    pr_owner: 'robll-v1',
    pr_repo: 'mission-control',
    pr_head_sha: 'e5b8d02',
    pr_base_ref: 'main',
    latest_review_result: task5Result,
    updated_at: now - 26 * hour + 6 * min,
    last_run_id: 'demo-round-5',
  },
  {
    id: '9d0f331a-5e6f-4a7b-8c9d-2e3f4a5b6c05',
    title: 'Makefile 端口探测改造 · 本地变更',
    description: '把固定端口改为动态探测，支持 AMC_PORT 覆盖。',
    repo_path: '~/code/mission-control',
    backend: 'opencode',
    source_type: 'local_diff',
    status: 'completed',
    current_stage: 'review_done',
    source_url: null,
    review_focus: '',
    pr_number: null,
    pr_owner: null,
    pr_repo: null,
    pr_head_sha: '9d0f331',
    pr_base_ref: 'main',
    latest_review_result: task6Result,
    updated_at: now - 34 * min,
    last_run_id: 'demo-round-6',
  },
  {
    id: 'c4a1b2c3-d4e5-4f6a-8b9c-0d1e2f3a4b06',
    title: 'fix: 跨平台路径处理（PR #19）',
    description: 'PR #19：统一 Windows / macOS 路径拼接，修复 Scripts\\activate 文档问题。',
    repo_path: '~/code/mission-control',
    backend: 'claude-code',
    source_type: 'pull_request',
    status: 'failed',
    current_stage: 'review_failed',
    source_url: 'https://github.com/robll-v1/mission-control/pull/19',
    review_focus: '',
    pr_number: 19,
    pr_owner: 'robll-v1',
    pr_repo: 'mission-control',
    pr_head_sha: null,
    pr_base_ref: 'main',
    latest_review_result: null,
    updated_at: now - 4.6 * hour,
    last_run_id: 'demo-round-4',
  },
]

export const DEMO_DETAILS: Record<string, TaskDetailResponse> = {
  'a3f9c218-4b2d-4e1a-9c7e-1d2f3a4b5c01': {
    task: DEMO_TASKS[0],
    events: task1Events,
    important_events: task1Events,
    event_stats: {
      all_count: task1Events.length,
      important_count: task1Events.length,
      hidden_count: 0,
    },
    latest_review_result: null,
    review_rounds: task1Rounds,
    artifacts: artifactsFor([
      ['context/snapshot.md', '~/.amc/runs/demo-round-1/context/snapshot.md'],
      ['diff/patch.diff', '~/.amc/runs/demo-round-1/diff/patch.diff'],
    ]),
  },
  '7c1e0a42-8f3b-4c5d-9e6f-0a1b2c3d4e02': {
    task: DEMO_TASKS[1],
    events: task2Events,
    important_events: task2Events,
    event_stats: {
      all_count: task2Events.length,
      important_count: task2Events.length,
      hidden_count: 0,
    },
    latest_review_result: task2Result,
    review_rounds: task2Rounds,
    artifacts: artifactsFor([
      ['context/snapshot.md', '~/.amc/runs/demo-round-2b/context/snapshot.md'],
      ['diff/patch.diff', '~/.amc/runs/demo-round-2b/diff/patch.diff'],
      ['summary/review-summary.md', '~/.amc/runs/demo-round-2b/summary/review-summary.md'],
      ['logs/round-1.log', '~/.amc/runs/demo-round-2b/logs/round-1.log'],
      ['logs/round-2.log', '~/.amc/runs/demo-round-2b/logs/round-2.log'],
    ]),
  },
  'b2d4e190-1a2b-4c3d-8e9f-4a5b6c7d8e03': {
    task: DEMO_TASKS[2],
    events: task3Events,
    important_events: task3Events,
    event_stats: { all_count: 6, important_count: 6, hidden_count: 0 },
    latest_review_result: task3Result,
    review_rounds: task3Rounds,
    artifacts: artifactsFor([
      ['context/snapshot.md', '~/.amc/runs/demo-round-3/context/snapshot.md'],
      ['summary/review-summary.md', '~/.amc/runs/demo-round-3/summary/review-summary.md'],
    ]),
  },
  'e5b8d026-9c8d-4e7f-8a9b-1c2d3e4f5a04': {
    task: DEMO_TASKS[3],
    events: task5Events,
    important_events: task5Events,
    event_stats: {
      all_count: task5Events.length,
      important_count: task5Events.length,
      hidden_count: 0,
    },
    latest_review_result: task5Result,
    review_rounds: task5Rounds,
    artifacts: artifactsFor([
      ['context/snapshot.md', '~/.amc/runs/demo-round-5/context/snapshot.md'],
      ['diff/patch.diff', '~/.amc/runs/demo-round-5/diff/patch.diff'],
      ['summary/review-summary.md', '~/.amc/runs/demo-round-5/summary/review-summary.md'],
    ]),
  },
  '9d0f331a-5e6f-4a7b-8c9d-2e3f4a5b6c05': {
    task: DEMO_TASKS[4],
    events: task6Events,
    important_events: task6Events,
    event_stats: {
      all_count: task6Events.length,
      important_count: task6Events.length,
      hidden_count: 0,
    },
    latest_review_result: task6Result,
    review_rounds: task6Rounds,
    artifacts: artifactsFor([
      ['context/snapshot.md', '~/.amc/runs/demo-round-6/context/snapshot.md'],
      ['summary/review-summary.md', '~/.amc/runs/demo-round-6/summary/review-summary.md'],
    ]),
  },
  'c4a1b2c3-d4e5-4f6a-8b9c-0d1e2f3a4b06': {
    task: DEMO_TASKS[5],
    events: task4Events,
    important_events: task4Events,
    event_stats: {
      all_count: task4Events.length,
      important_count: task4Events.length,
      hidden_count: 0,
    },
    latest_review_result: null,
    review_rounds: task4Rounds,
    artifacts: [],
  },
}

/** True once the dev fallback has served at least one response. */
export const demoState = { active: false }
