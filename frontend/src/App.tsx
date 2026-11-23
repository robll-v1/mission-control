import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Settings } from './Settings'
import { StartTracker } from './startTracker'
import {
  DEFAULT_REVIEW_LANGUAGE,
  errorMessage,
  labelFor,
  messages,
  otherLanguage,
  readStoredLanguage,
  storeLanguage,
  type Language,
  type Localized,
  type Messages,
} from './i18n'

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
// Resolved when rendered, so a notice on screen follows a language switch.
// It stays inside an object: React calls a bare function passed to a state
// setter as an updater.
type Notice = { tone: 'error' | 'info'; text: Localized }
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

const relativeTimeText = (t: Messages, ts?: number) => {
  if (!ts) return '—'
  const diffSec = Math.max(0, Math.floor(Date.now() / 1000 - ts))
  if (diffSec < 60) return t.time.secondsAgo(diffSec)
  const diffMin = Math.floor(diffSec / 60)
  if (diffMin < 60) return t.time.minutesAgo(diffMin)
  const diffHour = Math.floor(diffMin / 60)
  if (diffHour < 24) return t.time.hoursAgo(diffHour)
  return t.time.daysAgo(Math.floor(diffHour / 24))
}

const durationText = (start?: number, end?: number | null) => {
  if (!start || !end) return '—'
  const sec = Math.max(0, Math.round(end - start))
  if (sec < 60) return `${sec}s`
  return `${Math.floor(sec / 60)}m ${sec % 60}s`
}

const shortSha = (value?: string | null) => (value ? value.slice(0, 10) : '—')

// A round is in flight from backend preflight through the agent run. Both
// phases can be aborted, so both count as active.
const isRoundActive = (status?: TaskStatus | null) => status === 'ingesting' || status === 'running'

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

// Status and stage names come from the backend, which can add new ones;
// an unknown name is shown as is.
const statusLabel = (t: Messages, status: TaskStatus) => labelFor(t.status, status)

const stageLabel = (t: Messages, stage: string) => labelFor(t.stage, stage)

const verdictLabel = (t: Messages, verdict?: ReviewVerdict | null) =>
  verdict && Object.prototype.hasOwnProperty.call(t.verdict, verdict) ? t.verdict[verdict] : t.result.none

const severityLabel = (t: Messages, severity: string) => labelFor(t.severity, severity)

const tabLabel = (t: Messages, tab: DetailTab) => t.tabs[tab]

