import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

type TaskStatus =
  | 'created'
  | 'ingesting'
  | 'context_ready'
  | 'running'
  | 'waiting_human'
  | 'validating'
  | 'completed'
  | 'failed'
  | 'aborted'

type ReviewVerdict = 'clear' | 'concerns' | 'failed' | 'inconclusive'
type ReviewSource = 'pull_request' | 'local_diff'

type ReviewFinding = {
  severity: string
  summary: string
  path?: string | null
  line?: number | null
  detail?: string
}

type ReviewResult = {
  verdict: ReviewVerdict
  summary: string
  findings: ReviewFinding[]
  severity_counts: Record<string, number>
  finding_count: number
  source_event_seq?: number | null
  supersedes_round_index?: number | null
}

type Task = {
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

type EventRecord = {
  id: string
  kind: string
  level: 'info' | 'warning' | 'error'
  seq: number
  ts: number
  payload: Record<string, unknown>
}

type ArtifactRecord = {
  path: string
  relative_path: string
}

type ReviewRound = {
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

type EventStats = {
  all_count: number
  important_count: number
  hidden_count: number
}

type TaskDetailResponse = {
  task: Task
  events: EventRecord[]
  important_events?: EventRecord[]
  event_stats?: EventStats
  latest_review_result?: ReviewResult | null
  review_rounds?: ReviewRound[]
  runs?: ReviewRound[]
  artifacts: ArtifactRecord[]
}

type TaskDetail = {
  task: Task
  events: EventRecord[]
  importantEvents: EventRecord[]
  eventStats: EventStats
  latestReviewResult: ReviewResult | null
  reviewRounds: ReviewRound[]
  artifacts: ArtifactRecord[]
}

type DetailTab = 'results' | 'timeline' | 'rounds' | 'artifacts'
type Notice = { tone: 'error' | 'info'; text: string }
type Theme = 'light' | 'dark'

const EVENT_LIMIT = 40
const SEVERITY_ORDER = ['critical', 'high', 'medium', 'low'] as const
const THEME_KEY = 'amc-theme'

const api = async <T,>(path: string, init?: RequestInit): Promise<T> => {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!response.ok) {
    const text = await response.text()
    let message = text
    try {
      const payload = JSON.parse(text) as { detail?: unknown }
      if (typeof payload.detail === 'string') message = payload.detail
    } catch {
      // Keep a non-JSON response as-is.
    }
    throw new Error(message || `Request failed: ${response.status}`)
  }
  return response.json() as Promise<T>
}

const normalizeTaskDetail = (payload: TaskDetailResponse): TaskDetail => {
  const events = payload.events
  const importantEvents = payload.important_events ?? events
  return {
    task: payload.task,
    events,
    importantEvents,
    eventStats: payload.event_stats ?? {
      all_count: events.length,
      important_count: importantEvents.length,
      hidden_count: Math.max(events.length - importantEvents.length, 0),
    },
    latestReviewResult: payload.latest_review_result ?? payload.task.latest_review_result ?? null,
    reviewRounds: payload.review_rounds ?? payload.runs ?? [],
    artifacts: payload.artifacts,
  }
}

const timeText = (ts?: number | null) => (ts ? new Date(ts * 1000).toLocaleString() : '—')

const relativeTimeText = (ts?: number) => {
  if (!ts) return '—'
  const diffSec = Math.max(0, Math.floor(Date.now() / 1000 - ts))
  if (diffSec < 60) return `${diffSec} 秒前`
  const diffMin = Math.floor(diffSec / 60)
  if (diffMin < 60) return `${diffMin} 分钟前`
  const diffHour = Math.floor(diffMin / 60)
  if (diffHour < 24) return `${diffHour} 小时前`
  return `${Math.floor(diffHour / 24)} 天前`
}

const durationText = (start?: number, end?: number | null) => {
  if (!start || !end) return '—'
  const sec = Math.max(0, Math.round(end - start))
  if (sec < 60) return `${sec}s`
  return `${Math.floor(sec / 60)}m ${sec % 60}s`
}

const shortSha = (value?: string | null) => (value ? value.slice(0, 10) : '—')

const stringifyPayload = (payload: Record<string, unknown>) => {
  if (typeof payload.text === 'string') return payload.text
  if (typeof payload.message === 'string') return payload.message
  if (typeof payload.summary === 'string') return payload.summary
  return JSON.stringify(payload, null, 2)
}

const previewText = (value: string, limit = 240) =>
  value.length <= limit ? value : `${value.slice(0, limit)}…`

const statusTone = (status: TaskStatus) => {
  switch (status) {
    case 'completed':
      return 'success'
    case 'failed':
    case 'aborted':
      return 'danger'
    case 'running':
    case 'ingesting':
    case 'validating':
      return 'accent'
    default:
      return 'neutral'
  }
}

const verdictTone = (verdict?: ReviewVerdict | null) => {
  switch (verdict) {
    case 'clear':
      return 'success'
    case 'concerns':
      return 'warning'
    case 'failed':
      return 'danger'
    default:
      return 'neutral'
  }
}

const statusLabel = (status: TaskStatus) => {
  switch (status) {
    case 'created':
      return '已创建'
    case 'context_ready':
      return '待审查'
    case 'ingesting':
      return '准备中'
    case 'running':
      return '审查中'
    case 'waiting_human':
      return '待处理'
    case 'validating':
      return '验证中'
    case 'completed':
      return '已完成'
    case 'failed':
      return '失败'
    case 'aborted':
      return '已中止'
    default:
      return status
  }
}

const stageLabel = (stage: string) => {
  switch (stage) {
    case 'created':
      return '已创建'
    case 'review_ready':
      return '待审查'
    case 'prepare_review':
      return '准备上下文'
    case 'review_in_progress':
      return '审查进行中'
    case 'awaiting_next_round':
      return '等待下一轮'
    case 'review_failed':
      return '审查失败'
    case 'review_done':
      return '审查完成'
    case 'interrupted':
      return '被中断'
    case 'aborted':
      return '已中止'
    case 'validate':
      return '验证中'
    case 'handoff':
      return '待交接'
    default:
      return stage
  }
}

const verdictLabel = (verdict?: ReviewVerdict | null) => {
  switch (verdict) {
    case 'clear':
      return '未发现明显问题'
    case 'concerns':
      return '需要关注'
    case 'failed':
      return '审查失败'
    case 'inconclusive':
      return '结论不明确'
    default:
      return '暂无结果'
  }
}

const severityLabel = (severity: string) => {
  switch (severity) {
    case 'critical':
      return '严重'
    case 'high':
      return '高'
    case 'medium':
      return '中'
    case 'low':
      return '低'
    default:
      return severity
  }
}

const tabLabel = (tab: DetailTab) => {
  switch (tab) {
    case 'results':
      return '审查结果'
    case 'timeline':
      return '事件流'
    case 'rounds':
      return '审查轮次'
    default:
      return '产物'
  }
}

const prShortLabel = (task: Task) => {
  if (task.source_type === 'local_diff') {
    return `本地变更 · ${task.pr_base_ref || '自动基准'}`
  }
  if (task.pr_owner && task.pr_repo && task.pr_number) {
    return `${task.pr_owner}/${task.pr_repo} #${task.pr_number}`
  }
  return task.source_url ?? null
}

/* ── Icons (inline, no dependency) ──────────────────────── */

const SunIcon = () => (
  <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
    strokeLinecap="round" aria-hidden="true">
    <circle cx="12" cy="12" r="4" />
    <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
  </svg>
)

const MoonIcon = () => (
  <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
    strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
  </svg>
)

const MenuIcon = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
    strokeLinecap="round" aria-hidden="true">
    <path d="M3 6h18M3 12h18M3 18h18" />
  </svg>
)

