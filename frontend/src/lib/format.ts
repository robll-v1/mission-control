/* Formatting + label maps for the console UI.
   Labels are bilingual: the CN label is the primary copy, the EN token
   is rendered as a mono micro-label (e.g. "审查中 RUNNING"). */

import type {
  ReviewResult,
  ReviewVerdict,
  Severity,
  StatusTone,
  Task,
  TaskStatus,
} from './types'

export const SEVERITY_ORDER: Severity[] = ['critical', 'high', 'medium', 'low']

export const EVENT_LIMIT = 40
export const THEME_KEY = 'amc-theme'

/* ── Label maps ─────────────────────────────────────────────── */

const STATUS_LABELS: Record<TaskStatus, { cn: string; en: string; tone: StatusTone }> = {
  created: { cn: '已创建', en: 'created', tone: 'neutral' },
  ingesting: { cn: '准备中', en: 'ingesting', tone: 'live' },
  context_ready: { cn: '待审查', en: 'ready', tone: 'neutral' },
  running: { cn: '审查中', en: 'running', tone: 'live' },
  waiting_human: { cn: '待处理', en: 'waiting', tone: 'warn' },
  validating: { cn: '验证中', en: 'validating', tone: 'live' },
  completed: { cn: '已完成', en: 'completed', tone: 'ok' },
  failed: { cn: '失败', en: 'failed', tone: 'danger' },
  aborted: { cn: '已中止', en: 'aborted', tone: 'danger' },
}

const STAGE_LABELS: Record<string, string> = {
  created: '已创建',
  review_ready: '待审查',
  prepare_review: '准备上下文',
  review_in_progress: '审查进行中',
  awaiting_next_round: '等待下一轮',
  review_failed: '审查失败',
  review_done: '审查完成',
  interrupted: '被中断',
  aborted: '已中止',
  validate: '验证中',
  handoff: '待交接',
}

const VERDICT_LABELS: Record<ReviewVerdict, { cn: string; en: string }> = {
  clear: { cn: '未发现明显问题', en: 'clear' },
  concerns: { cn: '需要关注', en: 'concerns' },
  failed: { cn: '审查失败', en: 'failed' },
  inconclusive: { cn: '结论不明确', en: 'inconclusive' },
}

const SEVERITY_LABELS: Record<Severity, { cn: string; en: string }> = {
  critical: { cn: '严重', en: 'critical' },
  high: { cn: '高', en: 'high' },
  medium: { cn: '中', en: 'medium' },
  low: { cn: '低', en: 'low' },
}

const TAB_LABELS: Record<string, { cn: string; en: string }> = {
  results: { cn: '审查结果', en: 'findings' },
  timeline: { cn: '事件流', en: 'timeline' },
  rounds: { cn: '审查轮次', en: 'rounds' },
  artifacts: { cn: '产物', en: 'artifacts' },
}

export const statusLabel = (status: TaskStatus) => STATUS_LABELS[status]?.cn ?? status
export const statusEnLabel = (status: TaskStatus) => STATUS_LABELS[status]?.en ?? status
export const statusTone = (status: TaskStatus): StatusTone => STATUS_LABELS[status]?.tone ?? 'neutral'
export const stageLabel = (stage: string) => STAGE_LABELS[stage] ?? stage
export const verdictLabel = (verdict?: ReviewVerdict | null) =>
  verdict ? VERDICT_LABELS[verdict].cn : '暂无结果'
export const verdictEnLabel = (verdict?: ReviewVerdict | null) =>
  verdict ? VERDICT_LABELS[verdict].en : 'pending'
export const severityLabel = (severity: string) =>
  SEVERITY_LABELS[severity as Severity]?.cn ?? severity
export const severityEnLabel = (severity: string) =>
  SEVERITY_LABELS[severity as Severity]?.en ?? severity
export const tabLabel = (tab: string) => TAB_LABELS[tab]?.cn ?? tab
export const tabEnLabel = (tab: string) => TAB_LABELS[tab]?.en ?? tab

