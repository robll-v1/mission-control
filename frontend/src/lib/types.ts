/* Shared domain types — mirrors the backend /api contract.
   Kept in one place so the shell, rail and panels agree. */

export type TaskStatus =
  | 'created'
  | 'ingesting'
  | 'context_ready'
  | 'running'
  | 'waiting_human'
  | 'validating'
  | 'completed'
  | 'failed'
  | 'aborted'

export type ReviewVerdict = 'clear' | 'concerns' | 'failed' | 'inconclusive'
export type ReviewSource = 'pull_request' | 'local_diff'
export type Severity = 'critical' | 'high' | 'medium' | 'low'

export type ReviewFinding = {
  severity: string
  summary: string
  path?: string | null
  line?: number | null
  detail?: string
}

export type ReviewResult = {
  verdict: ReviewVerdict
  summary: string
  findings: ReviewFinding[]
  severity_counts: Record<string, number>
  finding_count: number
  source_event_seq?: number | null
  supersedes_round_index?: number | null
}

export type Task = {
  id: string
  title: string
  description: string
  repo_path: string
  backend: string
  source_type: ReviewSource
  status: TaskStatus
  current_stage: string
  source_url?: string | null
  review_focus?: string
  pr_number?: number | null
  pr_owner?: string | null
  pr_repo?: string | null
  pr_head_sha?: string | null
  pr_base_ref?: string | null
  latest_review_result?: ReviewResult | null
  updated_at: number
  last_run_id?: string | null
}

export type EventRecord = {
  id: string
  kind: string
  level: 'info' | 'warning' | 'error'
  seq: number
  ts: number
  payload: Record<string, unknown>
}

export type ArtifactRecord = {
  path: string
  relative_path: string
}

export type ReviewRound = {
  id: string
  backend: string
  round_index: number
  review_note: string
  review_revision?: string | null
  status: string
  backend_session_id?: string | null
  exit_code?: number | null
  started_at: number
  ended_at?: number | null
  review_result?: ReviewResult | null
}

export type EventStats = {
  all_count: number
  important_count: number
  hidden_count: number
}

export type TaskDetailResponse = {
  task: Task
  events: EventRecord[]
  important_events?: EventRecord[]
  event_stats?: EventStats
  latest_review_result?: ReviewResult | null
  review_rounds?: ReviewRound[]
  runs?: ReviewRound[]
  artifacts: ArtifactRecord[]
}

export type TaskDetail = {
  task: Task
  events: EventRecord[]
  importantEvents: EventRecord[]
  eventStats: EventStats
  latestReviewResult: ReviewResult | null
  reviewRounds: ReviewRound[]
  artifacts: ArtifactRecord[]
}

export type DetailTab = 'results' | 'timeline' | 'rounds' | 'artifacts'
export type Notice = { tone: 'error' | 'info'; text: string }
export type Theme = 'light' | 'dark'

/** Lamp colour class per task status. Every status also carries a text label. */
export type StatusTone = 'live' | 'ok' | 'warn' | 'danger' | 'neutral'