const prShortLabel = (t: Messages, task: Task) => {
  if (task.source_type === 'local_diff') {
    return t.source.localChanges(task.pr_base_ref || null)
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

const SlidersIcon = () => (
  <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
    strokeLinecap="round" aria-hidden="true">
    <path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6" />
  </svg>
)

const MenuIcon = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
    strokeLinecap="round" aria-hidden="true">
    <path d="M3 6h18M3 12h18M3 18h18" />
  </svg>
)

const PlusIcon = () => (
  <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
    strokeLinecap="round" aria-hidden="true">
    <path d="M12 5v14M5 12h14" />
  </svg>
)

/* ── Event row ──────────────────────────────────────────── */

function EventRow({ event, t }: { event: EventRecord; t: Messages }) {
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
          <summary>{t.timeline.fullOutput}</summary>
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
  const [showSettings, setShowSettings] = useState(false)
  // Start requests stay pending through preflight and prepare; one record
  // per task, so actions on another task cannot affect this one.
  const startTrackerRef = useRef(new StartTracker())
  const [startingTaskIds, setStartingTaskIds] = useState<string[]>([])
  const [theme, setTheme] = useState<Theme>(() => {
    if (typeof window === 'undefined') return 'light'
    const stored = window.localStorage.getItem(THEME_KEY)
    if (stored === 'light' || stored === 'dark') return stored
    return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
  })
  const eventSourceRef = useRef<EventSource | null>(null)
  // English unless the user picked another language before.
  const [language, setLanguage] = useState<Language>(() => readStoredLanguage())
  const t = messages[language]
  const languageOffer = otherLanguage(language)

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    window.localStorage.setItem(THEME_KEY, theme)
  }, [theme])

  // Screen readers and CJK font fallback both read the page's lang.
  useEffect(() => {
    document.documentElement.lang = language
  }, [language])

  // Only an explicit choice is stored, so the default can still change later.
  const chooseLanguage = (next: Language) => {
    setLanguage(next)
    storeLanguage(next)
  }

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
        setNotice({ tone: 'error', text: errorMessage(err, (t) => t.errors.loadReviews) })
      }
    }
  }, [])

  const loadTask = useCallback(async (taskId: string, options?: { silent?: boolean }) => {
    try {
      const data = normalizeTaskDetail(await api<TaskDetailResponse>(`/api/tasks/${taskId}`))
      setDetail(data)
    } catch (err) {
      if (!options?.silent) {
        setNotice({ tone: 'error', text: errorMessage(err, (t) => t.errors.loadReview) })
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
        text: reviewSource === 'pull_request' ? (t) => t.errors.missingRepoAndPr : (t) => t.errors.missingRepo,
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
      setNotice({ tone: 'error', text: errorMessage(err, (t) => t.errors.createReview) })
    } finally {
      setIsCreating(false)
    }
  }

  const startReviewRound = async (taskId: string) => {
    const tracker = startTrackerRef.current
    const request = tracker.begin(taskId)
    setStartingTaskIds(tracker.pendingTaskIds())
    try {
      // The review output language is its own setting, not the interface
      // language. The current backend ignores the field (pydantic drops
      // unknown keys) and keeps its own default until it reads it.
      await api(`/api/tasks/${taskId}/start`, {
        method: 'POST',
        body: JSON.stringify({ language: DEFAULT_REVIEW_LANGUAGE }),
      })
      setNotice(null)
      await loadTask(taskId)
      await refreshTasks()
    } catch (err) {
      // Decided per request: waits for any abort of this start to answer,
      // so it holds whichever response arrives first.
      if (!(await tracker.wasAborted(request))) {
        setNotice({ tone: 'error', text: errorMessage(err, (t) => t.errors.startRound) })
      }
      // Failed or aborted, the task has moved on; show where it ended up.
      void loadTask(taskId, { silent: true })
      void refreshTasks({ silent: true })
    } finally {
      tracker.end(request)
      setStartingTaskIds(tracker.pendingTaskIds())
    }
  }

  const abortTask = async (taskId: string) => {
    // Attach before sending: the start request can fail with "cancelled"
    // before this abort's own response arrives.
    let settle: (accepted: boolean) => void = () => {}
    startTrackerRef.current.noteAbort(taskId, new Promise<boolean>((resolve) => { settle = resolve }))
    try {
      await api(`/api/tasks/${taskId}/abort`, { method: 'POST' })
      settle(true)
      setNotice(null)
      await loadTask(taskId)
      await refreshTasks()
    } catch (err) {
      settle(false)
      setNotice({ tone: 'error', text: errorMessage(err, (t) => t.errors.cancelRound) })
    }
  }

  const exportSummary = async (taskId: string) => {
    try {
      const result = await api<{ path: string }>(`/api/tasks/${taskId}/export-summary`, { method: 'POST' })
      setNotice({ tone: 'info', text: (t) => t.notices.exported(result.path) })
    } catch (err) {
      setNotice({ tone: 'error', text: errorMessage(err, (t) => t.errors.exportSummary) })
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

  const roundActive = isRoundActive(selectedTask?.status)
  const isStarting = selectedTask !== null && startingTaskIds.includes(selectedTask.id)
  const startLabel = latestRound ? t.detail.runNextRound : t.detail.runFirstRound

  return (
    <div className="layout">
      <header className="topbar">
        <button
          className={`icon-btn sidebar-toggle ${showSettings ? 'is-hidden' : ''}`}
          onClick={() => setNavOpen((v) => !v)}
          aria-label={t.topbar.toggleReviewList}
          aria-expanded={navOpen}
        >
          <MenuIcon />
        </button>
        <div className="brand">
          <span className="brand-mark">Mission Control</span>
          <span className="brand-sub">{t.topbar.tagline}</span>
        </div>
        <div className="topbar-actions">
          <button
            className={`icon-btn ${showSettings ? 'is-active' : ''}`}
            onClick={() => setShowSettings((v) => !v)}
            aria-label={t.topbar.modelSelection}
            aria-pressed={showSettings}
            title={t.topbar.modelSelection}
          >
            <SlidersIcon />
          </button>
          {/* Shows the language it switches to, in that language, so a reader
              of either one can find it. */}
          <button
            className="icon-btn lang-btn"
            onClick={() => chooseLanguage(languageOffer.code)}
            lang={languageOffer.code}
            title={messages[languageOffer.code].topbar.switchToThisLanguage}
          >
            {languageOffer.label}
          </button>
          <button
            className="icon-btn"
            onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
            aria-label={theme === 'dark' ? t.topbar.switchToLight : t.topbar.switchToDark}
            title={theme === 'dark' ? t.topbar.lightTheme : t.topbar.darkTheme}
          >
            {theme === 'dark' ? <SunIcon /> : <MoonIcon />}
          </button>
          {/* Collapses to the plus icon on narrow screens; aria-label keeps its name. */}
          <button
            className="btn btn-primary new-review-btn"
            aria-label={t.topbar.newReview}
            onClick={() => {
              setShowSettings(false)
              setShowModal(true)
            }}
          >
            <PlusIcon />
            <span className="new-review-label">{t.topbar.newReview}</span>
          </button>
        </div>
      </header>

      {showSettings ? (
        <div className="body settings-body">
          <Settings t={t} onBack={() => setShowSettings(false)} />
        </div>
      ) : (
      <div className="body">
        {navOpen && <div className="scrim" onClick={() => setNavOpen(false)} />}

        <nav className={`sidebar ${navOpen ? 'open' : ''}`} aria-label={t.sidebar.heading}>
          <div className="sidebar-heading">
            <span>{t.sidebar.heading}</span>
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
                  <span className={`dot ${isRoundActive(task.status) ? 'pulsing' : ''}`} />
                  {statusLabel(t, task.status)}
                </div>
                <div className="tile-title">{task.title}</div>
                <div className="tile-foot">
                  {verdict ? (
                    <span className={`pill tone-${verdictTone(verdict)}`}>{verdictLabel(t, verdict)}</span>
                  ) : (
                    <span className="pill">{t.sidebar.notRun}</span>
                  )}
                  {count > 0 && <span>{t.counts.findingsCompact(count)}</span>}
                  <span className="spacer">{relativeTimeText(t, task.updated_at)}</span>
                </div>
              </button>
            )
          })}
          {!tasks.length && <div className="empty-note">{t.sidebar.empty}</div>}
        </nav>

        <main className="main">
          <div className="main-inner">
            {notice && (
              <div className={`banner ${notice.tone === 'error' ? 'is-error' : 'is-info'}`} role="status">
                <span>{notice.text(t)}</span>
                <button onClick={() => setNotice(null)} aria-label={t.common.dismiss}>✕</button>
              </div>
            )}

            {!selectedTask && (
              <div className="empty-state">
                <h2>{t.empty.title}</h2>
                <p>{t.empty.body}</p>
              </div>
            )}

            {selectedTask && detail && (
              <>
                <section className="detail-head">
                  <div className="detail-eyebrow">
                    <span className={`tone-${statusTone(selectedTask.status)}`}
                      style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                      <span className={`dot ${roundActive ? 'pulsing' : ''}`} />
                      {statusLabel(t, selectedTask.status)}
                    </span>
                    <span>·</span>
                    <span>{stageLabel(t, selectedTask.current_stage)}</span>
                    {prShortLabel(t, selectedTask) && (
                      <>
                        <span>·</span>
                        {selectedTask.source_url ? (
                          <a href={selectedTask.source_url} target="_blank" rel="noreferrer">
                            {prShortLabel(t, selectedTask)}
                          </a>
                        ) : (
                          <span>{prShortLabel(t, selectedTask)}</span>
                        )}
                      </>
                    )}
                  </div>

                  <h1 className="detail-title">{selectedTask.title}</h1>

                  <div className="detail-bar">
                    <button
                      className="btn btn-primary"
                      onClick={() => startReviewRound(selectedTask.id)}
                      disabled={roundActive || isStarting}
                    >
                      {startLabel}
                    </button>
                    <button className="btn btn-ghost" onClick={() => exportSummary(selectedTask.id)}>
                      {t.detail.exportSummary}
                    </button>
                    <button
                      className="btn btn-danger"
                      onClick={() => abortTask(selectedTask.id)}
                      disabled={!roundActive}
                    >
                      {t.detail.cancelRound}
                    </button>
                  </div>

                  <div className="meta-row">
                    <span className="meta-item">
                      <span className="meta-key">{t.detail.repository}</span>
                      <span className="meta-val mono" title={selectedTask.repo_path}>{selectedTask.repo_path}</span>
                    </span>
                    <span className="meta-item">
                      <span className="meta-key">{t.detail.agent}</span>
                      <span className="meta-val">{selectedTask.backend}</span>
                    </span>
                    <span className="meta-item">
                      <span className="meta-key">{t.detail.rounds}</span>
                      <span className="meta-val">{orderedRounds.length}</span>
                    </span>
                    <span className="meta-item">
                      <span className="meta-key">{t.detail.commit}</span>
                      <span className="meta-val mono">
                        {shortSha(latestRound?.review_revision ?? selectedTask.pr_head_sha)}
                      </span>
                    </span>
                    <span className="meta-item">
                      <span className="meta-key">{t.detail.updated}</span>
                      <span className="meta-val">{relativeTimeText(t, selectedTask.updated_at)}</span>
                    </span>
                  </div>
                </section>

                <section className={`verdict is-${verdictTone(latestResult?.verdict)}`}>
                  <div className="verdict-top">
                    <span className="verdict-word">{verdictLabel(t, latestResult?.verdict)}</span>
                    {latestResult && (
                      <span className="verdict-count">
                        {t.counts.findings(latestResult.finding_count)}
                        {latestResult.supersedes_round_index
                          ? ` · ${t.rounds.title(latestResult.supersedes_round_index)}`
                          : ''}
                      </span>
                    )}
                  </div>
                  <p className="verdict-summary">
                    {latestResult?.summary || t.result.noSummary}
                  </p>
                  {latestResult && latestResult.finding_count > 0 && (
                    <div className="sev-chips">
                      {SEVERITY_ORDER.map((severity) => {
                        const n = latestResult.severity_counts?.[severity] ?? 0
                        return (
                          <span key={severity} className={`sev-chip ${n > 0 ? `on-${severity}` : ''}`}>
                            {severityLabel(t, severity)}
                            <span className="n">{n}</span>
                          </span>
                        )
                      })}
                    </div>
                  )}
                </section>

                {selectedTask.description && (
                  <details className="description">
                    <summary>{t.detail.background}</summary>
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
                      {tabLabel(t, tab)}
                      <span className="tab-badge">{tabCounts[tab]}</span>
                    </button>
                  ))}
                </div>

                {detailTab === 'results' && (
                  <>
                    {latestResult && latestResult.findings.length > 0 ? (
                      <>
                        <div className="section-label">
                          <span>{t.findings.heading}</span>
                          <span className="count">{t.counts.findingsCompact(latestResult.finding_count)}</span>
                        </div>
                        <div className="findings">
                          {latestResult.findings.map((finding, index) => (
                            <article
                              className={`finding sev-${finding.severity}`}
                              key={`${finding.severity}-${finding.path ?? 'na'}-${index}`}
                            >
                              <div className="finding-top">
                                <span className="sev-tag">{severityLabel(t, finding.severity)}</span>
                                <span className="finding-loc">
                                  {finding.path
                                    ? `${finding.path}${finding.line ? `:${finding.line}` : ''}`
                                    : t.findings.noLocation}
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
                        {latestResult ? t.findings.noneThisRound : t.findings.noResultYet}
                      </div>
                    )}
                  </>
                )}

                {detailTab === 'timeline' && (
                  <>
                    <div className="timeline-bar">
                      <span>
                        {showAllEvents
                          ? t.timeline.allEvents(detail.eventStats.all_count)
                          : t.timeline.importantEvents(detail.eventStats.important_count)}
                      </span>
                      {detail.eventStats.hidden_count > 0 && (
                        <button className="btn btn-ghost" onClick={() => setShowAllEvents((v) => !v)}>
                          {showAllEvents ? t.timeline.importantOnly : t.timeline.showAll(detail.eventStats.all_count)}
                        </button>
                      )}
                    </div>
                    {visibleEvents.length ? (
                      <div className="events">
                        {visibleEvents.map((event) => <EventRow key={event.id} event={event} t={t} />)}
                      </div>
                    ) : (
                      <div className="empty-note">{t.timeline.empty}</div>
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
                              <span className="round-n">{t.rounds.title(round.round_index)}</span>
                              <span className={`pill tone-${verdictTone(round.review_result?.verdict)}`}>
                                {verdictLabel(t, round.review_result?.verdict)}
                              </span>
                              {(round.review_result?.finding_count ?? 0) > 0 && (
                                <span className="verdict-count">
                                  {t.counts.findings(round.review_result?.finding_count ?? 0)}
                                </span>
                              )}
                            </div>
                            {round.review_note && <div className="round-note">{round.review_note}</div>}
                            {round.review_result?.summary && (
                              <p className="round-summary">{round.review_result.summary}</p>
                            )}
                            <div className="kv-grid">
                              <div className="kv">
                                <span className="kv-k">{t.rounds.agent}</span>
                                <span className="kv-v">{round.backend}</span>
                              </div>
                              <div className="kv">
                                <span className="kv-k">{t.rounds.status}</span>
                                <span className="kv-v">{round.status}</span>
                              </div>
                              <div className="kv">
                                <span className="kv-k">{t.rounds.duration}</span>
                                <span className="kv-v">{durationText(round.started_at, round.ended_at)}</span>
                              </div>
                              <div className="kv">
                                <span className="kv-k">{t.rounds.exitCode}</span>
                                <span className="kv-v">{String(round.exit_code ?? '—')}</span>
                              </div>
                              <div className="kv">
                                <span className="kv-k">{t.rounds.commit}</span>
                                <span className="kv-v mono">{shortSha(round.review_revision)}</span>
                              </div>
                              <div className="kv">
                                <span className="kv-k">{t.rounds.startedAt}</span>
                                <span className="kv-v">{timeText(round.started_at)}</span>
                              </div>
                            </div>
                          </article>
                        ))}
                      </div>
                    ) : (
                      <div className="empty-note">{t.rounds.empty}</div>
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
                      <div className="empty-note">{t.artifacts.empty}</div>
                    )}
                  </>
                )}
              </>
            )}
          </div>
        </main>
      </div>
      )}

      {showModal && (
        <div className="modal-overlay" onClick={() => setShowModal(false)} role="presentation">
          <div
            className="modal"
            onClick={(event) => event.stopPropagation()}
            role="dialog"
            aria-modal="true"
            aria-label={t.newReview.title}
          >
            <h2>{t.newReview.title}</h2>
            <p className="modal-hint">
              {reviewSource === 'pull_request' ? t.newReview.hintPullRequest : t.newReview.hintLocal}
            </p>
            <div className="mode-switch" role="group" aria-label={t.newReview.source}>
              <button
                type="button"
                className={reviewSource === 'pull_request' ? 'active' : ''}
                aria-pressed={reviewSource === 'pull_request'}
                onClick={() => setReviewSource('pull_request')}
              >
                {t.newReview.pullRequest}
              </button>
              <button
                type="button"
                className={reviewSource === 'local_diff' ? 'active' : ''}
                aria-pressed={reviewSource === 'local_diff'}
                onClick={() => setReviewSource('local_diff')}
              >
                {t.newReview.localChanges}
              </button>
            </div>
            <label className="field">
              <span className="field-label">{t.newReview.repoPath}</span>
              <input
                value={repoPath}
                onChange={(event) => setRepoPath(event.target.value)}
                placeholder="C:\\path\\to\\repo"
                autoFocus
              />
            </label>
            {reviewSource === 'pull_request' ? (
              <label className="field">
                <span className="field-label">{t.newReview.prUrl}</span>
                <input
                  value={prUrl}
                  onChange={(event) => setPrUrl(event.target.value)}
                  placeholder="https://github.com/org/repo/pull/123"
                />
              </label>
            ) : (
              <label className="field">
                <span className="field-label">{t.newReview.baseBranch}</span>
                <input
                  value={baseBranch}
                  onChange={(event) => setBaseBranch(event.target.value)}
                  placeholder={t.newReview.baseBranchPlaceholder}
                />
              </label>
            )}
            <label className="field">
              <span className="field-label">{t.newReview.focus}</span>
              <textarea
                value={reviewFocus}
                onChange={(event) => setReviewFocus(event.target.value)}
                rows={3}
                placeholder={t.newReview.focusPlaceholder}
              />
            </label>
            <div className="modal-actions">
              <button className="btn btn-ghost" disabled={isCreating} onClick={() => setShowModal(false)}>
                {t.common.cancel}
              </button>
              <button
                className="btn btn-primary"
                disabled={isCreating || !repoPath.trim() || (reviewSource === 'pull_request' && !prUrl.trim())}
                onClick={createTask}
              >
                {isCreating ? t.newReview.creating : t.newReview.create}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
