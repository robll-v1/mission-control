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
  type ReviewLanguage,
} from './i18n'
import {
  applyUpdatedFinding,
  buildFixPrompt,
  computeFindingCounts,
  filterFindings,
  findingLocationText,
  isClosedFinding,
  type FindingAction,
  type FindingCounts,
  type FindingFilter,
  type TrackedFinding,
} from './findings'
import {
  buildStartRoundPayload,
  createRunDraft,
  formatRoundSettingsLabel,
  mergeEffectiveIntoDraft,
  resolveReasoningOptions,
  savedForAgent,
  selectDraftModel,
  switchDraftAgent,
  type BackendCapability,
  type EffectiveSettingsResponse,
  type RunDraft,
  type RunSettings,
  type SavedConfig,
} from './runSettings'

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
  parse_mode?: string
  source_event_seq?: number | null
  supersedes_round_index?: number | null
}

type Task = {
  id: string
  title: string
  description: string
  repo_path: string
  backend: string
  model?: string | null
  reasoning?: string | null
  language?: string | null
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
  model?: string | null
  reasoning?: string | null
  language?: string
  settings?: RunSettings | null
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
  findings?: TrackedFinding[]
  finding_counts?: FindingCounts
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
  findings: TrackedFinding[]
  findingCounts: FindingCounts
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
    throw new Error(message)
  }
  return response.json() as Promise<T>
}