/* ── Event row ──────────────────────────────────────────── */

function EventRow({ event }: { event: EventRecord }) {
  const fullText = stringifyPayload(event.payload)
  const shortText = previewText(fullText)
  const truncated = shortText !== fullText

  return (
    <article className={`event lvl-${event.level}`}>
      <div className="event-head">
        <span className="event-kind">{event.kind}</span>
        <span className="event-seq">#{event.seq}</span>
        <span className="event-time">{timeText(event.ts)}</span>
      </div>
      {shortText && <div className="event-text">{shortText}</div>}
      {truncated && (
        <details>
          <summary>查看完整输出</summary>
          <pre>{fullText}</pre>
        </details>
      )}
    </article>
  )
}

/* ── App ────────────────────────────────────────────────── */

export function App() {
  const [tasks, setTasks] = useState<Task[]>([])
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null)
  const [detail, setDetail] = useState<TaskDetail | null>(null)
  const [repoPath, setRepoPath] = useState('')
  const [reviewSource, setReviewSource] = useState<ReviewSource>('pull_request')
  const [prUrl, setPrUrl] = useState('')
  const [baseBranch, setBaseBranch] = useState('')
  const [reviewFocus, setReviewFocus] = useState('')
  const [isCreating, setIsCreating] = useState(false)
  const [notice, setNotice] = useState<Notice | null>(null)
  const [detailTab, setDetailTab] = useState<DetailTab>('results')
  const [showAllEvents, setShowAllEvents] = useState(false)
  const [showModal, setShowModal] = useState(false)
  const [navOpen, setNavOpen] = useState(false)
  const [theme, setTheme] = useState<Theme>(() => {
    if (typeof window === 'undefined') return 'light'
    const stored = window.localStorage.getItem(THEME_KEY)
    if (stored === 'light' || stored === 'dark') return stored
    return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
  })
  const eventSourceRef = useRef<EventSource | null>(null)

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    window.localStorage.setItem(THEME_KEY, theme)
  }, [theme])

  const selectedTask = useMemo(
    () => tasks.find((task) => task.id === selectedTaskId) ?? detail?.task ?? null,
    [detail?.task, selectedTaskId, tasks],
  )

  const visibleEvents = useMemo(() => {
    if (!detail) return []
    const source = showAllEvents ? detail.events : detail.importantEvents
    return showAllEvents ? source : source.slice(-EVENT_LIMIT)
  }, [detail, showAllEvents])

  const orderedRounds = useMemo(
    () => (detail ? [...detail.reviewRounds].sort((a, b) => a.round_index - b.round_index) : []),
    [detail],
  )
  const latestRound = orderedRounds.length ? orderedRounds[orderedRounds.length - 1] : undefined
  const latestResult = detail?.latestReviewResult ?? selectedTask?.latest_review_result ?? null

  // `refreshTasks` runs on a 4s timer. It must never clear a notice raised by a
  // user action, otherwise failed actions silently look like no-ops.
  const refreshTasks = useCallback(async (options?: { silent?: boolean }) => {
    try {
      const data = await api<Task[]>('/api/tasks')
      setTasks(data.sort((a, b) => b.updated_at - a.updated_at))
    } catch (err) {
      if (!options?.silent) {
        setNotice({ tone: 'error', text: err instanceof Error ? err.message : '任务列表加载失败' })
      }
    }
  }, [])

  const loadTask = useCallback(async (taskId: string, options?: { silent?: boolean }) => {
    try {
      const data = normalizeTaskDetail(await api<TaskDetailResponse>(`/api/tasks/${taskId}`))
      setDetail(data)
    } catch (err) {
      if (!options?.silent) {
        setNotice({ tone: 'error', text: err instanceof Error ? err.message : '任务详情加载失败' })
      }
    }
  }, [])

  useEffect(() => {
    void refreshTasks()
    const timer = window.setInterval(() => void refreshTasks({ silent: true }), 4000)
    return () => window.clearInterval(timer)
  }, [refreshTasks])

  useEffect(() => {
    if (!selectedTaskId) return
    setDetailTab('results')
    setShowAllEvents(false)
    void loadTask(selectedTaskId)
  }, [selectedTaskId, loadTask])

  useEffect(() => {
    if (!selectedTaskId) return
    eventSourceRef.current?.close()
    const source = new EventSource(`/api/tasks/${selectedTaskId}/stream`)
    eventSourceRef.current = source
    source.onmessage = () => {
      void loadTask(selectedTaskId, { silent: true })
      void refreshTasks({ silent: true })
    }
    source.onerror = () => source.close()
    return () => {
      source.close()
      eventSourceRef.current = null
    }
  }, [selectedTaskId, loadTask, refreshTasks])

  useEffect(() => {
    if (!showModal) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setShowModal(false) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [showModal])

  const createTask = async () => {
    if (!repoPath.trim() || (reviewSource === 'pull_request' && !prUrl.trim())) {
      setNotice({
        tone: 'error',
        text: reviewSource === 'pull_request' ? '请填写仓库路径和 GitHub PR 链接。' : '请填写仓库路径。',
      })
      return
    }
    if (isCreating) return
    setIsCreating(true)
    try {
      const task = await api<Task>('/api/tasks', {
        method: 'POST',
        body: JSON.stringify({
          repo_path: repoPath.trim(),
          source_type: reviewSource,
          pr_url: reviewSource === 'pull_request' ? prUrl.trim() : null,
          base: reviewSource === 'local_diff' ? baseBranch.trim() || null : null,
          review_focus: reviewFocus.trim(),
        }),
      })
      setRepoPath(repoPath.trim())
      setPrUrl('')
      setBaseBranch('')
      setReviewFocus('')
      setShowModal(false)
      setNotice(null)
      await refreshTasks()
      setSelectedTaskId(task.id)
    } catch (err) {
      setNotice({ tone: 'error', text: err instanceof Error ? err.message : '创建审查任务失败' })
    } finally {
      setIsCreating(false)
    }
  }

  const startReviewRound = async (taskId: string) => {
    try {
      await api(`/api/tasks/${taskId}/start`, { method: 'POST', body: JSON.stringify({}) })
      setNotice(null)
      await loadTask(taskId)
      await refreshTasks()
    } catch (err) {
      setNotice({ tone: 'error', text: err instanceof Error ? err.message : '启动审查失败' })
    }
  }

  const abortTask = async (taskId: string) => {
    try {
      await api(`/api/tasks/${taskId}/abort`, { method: 'POST' })
      setNotice(null)
      await loadTask(taskId)
      await refreshTasks()
    } catch (err) {
      setNotice({ tone: 'error', text: err instanceof Error ? err.message : '中止审查失败' })
    }
  }

  const exportSummary = async (taskId: string) => {
    try {
      const result = await api<{ path: string }>(`/api/tasks/${taskId}/export-summary`, { method: 'POST' })
      setNotice({ tone: 'info', text: `摘要已导出到 ${result.path}` })
    } catch (err) {
      setNotice({ tone: 'error', text: err instanceof Error ? err.message : '导出摘要失败' })
    }
  }

  const selectTask = (id: string) => {
    setSelectedTaskId(id)
    setNavOpen(false)
  }

  const tabCounts: Record<DetailTab, number> = {
    results: latestResult?.finding_count ?? 0,
    timeline: detail?.eventStats.important_count ?? 0,
    rounds: orderedRounds.length,
    artifacts: detail?.artifacts.length ?? 0,
  }

  const isRunning = selectedTask?.status === 'running'
  const startLabel = latestRound ? '发起下一轮审查' : '开始首轮审查'

  return (
    <div className="layout">
      <header className="topbar">
        <button
          className="icon-btn sidebar-toggle"
          onClick={() => setNavOpen((v) => !v)}
          aria-label="切换任务列表"
          aria-expanded={navOpen}
        >
          <MenuIcon />
        </button>
        <div className="brand">
          <span className="brand-mark">Mission Control</span>
          <span className="brand-sub">AI 代码审查控制台</span>
        </div>
        <div className="topbar-actions">
          <button
            className="icon-btn"
            onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
            aria-label={theme === 'dark' ? '切换到浅色主题' : '切换到深色主题'}
            title={theme === 'dark' ? '浅色主题' : '深色主题'}
          >
            {theme === 'dark' ? <SunIcon /> : <MoonIcon />}
          </button>
          <button className="btn btn-primary" onClick={() => setShowModal(true)}>
            新建审查
          </button>
        </div>
      </header>

      <div className="body">
        {navOpen && <div className="scrim" onClick={() => setNavOpen(false)} />}

        <nav className={`sidebar ${navOpen ? 'open' : ''}`} aria-label="审查任务">
          <div className="sidebar-heading">
            <span>审查任务</span>
            <span className="sidebar-count">{tasks.length}</span>
          </div>
          {tasks.map((task) => {
            const verdict = task.latest_review_result?.verdict
            const count = task.latest_review_result?.finding_count ?? 0
            return (
              <button
                key={task.id}
                className={`task-tile ${task.id === selectedTaskId ? 'active' : ''}`}
                onClick={() => selectTask(task.id)}
                aria-current={task.id === selectedTaskId}
              >
                <div className={`tile-status tone-${statusTone(task.status)}`}>
                  <span className={`dot ${task.status === 'running' ? 'pulsing' : ''}`} />
                  {statusLabel(task.status)}
                </div>
                <div className="tile-title">{task.title}</div>
                <div className="tile-foot">
                  {verdict ? (
                    <span className={`pill tone-${verdictTone(verdict)}`}>{verdictLabel(verdict)}</span>
                  ) : (
                    <span className="pill">未运行</span>
                  )}
                  {count > 0 && <span>{count} 条</span>}
                  <span className="spacer">{relativeTimeText(task.updated_at)}</span>
                </div>
              </button>
            )
          })}
          {!tasks.length && <div className="empty-note">还没有审查任务</div>}
        </nav>

        <main className="main">
          <div className="main-inner">
            {notice && (
              <div className={`banner ${notice.tone === 'error' ? 'is-error' : 'is-info'}`} role="status">
                <span>{notice.text}</span>
                <button onClick={() => setNotice(null)} aria-label="关闭提示">✕</button>
              </div>
            )}

            {!selectedTask && (
              <div className="empty-state">
                <h2>选择一个审查任务</h2>
                <p>从左侧列表挑一个任务查看结论与问题列表，或新建一次 PR 审查。</p>
              </div>
            )}

            {selectedTask && detail && (
              <>
                <section className="detail-head">
                  <div className="detail-eyebrow">
                    <span className={`tone-${statusTone(selectedTask.status)}`}
                      style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                      <span className={`dot ${isRunning ? 'pulsing' : ''}`} />
                      {statusLabel(selectedTask.status)}
                    </span>
                    <span>·</span>
                    <span>{stageLabel(selectedTask.current_stage)}</span>
                    {prShortLabel(selectedTask) && (
                      <>
                        <span>·</span>
                        {selectedTask.source_url ? (
                          <a href={selectedTask.source_url} target="_blank" rel="noreferrer">
                            {prShortLabel(selectedTask)}
                          </a>
                        ) : (
                          <span>{prShortLabel(selectedTask)}</span>
                        )}
                      </>
                    )}
                  </div>

                  <h1 className="detail-title">{selectedTask.title}</h1>

                  <div className="detail-bar">
                    <button
                      className="btn btn-primary"
                      onClick={() => startReviewRound(selectedTask.id)}
                      disabled={isRunning}
                    >
                      {startLabel}
                    </button>
                    <button className="btn btn-ghost" onClick={() => exportSummary(selectedTask.id)}>
                      导出摘要
                    </button>
                    <button
                      className="btn btn-danger"
                      onClick={() => abortTask(selectedTask.id)}
                      disabled={!isRunning}
                    >
                      中止
                    </button>
                  </div>

                  <div className="meta-row">
                    <span className="meta-item">
                      <span className="meta-key">仓库</span>
                      <span className="meta-val mono" title={selectedTask.repo_path}>{selectedTask.repo_path}</span>
                    </span>
                    <span className="meta-item">
                      <span className="meta-key">后端</span>
                      <span className="meta-val">{selectedTask.backend}</span>
                    </span>
                    <span className="meta-item">
                      <span className="meta-key">轮次</span>
                      <span className="meta-val">{orderedRounds.length}</span>
                    </span>
                    <span className="meta-item">
                      <span className="meta-key">Commit</span>
                      <span className="meta-val mono">
                        {shortSha(latestRound?.review_revision ?? selectedTask.pr_head_sha)}
                      </span>
                    </span>
                    <span className="meta-item">
                      <span className="meta-key">更新</span>
                      <span className="meta-val">{relativeTimeText(selectedTask.updated_at)}</span>
                    </span>
                  </div>
                </section>

                <section className={`verdict is-${verdictTone(latestResult?.verdict)}`}>
                  <div className="verdict-top">
                    <span className="verdict-word">{verdictLabel(latestResult?.verdict)}</span>
                    {latestResult && (
                      <span className="verdict-count">
                        {latestResult.finding_count} 条问题
                        {latestResult.supersedes_round_index
                          ? ` · 第 ${latestResult.supersedes_round_index} 轮`
                          : ''}
                      </span>
                    )}
                  </div>
                  <p className="verdict-summary">
                    {latestResult?.summary || '这个任务还没有产出结构化的审查结论。'}
                  </p>
                  {latestResult && latestResult.finding_count > 0 && (
                    <div className="sev-chips">
                      {SEVERITY_ORDER.map((severity) => {
                        const n = latestResult.severity_counts?.[severity] ?? 0
                        return (
                          <span key={severity} className={`sev-chip ${n > 0 ? `on-${severity}` : ''}`}>
                            {severityLabel(severity)}
                            <span className="n">{n}</span>
                          </span>
                        )
                      })}
                    </div>
                  )}
                </section>

                {selectedTask.description && (
                  <details className="description">
                    <summary>任务背景</summary>
                    <pre>{selectedTask.description}</pre>
                  </details>
                )}

                <div className="tabs" role="tablist">
                  {(['results', 'timeline', 'rounds', 'artifacts'] as DetailTab[]).map((tab) => (
                    <button
                      key={tab}
                      role="tab"
                      aria-selected={detailTab === tab}
                      className={`tab ${detailTab === tab ? 'active' : ''}`}
                      onClick={() => setDetailTab(tab)}
                    >
                      {tabLabel(tab)}
                      <span className="tab-badge">{tabCounts[tab]}</span>
                    </button>
                  ))}
                </div>

                {detailTab === 'results' && (
                  <>
                    {latestResult && latestResult.findings.length > 0 ? (
                      <>
                        <div className="section-label">
                          <span>问题列表</span>
                          <span className="count">{latestResult.finding_count} 条</span>
                        </div>
                        <div className="findings">
                          {latestResult.findings.map((finding, index) => (
                            <article
                              className={`finding sev-${finding.severity}`}
                              key={`${finding.severity}-${finding.path ?? 'na'}-${index}`}
                            >
                              <div className="finding-top">
                                <span className="sev-tag">{severityLabel(finding.severity)}</span>
                                <span className="finding-loc">
                                  {finding.path
                                    ? `${finding.path}${finding.line ? `:${finding.line}` : ''}`
                                    : '未指明位置'}
                                </span>
                              </div>
                              <div className="finding-title">{finding.summary}</div>
                              {finding.detail && finding.detail !== finding.summary && (
                                <p className="finding-body">{finding.detail}</p>
                              )}
                            </article>
                          ))}
                        </div>
                      </>
                    ) : (
                      <div className="empty-note">
                        {latestResult ? '这一轮没有产出结构化问题。' : '还没有审查结果，先跑一轮吧。'}
                      </div>
                    )}
                  </>
                )}

                {detailTab === 'timeline' && (
                  <>
                    <div className="timeline-bar">
                      <span>
                        {showAllEvents
                          ? `全部 ${detail.eventStats.all_count} 条事件`
                          : `${detail.eventStats.important_count} 条重要事件`}
                      </span>
                      {detail.eventStats.hidden_count > 0 && (
                        <button className="btn btn-ghost" onClick={() => setShowAllEvents((v) => !v)}>
                          {showAllEvents ? '仅看重要事件' : `显示全部 ${detail.eventStats.all_count} 条`}
                        </button>
                      )}
                    </div>
                    {visibleEvents.length ? (
                      <div className="events">
                        {visibleEvents.map((event) => <EventRow key={event.id} event={event} />)}
                      </div>
                    ) : (
                      <div className="empty-note">暂无事件</div>
                    )}
                  </>
                )}

                {detailTab === 'rounds' && (
                  <>
                    {orderedRounds.length ? (
                      <div className="rounds">
                        {[...orderedRounds].reverse().map((round) => (
                          <article className="round" key={round.id}>
                            <div className="round-top">
                              <span className="round-n">第 {round.round_index} 轮</span>
                              <span className={`pill tone-${verdictTone(round.review_result?.verdict)}`}>
                                {verdictLabel(round.review_result?.verdict)}
                              </span>
                              {(round.review_result?.finding_count ?? 0) > 0 && (
                                <span className="verdict-count">
                                  {round.review_result?.finding_count} 条问题
                                </span>
                              )}
                            </div>
                            {round.review_note && <div className="round-note">{round.review_note}</div>}
                            {round.review_result?.summary && (
                              <p className="round-summary">{round.review_result.summary}</p>
                            )}
                            <div className="kv-grid">
                              <div className="kv">
                                <span className="kv-k">后端</span>
                                <span className="kv-v">{round.backend}</span>
                              </div>
                              <div className="kv">
                                <span className="kv-k">状态</span>
                                <span className="kv-v">{round.status}</span>
                              </div>
                              <div className="kv">
                                <span className="kv-k">耗时</span>
                                <span className="kv-v">{durationText(round.started_at, round.ended_at)}</span>
                              </div>
                              <div className="kv">
                                <span className="kv-k">退出码</span>
                                <span className="kv-v">{String(round.exit_code ?? '—')}</span>
                              </div>
                              <div className="kv">
                                <span className="kv-k">Commit</span>
                                <span className="kv-v mono">{shortSha(round.review_revision)}</span>
                              </div>
                              <div className="kv">
                                <span className="kv-k">开始于</span>
                                <span className="kv-v">{timeText(round.started_at)}</span>
                              </div>
                            </div>
                          </article>
                        ))}
                      </div>
                    ) : (
                      <div className="empty-note">还没有跑过任何一轮审查</div>
                    )}
                  </>
                )}

                {detailTab === 'artifacts' && (
                  <>
                    {detail.artifacts.length ? (
                      <div className="artifacts">
                        {detail.artifacts.map((artifact) => (
                          <div className="artifact" key={artifact.path}>
                            <span className="artifact-name">{artifact.relative_path}</span>
                            <span className="artifact-path" title={artifact.path}>{artifact.path}</span>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <div className="empty-note">暂无产物文件</div>
                    )}
                  </>
                )}
              </>
            )}
          </div>
        </main>
      </div>

      {showModal && (
        <div className="modal-overlay" onClick={() => setShowModal(false)} role="presentation">
          <div
            className="modal"
            onClick={(event) => event.stopPropagation()}
            role="dialog"
            aria-modal="true"
            aria-label="新建代码审查"
          >
            <h2>新建代码审查</h2>
            <p className="modal-hint">
              {reviewSource === 'pull_request'
                ? '拉取 GitHub PR 的 diff 与讨论，编译上下文后交给 AI 后端审查。'
                : '审查本地仓库相对基准分支的提交、暂存区和工作区变更。'}
            </p>
            <div className="mode-switch" role="group" aria-label="审查来源">
              <button
                type="button"
                className={reviewSource === 'pull_request' ? 'active' : ''}
                aria-pressed={reviewSource === 'pull_request'}
                onClick={() => setReviewSource('pull_request')}
              >
                GitHub PR
              </button>
              <button
                type="button"
                className={reviewSource === 'local_diff' ? 'active' : ''}
                aria-pressed={reviewSource === 'local_diff'}
                onClick={() => setReviewSource('local_diff')}
              >
                本地变更
              </button>
            </div>
            <label className="field">
              <span className="field-label">本地仓库路径</span>
              <input
                value={repoPath}
                onChange={(event) => setRepoPath(event.target.value)}
                placeholder="C:\\path\\to\\repo"
                autoFocus
              />
            </label>
            {reviewSource === 'pull_request' ? (
              <label className="field">
                <span className="field-label">GitHub PR 链接</span>
                <input
                  value={prUrl}
                  onChange={(event) => setPrUrl(event.target.value)}
                  placeholder="https://github.com/org/repo/pull/123"
                />
              </label>
            ) : (
              <label className="field">
                <span className="field-label">基准分支（可选）</span>
                <input
                  value={baseBranch}
                  onChange={(event) => setBaseBranch(event.target.value)}
                  placeholder="自动检测 main 或 master"
                />
              </label>
            )}
            <label className="field">
              <span className="field-label">审查重点（可选）</span>
              <textarea
                value={reviewFocus}
                onChange={(event) => setReviewFocus(event.target.value)}
                rows={3}
                placeholder="想让审查特别关注的风险点、模块、兼容性或测试缺口"
              />
            </label>
            <div className="modal-actions">
              <button className="btn btn-ghost" disabled={isCreating} onClick={() => setShowModal(false)}>取消</button>
              <button
                className="btn btn-primary"
                disabled={isCreating || !repoPath.trim() || (reviewSource === 'pull_request' && !prUrl.trim())}
                onClick={createTask}
              >
                {isCreating ? '正在创建…' : '创建审查'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