export const isTerminalStatus = (status: TaskStatus) =>
  status === 'completed' || status === 'failed' || status === 'aborted'

/* ── Time helpers ───────────────────────────────────────────── */

export const timeText = (ts?: number | null) =>
  ts ? new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false }) : '—'

export const relativeTimeText = (ts?: number) => {
  if (!ts) return '—'
  const diffSec = Math.max(0, Math.floor(Date.now() / 1000 - ts))
  if (diffSec < 60) return `${diffSec} 秒前`
  const diffMin = Math.floor(diffSec / 60)
  if (diffMin < 60) return `${diffMin} 分钟前`
  const diffHour = Math.floor(diffMin / 60)
  if (diffHour < 24) return `${diffHour} 小时前`
  return `${Math.floor(diffHour / 24)} 天前`
}

export const durationText = (start?: number, end?: number | null) => {
  if (!start || !end) return '—'
  const sec = Math.max(0, Math.round(end - start))
  if (sec < 60) return `${sec}s`
  return `${Math.floor(sec / 60)}m ${sec % 60}s`
}

/** HH:MM:SS UTC — the console clock. */
export const utcClockText = (date: Date) =>
  [date.getUTCHours(), date.getUTCMinutes(), date.getUTCSeconds()]
    .map((n) => String(n).padStart(2, '0'))
    .join(':')

/* ── Misc ───────────────────────────────────────────────────── */

export const shortSha = (value?: string | null) => (value ? value.slice(0, 10) : '—')

/** MC-1A2B3C4D style mission id from the task uuid. */
export const missionId = (taskId: string) => `MC-${taskId.slice(0, 8).toUpperCase()}`

export const prShortLabel = (task: Task) => {
  if (task.source_type === 'local_diff') {
    return `本地变更 · ${task.pr_base_ref || '自动基准'}`
  }
  if (task.pr_owner && task.pr_repo && task.pr_number) {
    return `${task.pr_owner}/${task.pr_repo} #${task.pr_number}`
  }
  return task.source_url ?? null
}

export const severityCount = (result: ReviewResult | null | undefined, severity: string) =>
  result?.severity_counts?.[severity] ?? 0

/** Severity shares for the stacked meter; returns zero-safe segments. */
export const severitySegments = (result: ReviewResult | null | undefined) => {
  const total = SEVERITY_ORDER.reduce((sum, s) => sum + severityCount(result, s), 0)
  return SEVERITY_ORDER.map((severity) => ({
    severity,
    count: severityCount(result, severity),
    percent: total > 0 ? (severityCount(result, severity) / total) * 100 : 0,
  }))
}

export const stringifyPayload = (payload: Record<string, unknown>) => {
  if (typeof payload.text === 'string') return payload.text
  if (typeof payload.message === 'string') return payload.message
  if (typeof payload.summary === 'string') return payload.summary
  return JSON.stringify(payload, null, 2)
}

export const previewText = (value: string, limit = 240) =>
  value.length <= limit ? value : `${value.slice(0, limit)}…`

/* ── Rail filters ───────────────────────────────────────────── */

export type RailFilter = 'all' | 'live' | 'waiting' | 'done' | 'fault'

export const RAIL_FILTERS: { id: RailFilter; label: string }[] = [
  { id: 'all', label: '全部' },
  { id: 'live', label: '进行中' },
  { id: 'waiting', label: '待处理' },
  { id: 'done', label: '已完成' },
  { id: 'fault', label: '异常' },
]

export const matchesFilter = (task: Task, filter: RailFilter) => {
  switch (filter) {
    case 'live':
      return task.status === 'running' || task.status === 'ingesting' || task.status === 'validating'
    case 'waiting':
      return task.status === 'waiting_human' || task.status === 'context_ready' || task.status === 'created'
    case 'done':
      return task.status === 'completed'
    case 'fault':
      return task.status === 'failed' || task.status === 'aborted'
    default:
      return true
  }
}
