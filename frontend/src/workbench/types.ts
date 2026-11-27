import type { AttestationCounts, FindingCounts, TrackedFinding } from '../findings'
import type { RunSettings } from '../runSettings'

export type ReviewFinding = {
  severity: string
  path?: string | null
  line?: number | null
  summary: string
}

export type ReviewResult = {
  verdict: 'clear' | 'concerns' | 'failed' | 'inconclusive'
  summary: string
  findings: ReviewFinding[]
  finding_count: number
  severity_counts: Record<string, number>
  parse_mode?: 'json' | 'markdown' | 'none'
}

export type Task = {
  id: string
  title: string
  repo_path: string
  description: string
  source_type: string
  source_url?: string | null
  backend: string
  model?: string | null
  reasoning?: string | null
  language?: string
  review_focus?: string
  status: string
  current_stage: string
  branch_name?: string | null
  worktree_path?: string | null
  pr_owner?: string | null
  pr_repo?: string | null
  pr_number?: number | null
  pr_head_ref?: string | null
  pr_head_sha?: string | null
  pr_base_ref?: string | null
  local_head_sha?: string | null
  local_snapshot_sha?: string | null
  review_paths: string[]
  latest_review_result?: ReviewResult | null
  updated_at: number
  created_at?: number
}

export type EventRecord = {
  id: string
  kind: string
  level: 'info' | 'warning' | 'error'
  seq: number
  ts: number
  payload: Record<string, unknown>
}

export type EventStats = {
  all_count: number
  important_count: number
  hidden_count: number
}

export type ReviewRound = {
  id: string
  backend: string
  model?: string | null
  reasoning?: string | null
  language?: string
  round_index: number
  review_note?: string
  review_revision?: string | null
  status: string
  started_at: number
  ended_at?: number | null
  exit_code?: number | null
  review_result?: ReviewResult | null
  settings?: RunSettings | null
  metrics?: Record<string, unknown> | null
}

export type CheckRun = {
  id: string
  name: string
  command: string
  status: string
  exit_code?: number | null
  duration_sec?: number | null
  stdout_path?: string | null
  stderr_path?: string | null
  log_path?: string | null
  output_excerpt?: string | null
}

export type ArtifactEntry = {
  path: string
  relative_path: string
}

export type TaskDetailResponse = {
  task: Task
  events: EventRecord[]
  important_events?: EventRecord[]
  event_stats?: EventStats
  latest_review_result?: ReviewResult | null
  findings?: TrackedFinding[]
  finding_counts?: FindingCounts
  attestation_counts?: AttestationCounts
  review_rounds?: ReviewRound[]
  runs?: ReviewRound[]
  checks?: CheckRun[]
  artifacts: ArtifactEntry[]
}

export type TaskDetail = {
  task: Task
  events: EventRecord[]
  importantEvents: EventRecord[]
  eventStats: EventStats
  latestReviewResult: ReviewResult | null
  findings: TrackedFinding[]
  findingCounts: FindingCounts
  attestationCounts: AttestationCounts
  rounds: ReviewRound[]
  checks: CheckRun[]
  artifacts: ArtifactEntry[]
}

export type SnippetLine = {
  number: number
  text: string
  highlighted: boolean
}

export type SnippetResponse = {
  path: string
  target_line: number
  lines: SnippetLine[]
}

export type DiffResponse = {
  task_id: string
  source: string
  changed_files: string[]
  diff: string
  truncated: boolean
}

export type StorageInfo = {
  data_dir: string
  db_path: string
  runtime_dir: string
  worktrees_dir: string
  config_path: string
  bundled_ui: boolean
}

export type RepoStatus = {
  repo_path: string
  exists: boolean
  is_git_repo: boolean
  branch: string | null
  changed_files: number
  gh_available: boolean
  gh_authenticated: boolean
}