const normalizeTaskDetail = (payload: TaskDetailResponse): TaskDetail => {
  const events = payload.events
  const importantEvents = payload.important_events ?? events
  const findings = payload.findings ?? []
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
    findings,
    findingCounts: payload.finding_counts ?? computeFindingCounts(findings),
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
  const [newAgent, setNewAgent] = useState('')
  const [newModel, setNewModel] = useState('')
  const [newReasoning, setNewReasoning] = useState('')
  const [newLanguage, setNewLanguage] = useState<ReviewLanguage>(DEFAULT_REVIEW_LANGUAGE)
  const [backends, setBackends] = useState<BackendCapability[]>([])
  const [savedConfig, setSavedConfig] = useState<SavedConfig | null>(null)
  const [taskEffective, setTaskEffective] = useState<{ taskId: string; data: EffectiveSettingsResponse } | null>(null)
  const [runDraft, setRunDraft] = useState<RunDraft>(() => createRunDraft(null, undefined, null))
  const [isCreating, setIsCreating] = useState(false)
  const [notice, setNotice] = useState<Notice | null>(null)
  const [detailTab, setDetailTab] = useState<DetailTab>('results')
  const [findingFilter, setFindingFilter] = useState<FindingFilter>('open')
  const [updatingFindingIds, setUpdatingFindingIds] = useState<string[]>([])
  const [showAllEvents, setShowAllEvents] = useState(false)
  const [showModal, setShowModal] = useState(false)
  const [navOpen, setNavOpen] = useState(false)
  const [showSettings, setShowSettings] = useState(false)
  // Start requests stay pending through preflight and prepare; one record
  // per task, so actions on another task cannot affect this one.
  const startTrackerRef = useRef(new StartTracker())
  const findingSeqRef = useRef<Map<string, number>>(new Map())
  const [startingTaskIds, setStartingTaskIds] = useState<string[]>([])
  const [theme, setTheme] = useState<Theme>(() => {
    if (typeof window === 'undefined') return 'light'
    try {
      const stored = window.localStorage.getItem(THEME_KEY)
      if (stored === 'light' || stored === 'dark') return stored
    } catch {
      // Storage blocked (e.g. cookies disabled / SecurityError); fall back to system theme.
    }
    return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
  })
  const eventSourceRef = useRef<EventSource | null>(null)
  // English unless the user picked another language before.
  const [language, setLanguage] = useState<Language>(() => readStoredLanguage())
  const t = messages[language]
  const languageOffer = otherLanguage(language)

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    try {
      window.localStorage.setItem(THEME_KEY, theme)
    } catch {
      // Quota or privacy restrictions; theme still applies for the current session.
    }
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

  const selectedTaskIdRef = useRef<string | null>(null)
  selectedTaskIdRef.current = selectedTaskId
  const loadSeqByTaskRef = useRef<Map<string, number>>(new Map())
  const effectiveSeqRef = useRef(0)
  const configSeqRef = useRef(0)
  const wasModalOpenRef = useRef(false)
  const activeDetail = detail && detail.task.id === selectedTaskId ? detail : null

  const selectedTask = useMemo(
    () => tasks.find((task) => task.id === selectedTaskId) ?? activeDetail?.task ?? null,
    [activeDetail?.task, selectedTaskId, tasks],
  )

  const visibleEvents = useMemo(() => {
    if (!activeDetail) return []
    const source = showAllEvents ? activeDetail.events : activeDetail.importantEvents
    return showAllEvents ? source : source.slice(-EVENT_LIMIT)
  }, [activeDetail, showAllEvents])

  const orderedRounds = useMemo(
    () => (activeDetail ? [...activeDetail.reviewRounds].sort((a, b) => a.round_index - b.round_index) : []),
    [activeDetail],
  )
  const latestRound = orderedRounds.length ? orderedRounds[orderedRounds.length - 1] : undefined
  const latestResult = activeDetail?.latestReviewResult ?? selectedTask?.latest_review_result ?? null

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
    if (selectedTaskIdRef.current !== taskId) return
    const nextSeq = (loadSeqByTaskRef.current.get(taskId) ?? 0) + 1
    loadSeqByTaskRef.current.set(taskId, nextSeq)
    try {
      const data = normalizeTaskDetail(await api<TaskDetailResponse>(`/api/tasks/${taskId}`))
      if (selectedTaskIdRef.current !== taskId || loadSeqByTaskRef.current.get(taskId) !== nextSeq) return
      setDetail(data)
    } catch (err) {
      if (selectedTaskIdRef.current !== taskId || loadSeqByTaskRef.current.get(taskId) !== nextSeq) return
      if (!options?.silent) {
        setNotice({ tone: 'error', text: errorMessage(err, (t) => t.errors.loadReview) })
      }
    }
  }, [])

  const loadConfigMeta = useCallback(async () => {
    const seq = ++configSeqRef.current
    try {
      const [detectRes, currentRes] = await Promise.all([
        fetch('/api/config/detect'),
        fetch('/api/config/current'),
      ])
      if (configSeqRef.current !== seq) return
      if (detectRes.ok) {
        const d = (await detectRes.json()) as { backends: BackendCapability[] }
        if (configSeqRef.current === seq) setBackends(d.backends || [])
      }
      if (currentRes.ok) {
        const c = (await currentRes.json()) as { config: SavedConfig }
        if (configSeqRef.current === seq) setSavedConfig(c.config)
      }
    } catch {
      // Non-blocking: UI still works with fallback defaults.
    }
  }, [])

  useEffect(() => {
    void refreshTasks()
    void loadConfigMeta()
    const timer = window.setInterval(() => void refreshTasks({ silent: true }), 4000)
    return () => window.clearInterval(timer)
  }, [refreshTasks, loadConfigMeta])

  useEffect(() => {
    if (!showSettings) {
      void loadConfigMeta()
    }
  }, [showSettings, loadConfigMeta])

  useEffect(() => {
    const justOpened = showModal && !wasModalOpenRef.current
    wasModalOpenRef.current = showModal
    if (!justOpened) return
    // Start in Auto mode (omitted backend/model/reasoning) so the repository's
    // .amc.yaml and global config resolve normally unless the user explicitly
    // overrides the agent for this review.
    setNewAgent('')
    setNewModel('')
    setNewReasoning('')
    setNewLanguage(DEFAULT_REVIEW_LANGUAGE)
  }, [showModal])

  useEffect(() => {
    if (!selectedTask) {
      setTaskEffective(null)
      return
    }
    const targetId = selectedTask.id
    const reqSeq = ++effectiveSeqRef.current
    const params = new URLSearchParams()
    if (selectedTask.repo_path) params.set('repo_path', selectedTask.repo_path)
    if (selectedTask.backend) params.set('backend', selectedTask.backend)
    fetch(`/api/config/effective?${params.toString()}`)
      .then(async (res) => {
        if (!res.ok) return
        const data = (await res.json()) as EffectiveSettingsResponse
        if (selectedTaskIdRef.current !== targetId || effectiveSeqRef.current !== reqSeq) return
        setTaskEffective({ taskId: targetId, data })
      })
      .catch(() => {
        // Non-blocking: falls back to savedConfig defaults.
      })
  }, [selectedTask?.id, selectedTask?.repo_path, selectedTask?.backend, savedConfig])

  const activeTaskEffective =
    selectedTask && taskEffective?.taskId === selectedTask.id ? taskEffective.data : null

  useEffect(() => {
    setRunDraft(createRunDraft(selectedTask, latestRound, savedConfig, activeTaskEffective))
  }, [selectedTask?.id, latestRound?.id])

  useEffect(() => {
    if (!selectedTask) return
    setRunDraft((prev) =>
      mergeEffectiveIntoDraft(prev, selectedTask, latestRound, savedConfig, activeTaskEffective, backends),
    )
  }, [activeTaskEffective, savedConfig, backends, selectedTask?.id, latestRound?.id])

  useEffect(() => {
    if (!selectedTaskId) return
    setDetailTab('results')
    setFindingFilter('open')
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
      const chosenAgent = newAgent.trim()
      const newCap = backends.find((b) => b.name === chosenAgent)
      const newOpts = resolveReasoningOptions(newCap, newModel)
      const body: Record<string, string | null> = {
        repo_path: repoPath.trim(),
        source_type: reviewSource,
        pr_url: reviewSource === 'pull_request' ? prUrl.trim() : null,
        base: reviewSource === 'local_diff' ? baseBranch.trim() || null : null,
        review_focus: reviewFocus.trim(),
        language: newLanguage,
      }
      // Only send backend/model/reasoning when the user explicitly picked an
      // agent; otherwise omit them so repo .amc.yaml & global config apply.
      if (chosenAgent) {
        body.backend = chosenAgent
        if (newModel.trim()) body.model = newModel.trim()
        if (newOpts.supported && newReasoning.trim()) body.reasoning = newReasoning.trim()
      }
      const task = await api<Task>('/api/tasks', {
        method: 'POST',
        body: JSON.stringify(body),
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
    const payload = buildStartRoundPayload(runDraft)
    setStartingTaskIds(tracker.pendingTaskIds())
    try {
      await api(`/api/tasks/${taskId}/start`, {
        method: 'POST',
        body: JSON.stringify(payload),
      })
      if (selectedTaskIdRef.current === taskId) {
        setRunDraft((prev) => ({ ...prev, note: '' }))
      }
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

  const updateFindingStatus = async (taskId: string, findingId: string, action: FindingAction) => {
    const seqKey = `${taskId}:${findingId}`
    const nextFindingSeq = (findingSeqRef.current.get(seqKey) ?? 0) + 1
    findingSeqRef.current.set(seqKey, nextFindingSeq)
    // Advance the task's load sequence so any older in-flight GET /api/tasks/{id}
    // cannot overwrite this mutation when it returns later.
    const patchLoadSeq = (loadSeqByTaskRef.current.get(taskId) ?? 0) + 1
    loadSeqByTaskRef.current.set(taskId, patchLoadSeq)

    setUpdatingFindingIds((prev) => (prev.includes(findingId) ? prev : [...prev, findingId]))
    try {
      const res = await api<{ finding: TrackedFinding; counts: FindingCounts }>(
        `/api/tasks/${taskId}/findings/${findingId}`,
        {
          method: 'PATCH',
          body: JSON.stringify({ action }),
        },
      )
      if (findingSeqRef.current.get(seqKey) !== nextFindingSeq) return
      // If a newer GET (e.g., SSE round_finished that auto-reopened a finding)
      // started after this PATCH was issued, do not overwrite it with our stale
      // response; trigger a silent refresh to converge to exact server state.
      if (loadSeqByTaskRef.current.get(taskId) !== patchLoadSeq) {
        if (selectedTaskIdRef.current === taskId) {
          void loadTask(taskId, { silent: true })
        }
        return
      }
      setDetail((prev) => {
        if (!prev || prev.task.id !== taskId) return prev
        const nextFindings = applyUpdatedFinding(prev.findings, res.finding)
        return {
          ...prev,
          findings: nextFindings,
          findingCounts: res.counts ?? computeFindingCounts(nextFindings),
        }
      })
    } catch (err) {
      if (findingSeqRef.current.get(seqKey) === nextFindingSeq) {
        setNotice({ tone: 'error', text: errorMessage(err, (t) => t.errors.updateFinding) })
        // Re-sync if this failed mutation invalidated an in-flight SSE refresh.
        if (selectedTaskIdRef.current === taskId) {
          void loadTask(taskId, { silent: true })
        }
      }
    } finally {
      if (findingSeqRef.current.get(seqKey) === nextFindingSeq) {
        setUpdatingFindingIds((prev) => prev.filter((id) => id !== findingId))
      }
    }
  }

  const copyFixPrompt = async (finding: TrackedFinding) => {
    const text = buildFixPrompt(finding)
    try {
      if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
        await navigator.clipboard.writeText(text)
      } else {
        throw new Error('')
      }
      setNotice({ tone: 'info', text: (t) => t.findings.copiedFixPrompt })
    } catch {
      setNotice({ tone: 'error', text: (t) => t.errors.copyFailed })
    }
  }

  const selectTask = (id: string) => {
    setSelectedTaskId(id)
    setNavOpen(false)
  }

  const trackedFindings = activeDetail?.findings ?? []
  const findingCounts = activeDetail?.findingCounts ?? computeFindingCounts(trackedFindings)
  const visibleTrackedFindings = useMemo(
    () => filterFindings(trackedFindings, findingFilter),
    [trackedFindings, findingFilter],
  )

  const tabCounts: Record<DetailTab, number> = {
    results: trackedFindings.length > 0 ? findingCounts.open : (latestResult?.finding_count ?? 0),
    timeline: activeDetail?.eventStats.important_count ?? 0,
    rounds: orderedRounds.length,
    artifacts: activeDetail?.artifacts.length ?? 0,
  }

  const roundActive = isRoundActive(selectedTask?.status)
  const isStarting = selectedTask !== null && startingTaskIds.includes(selectedTask.id)
  const startLabel = latestRound ? t.detail.runNextRound : t.detail.runFirstRound
  const runAgentCap = backends.find((b) => b.name === runDraft.agent)
  const runReasoningOptions = resolveReasoningOptions(runAgentCap, runDraft.model)
  const newAgentCap = backends.find((b) => b.name === newAgent)
  const newReasoningOptions = resolveReasoningOptions(newAgentCap, newModel)
  const roundLabels = { cliDefault: t.settings.agentDefault, modelDefault: t.settings.modelDefault }
  const agentOptions = useMemo(() => {
    const names = backends.map((b) => b.name)
    if (runDraft.agent && !names.includes(runDraft.agent)) names.unshift(runDraft.agent)
    return names
  }, [backends, runDraft.agent])

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
          <Settings
            t={t}
            onSaved={(next) => {
              configSeqRef.current += 1
              setSavedConfig(next)
            }}
            onBack={() => setShowSettings(false)}
          />
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

            {selectedTask && activeDetail && (
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

                  <div className="round-config-card" aria-label={t.detail.roundSettings}>
                    <div className="round-config-grid">
                      <label className="field field-compact">
                        <span className="field-label">{t.detail.roundAgent}</span>
                        <select
                          value={runDraft.agent}
                          disabled={roundActive || isStarting}
                          onChange={(e) => setRunDraft((prev) => switchDraftAgent(prev, e.target.value, backends, savedConfig))}
                        >
                          {agentOptions.map((name) => (
                            <option key={name} value={name}>{name}</option>
                          ))}
                        </select>
                      </label>
                      <label className="field field-compact">
                        <span className="field-label">{t.detail.roundModel}</span>
                        {runAgentCap && runAgentCap.models.length > 0 ? (
                          <select
                            value={runDraft.model}
                            disabled={roundActive || isStarting}
                            onChange={(e) => {
                              const nextModel = e.target.value
                              setRunDraft((prev) => selectDraftModel(prev, nextModel, runAgentCap))
                            }}
                          >
                            <option value="">{t.settings.agentDefault}</option>
                            {runDraft.model && !runAgentCap.models.includes(runDraft.model) && (
                              <option value={runDraft.model}>{runDraft.model}</option>
                            )}
                            {runAgentCap.models.map((m) => (
                              <option key={m} value={m}>{m}</option>
                            ))}
                          </select>
                        ) : (
                          <input
                            value={runDraft.model}
                            disabled={roundActive || isStarting}
                            placeholder={t.settings.agentDefault}
                            onChange={(e) => {
                              const nextModel = e.target.value
                              setRunDraft((prev) => selectDraftModel(prev, nextModel, runAgentCap))
                            }}
                          />
                        )}
                      </label>
                      {runReasoningOptions.supported && (
                        <label className="field field-compact">
                          <span className="field-label">{t.detail.roundReasoning}</span>
                          <select
                            value={runDraft.reasoning}
                            disabled={roundActive || isStarting}
                            onChange={(e) => setRunDraft((prev) => ({
                              ...prev,
                              reasoning: e.target.value,
                              explicit: { ...prev.explicit, reasoning: true },
                              userEdited: { ...prev.userEdited, reasoning: true },
                            }))}
                          >
                            <option value="">
                              {runReasoningOptions.defaultLevel
                                ? t.settings.modelDefaultWithLevel(runReasoningOptions.defaultLevel)
                                : t.settings.modelDefault}
                            </option>
                            {runDraft.reasoning && !runReasoningOptions.levels.includes(runDraft.reasoning) && (
                              <option value={runDraft.reasoning}>{runDraft.reasoning}</option>
                            )}
                            {runReasoningOptions.levels.map((lvl) => (
                              <option key={lvl} value={lvl}>{lvl}</option>
                            ))}
                          </select>
                        </label>
                      )}
                      <label className="field field-compact">
                        <span className="field-label">{t.detail.roundLanguage}</span>
                        <select
                          value={runDraft.language}
                          disabled={roundActive || isStarting}
                          onChange={(e) => setRunDraft((prev) => ({
                            ...prev,
                            language: (e.target.value === 'zh' ? 'zh' : 'en'),
                            explicit: { ...prev.explicit, language: true },
                            userEdited: { ...prev.userEdited, language: true },
                          }))}
                        >
                          <option value="en">{t.detail.reviewLangEn}</option>
                          <option value="zh">{t.detail.reviewLangZh}</option>
                        </select>
                      </label>
                    </div>
                    <label className="field field-compact round-note-field">
                      <span className="field-label">{t.detail.roundNote}</span>
                      <input
                        value={runDraft.note}
                        disabled={roundActive || isStarting}
                        placeholder={t.detail.roundNotePlaceholder}
                        onChange={(e) => setRunDraft((prev) => ({ ...prev, note: e.target.value }))}
                      />
                    </label>
                  </div>

                  <div className="meta-row">
                    <span className="meta-item">
                      <span className="meta-key">{t.detail.repository}</span>
                      <span className="meta-val mono" title={selectedTask.repo_path}>{selectedTask.repo_path}</span>
                    </span>
                    <span className="meta-item">
                      <span className="meta-key">{t.detail.agent}</span>
                      <span className="meta-val mono">
                        {latestRound
                          ? formatRoundSettingsLabel(latestRound, backends, roundLabels)
                          : formatRoundSettingsLabel(
                              {
                                backend: selectedTask.backend,
                                model: selectedTask.model,
                                reasoning: selectedTask.reasoning,
                              },
                              backends,
                              roundLabels,
                            )}
                      </span>
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
                    {trackedFindings.length > 0 ? (
                      <>
                        <div className="section-label">
                          <span>{t.findings.heading}</span>
                          <span className="count">{t.counts.findingsCompact(findingCounts.all)}</span>
                        </div>
                        <div className="finding-filters" role="group" aria-label={t.findings.heading}>
                          {(
                            [
                              'open',
                              'new',
                              'persistent',
                              'likely_fixed',
                              'unverified',
                              'resolved',
                              'dismissed',
                              'all',
                            ] as FindingFilter[]
                          )
                            .filter((key) => key === 'open' || key === 'all' || findingCounts[key] > 0 || findingFilter === key)
                            .map((key) => (
                              <button
                                key={key}
                                type="button"
                                className={`filter-pill ${findingFilter === key ? 'active' : ''}`}
                                aria-pressed={findingFilter === key}
                                onClick={() => setFindingFilter(key)}
                              >
                                <span>{t.findings.filter[key]}</span>
                                <span className="n">{findingCounts[key]}</span>
                              </button>
                            ))}
                        </div>
                        {visibleTrackedFindings.length > 0 ? (
                          <div className="findings">
                            {visibleTrackedFindings.map((finding) => {
                              const closed = isClosedFinding(finding.status)
                              const busy = updatingFindingIds.includes(finding.id)
                              const loc = findingLocationText(finding)
                              const prevLoc =
                                finding.location_changed && finding.previous_path
                                  ? findingLocationText({
                                      path: finding.previous_path,
                                      line: finding.previous_line,
                                    })
                                  : null
                              return (
                                <article
                                  className={`finding sev-${finding.severity} ${closed ? 'is-closed' : ''}`}
                                  key={finding.id}
                                >
                                  <div className="finding-top">
                                    <span className="sev-tag">{severityLabel(t, finding.severity)}</span>
                                    <span className={`finding-status st-${finding.status}`}>
                                      {t.findings.status[finding.status]}
                                    </span>
                                    <span className="finding-chip mono">
                                      {t.findings.roundSpan(finding.first_round, finding.last_round)}
                                    </span>
                                    {finding.category && (
                                      <span className="finding-chip mono">{finding.category}</span>
                                    )}
                                    {finding.reopened && (
                                      <span className="finding-chip is-warn">{t.findings.reopened}</span>
                                    )}
                                    {finding.location_changed && (
                                      <span className="finding-chip" title={prevLoc ? t.findings.previousLocation(prevLoc) : undefined}>
                                        {t.findings.locationChanged}
                                        {prevLoc ? ` · ${t.findings.previousLocation(prevLoc)}` : ''}
                                      </span>
                                    )}
                                    <span className="finding-loc">{loc ?? t.findings.noLocation}</span>
                                  </div>
                                  <div className="finding-title">{finding.title}</div>
                                  {finding.detail && finding.detail !== finding.title && (
                                    <p className="finding-body">{finding.detail}</p>
                                  )}
                                  {finding.status === 'likely_fixed' && (
                                    <p className="finding-note">{t.findings.likelyFixedHint}</p>
                                  )}
                                  {finding.status === 'dismissed' && finding.mentioned_after_dismiss && (
                                    <p className="finding-note is-warn">
                                      {t.findings.mentionedAfterDismiss(finding.last_round)}
                                    </p>
                                  )}
                                  <div className="finding-actions">
                                    {closed ? (
                                      <button
                                        type="button"
                                        className="btn btn-secondary btn-xs"
                                        disabled={busy}
                                        onClick={() => void updateFindingStatus(selectedTask.id, finding.id, 'reopen')}
                                      >
                                        {t.findings.undo}
                                      </button>
                                    ) : (
                                      <>
                                        <button
                                          type="button"
                                          className="btn btn-secondary btn-xs"
                                          disabled={busy}
                                          onClick={() => void updateFindingStatus(selectedTask.id, finding.id, 'resolve')}
                                        >
                                          {t.findings.markResolved}
                                        </button>
                                        <button
                                          type="button"
                                          className="btn btn-ghost btn-xs"
                                          disabled={busy}
                                          onClick={() => void updateFindingStatus(selectedTask.id, finding.id, 'dismiss')}
                                        >
                                          {t.findings.dismiss}
                                        </button>
                                        <button
                                          type="button"
                                          className="btn btn-ghost btn-xs"
                                          onClick={() => void copyFixPrompt(finding)}
                                        >
                                          {t.findings.copyFixPrompt}
                                        </button>
                                      </>
                                    )}
                                  </div>
                                </article>
                              )
                            })}
                          </div>
                        ) : (
                          <div className="empty-note">
                            <span>{t.findings.noneMatchingFilter}</span>{' '}
                            <button
                              type="button"
                              className="btn btn-ghost btn-xs"
                              onClick={() => setFindingFilter('all')}
                            >
                              {t.findings.showAll}
                            </button>
                          </div>
                        )}
                      </>
                    ) : latestResult && latestResult.findings.length > 0 ? (
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
                          ? t.timeline.allEvents(activeDetail.eventStats.all_count)
                          : t.timeline.importantEvents(activeDetail.eventStats.important_count)}
                      </span>
                      {activeDetail.eventStats.hidden_count > 0 && (
                        <button className="btn btn-ghost" onClick={() => setShowAllEvents((v) => !v)}>
                          {showAllEvents ? t.timeline.importantOnly : t.timeline.showAll(activeDetail.eventStats.all_count)}
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
                              <span className="pill mono round-settings-pill">
                                {formatRoundSettingsLabel(round, backends, roundLabels)}
                              </span>
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
                                <span className="kv-v mono">{round.settings?.agent || round.backend}</span>
                              </div>
                              <div className="kv">
                                <span className="kv-k">{t.rounds.model}</span>
                                <span className="kv-v mono">
                                  {(round.settings?.model ?? round.model) || t.settings.agentDefault}
                                </span>
                              </div>
                              <div className="kv">
                                <span className="kv-k">{t.rounds.reasoning}</span>
                                <span className="kv-v mono">
                                  {(round.settings?.reasoning ?? round.reasoning) || t.settings.modelDefault}
                                </span>
                              </div>
                              <div className="kv">
                                <span className="kv-k">{t.rounds.language}</span>
                                <span className="kv-v mono">{round.settings?.language || round.language || DEFAULT_REVIEW_LANGUAGE}</span>
                              </div>
                              {round.review_result?.parse_mode && (
                                <div className="kv">
                                  <span className="kv-k">{t.rounds.parseMode}</span>
                                  <span className="kv-v mono">{round.review_result.parse_mode}</span>
                                </div>
                              )}
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
                    {activeDetail.artifacts.length ? (
                      <div className="artifacts">
                        {activeDetail.artifacts.map((artifact) => (
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
                placeholder="/path/to/repo"
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
            <div className="modal-grid">
              {backends.length > 0 && (
                <label className="field">
                  <span className="field-label">{t.newReview.agent}</span>
                  <select
                    value={newAgent}
                    onChange={(event) => {
                      const nextAgent = event.target.value
                      if (!nextAgent) {
                        setNewAgent('')
                        setNewModel('')
                        setNewReasoning('')
                        return
                      }
                      const saved = savedForAgent(savedConfig, nextAgent)
                      const cap = backends.find((b) => b.name === nextAgent)
                      const opts = resolveReasoningOptions(cap, saved.model)
                      setNewAgent(nextAgent)
                      setNewModel(saved.model)
                      setNewReasoning(opts.supported ? saved.reasoning : '')
                    }}
                  >
                    <option value="">{t.newReview.agentAuto}</option>
                    {backends.map((b) => (
                      <option key={b.name} value={b.name}>{b.name}</option>
                    ))}
                  </select>
                </label>
              )}
              {newAgentCap && (
                <label className="field">
                  <span className="field-label">{t.newReview.model}</span>
                  {newAgentCap.models.length > 0 ? (
                    <select
                      value={newModel}
                      onChange={(event) => {
                        const nextModel = event.target.value
                        const opts = resolveReasoningOptions(newAgentCap, nextModel)
                        setNewModel(nextModel)
                        if (newReasoning && (!opts.supported || !opts.levels.includes(newReasoning))) {
                          setNewReasoning('')
                        }
                      }}
                    >
                      <option value="">{t.settings.agentDefault}</option>
                      {newModel && !newAgentCap.models.includes(newModel) && (
                        <option value={newModel}>{newModel}</option>
                      )}
                      {newAgentCap.models.map((m) => (
                        <option key={m} value={m}>{m}</option>
                      ))}
                    </select>
                  ) : (
                    <input
                      value={newModel}
                      placeholder={t.settings.agentDefault}
                      onChange={(event) => setNewModel(event.target.value)}
                    />
                  )}
                </label>
              )}
              {newReasoningOptions.supported && (
                <label className="field">
                  <span className="field-label">{t.newReview.reasoning}</span>
                  <select
                    value={newReasoning}
                    onChange={(event) => setNewReasoning(event.target.value)}
                  >
                    <option value="">
                      {newReasoningOptions.defaultLevel
                        ? t.settings.modelDefaultWithLevel(newReasoningOptions.defaultLevel)
                        : t.settings.modelDefault}
                    </option>
                    {newReasoningOptions.levels.map((lvl) => (
                      <option key={lvl} value={lvl}>{lvl}</option>
                    ))}
                  </select>
                </label>
              )}
              <label className="field">
                <span className="field-label">{t.newReview.language}</span>
                <select
                  value={newLanguage}
                  onChange={(event) => setNewLanguage(event.target.value === 'zh' ? 'zh' : 'en')}
                >
                  <option value="en">{t.detail.reviewLangEn}</option>
                  <option value="zh">{t.detail.reviewLangZh}</option>
                </select>
              </label>
            </div>
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
