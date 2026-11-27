import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  computeOverflowResyncDelayMs,
  mergeEventLists,
  mergeStreamEvent,
  parseStreamEventPayload,
  shouldRefreshTaskOnEvent,
} from '../eventStream'
import {
  applyUpdatedFinding,
  computeFindingCounts,
  filterFindings,
  type FindingAction,
  type FindingFilter,
  type TrackedFinding,
} from '../findings'
import {
  errorMessage,
  labelFor,
  messages,
  otherLanguage,
  readStoredLanguage,
  storeLanguage,
  type Language,
  type Localized,
  type ReviewLanguage,
} from '../i18n'
import {
  buildStartRoundPayload,
  createRunDraft,
  formatRoundSettingsLabel,
  mergeEffectiveIntoDraft,
  selectDraftModel,
  switchDraftAgent,
  type BackendCapability,
  type EffectiveSettingsResponse,
  type RunDraft,
  type SavedConfig,
} from '../runSettings'
import { StartTracker } from '../startTracker'
import { ActivityFeed } from './ActivityFeed'
import { api, formatElapsed, normalizeTaskDetail, repoBasename } from './api'
import { FindingCard } from './FindingCard'
import {
  BrandMarkIcon,
  ChevronDownIcon,
  CloseIcon,
  LockIcon,
  MenuIcon,
  PlusIcon,
  SlidersIcon,
  TrashIcon,
  WarningIcon,
} from './Icons'
import { NewReviewModal } from './NewReviewModal'
import { hasReadOnlySandbox, ProgressStepper, type PendingRoundSpec } from './ProgressStepper'
import { RunRoundPopover } from './RunRoundPopover'
import type { DiffResponse, EventRecord, ReviewRound, Task, TaskDetail, TaskDetailResponse } from './types'
import { WorkbenchSettings } from './WorkbenchSettings'
import { WorkbenchSidebar, type SidebarFilter } from './WorkbenchSidebar'
import { ArtifactsTab, ChangesTab, ChecksTab } from './WorkbenchTabs'

type TabKey = 'findings' | 'activity' | 'changes' | 'checks' | 'artifacts'
type Notice = { tone: 'error' | 'info'; text: Localized }

const SEVERITY_ORDER = ['critical', 'high', 'medium', 'low'] as const
const FINDING_FILTERS: FindingFilter[] = [
  'open',
  'all',
  'new',
  'persistent',
  'likely_fixed',
  'unverified',
  'resolved',
  'dismissed',
]

function isRoundActive(status?: string | null): boolean {
  return status === 'ingesting' || status === 'running'
}

async function copyText(text: string): Promise<boolean> {
  if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // Fallback below.
    }
  }
  try {
    const el = document.createElement('textarea')
    el.value = text
    el.style.position = 'fixed'
    el.style.opacity = '0'
    document.body.appendChild(el)
    el.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(el)
    return ok
  } catch {
    return false
  }
}

export function WorkbenchApp() {
  const [language, setLanguage] = useState<Language>(() => readStoredLanguage())
  const t = messages[language]
  const languageOffer = otherLanguage(language)

  const [tasks, setTasks] = useState<Task[]>([])
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null)
  const [detail, setDetail] = useState<TaskDetail | null>(null)
  const [repoFilter, setRepoFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState<SidebarFilter>('all')
  const [mobileNavOpen, setMobileNavOpen] = useState(false)
  const [showSettings, setShowSettings] = useState(false)
  const [showNewModal, setShowNewModal] = useState(false)
  const [showRunPopover, setShowRunPopover] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)

  const [backends, setBackends] = useState<BackendCapability[]>([])
  const [backendsLoaded, setBackendsLoaded] = useState(false)
  const [savedConfig, setSavedConfig] = useState<SavedConfig | null>(null)
  const [taskEffective, setTaskEffective] = useState<{ taskId: string; data: EffectiveSettingsResponse } | null>(null)
  const [runDraft, setRunDraft] = useState<RunDraft>(() => createRunDraft(null, undefined, null))
  const [focusOverride, setFocusOverride] = useState('')

  const [activeTab, setActiveTab] = useState<TabKey>('findings')
  const [findingFilter, setFindingFilter] = useState<FindingFilter>('open')
  const [severityFilter, setSeverityFilter] = useState('')
  const [updatingFindingIds, setUpdatingFindingIds] = useState<string[]>([])
  const [startingTaskIds, setStartingTaskIds] = useState<string[]>([])
  const [startingSpecByTask, setStartingSpecByTask] = useState<Record<string, PendingRoundSpec>>({})
  const [runningValidation, setRunningValidation] = useState(false)
  const [verifyingFindings, setVerifyingFindings] = useState(false)
  const [runningJury, setRunningJury] = useState(false)
  const [diffState, setDiffState] = useState<{ taskId: string; data: DiffResponse | null; loading: boolean }>({
    taskId: '',
    data: null,
    loading: false,
  })
  const [notice, setNotice] = useState<Notice | null>(null)

  const selectedTaskIdRef = useRef<string | null>(null)
  selectedTaskIdRef.current = selectedTaskId
  const listSeqRef = useRef(0)
  const startTrackerRef = useRef(new StartTracker())
  const eventSourceRef = useRef<EventSource | null>(null)
  const loadSeqByTaskRef = useRef<Map<string, number>>(new Map())
  const activeLoadSeqByTaskRef = useRef<Map<string, number>>(new Map())
  const streamEventsByTaskRef = useRef<Map<string, EventRecord[]>>(new Map())
  const streamOverflowCutoffByTaskRef = useRef<Map<string, number>>(new Map())
  const overflowResyncTimerByTaskRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map())
  const overflowRetryAttemptByTaskRef = useRef<Map<string, number>>(new Map())
  const findingSeqRef = useRef<Map<string, number>>(new Map())
  const effectiveSeqRef = useRef(0)
  const configSeqRef = useRef(0)
  const loadTaskRef = useRef<(taskId: string, options?: { silent?: boolean; isOverflowResync?: boolean }) => Promise<void>>(
    async () => {},
  )

  useEffect(() => {
    document.documentElement.lang = language
    document.documentElement.setAttribute('data-theme', 'light')
  }, [language])

  const chooseLanguage = (next: Language) => {
    setLanguage(next)
    storeLanguage(next)
  }

  const clearOverflowResync = useCallback((taskId: string, opts?: { clearCutoff?: boolean }) => {
    const timer = overflowResyncTimerByTaskRef.current.get(taskId)
    if (timer !== undefined) {
      clearTimeout(timer)
      overflowResyncTimerByTaskRef.current.delete(taskId)
    }
    overflowRetryAttemptByTaskRef.current.delete(taskId)
    if (opts?.clearCutoff) {
      streamOverflowCutoffByTaskRef.current.delete(taskId)
    }
  }, [])

  const scheduleOverflowResync = useCallback((taskId: string, opts?: { immediate?: boolean }) => {
    if (overflowResyncTimerByTaskRef.current.has(taskId)) return
    const attempt = overflowRetryAttemptByTaskRef.current.get(taskId) ?? 0
    const delayMs = computeOverflowResyncDelayMs(attempt, Boolean(opts?.immediate))
    overflowRetryAttemptByTaskRef.current.set(taskId, attempt + 1)
    const timer = setTimeout(() => {
      overflowResyncTimerByTaskRef.current.delete(taskId)
      if (selectedTaskIdRef.current !== taskId) return
      if ((streamOverflowCutoffByTaskRef.current.get(taskId) ?? 0) <= 0) return
      if (activeLoadSeqByTaskRef.current.has(taskId)) return
      void loadTaskRef.current(taskId, { silent: true, isOverflowResync: true })
    }, delayMs)
    overflowResyncTimerByTaskRef.current.set(taskId, timer)
  }, [])

  const refreshTasks = useCallback(async (options?: { silent?: boolean }) => {
    const seq = ++listSeqRef.current
    try {
      const list = await api<Task[]>('/api/tasks')
      if (listSeqRef.current !== seq) return
      setTasks(list)
      setSelectedTaskId((prev) => {
        if (prev && list.some((item) => item.id === prev)) return prev
        return list[0]?.id ?? null
      })
    } catch (err) {
      if (listSeqRef.current !== seq) return
      if (!options?.silent) {
        setNotice({ tone: 'error', text: errorMessage(err, (m) => m.errors.loadReviews) })
      }
    }
  }, [])

  const loadTask = useCallback(
    async (taskId: string, options?: { silent?: boolean; isOverflowResync?: boolean }) => {
      const nextSeq = (loadSeqByTaskRef.current.get(taskId) ?? 0) + 1
      loadSeqByTaskRef.current.set(taskId, nextSeq)
      activeLoadSeqByTaskRef.current.set(taskId, nextSeq)
      let needsOverflowRetry = false
      let immediateOverflowRetry = false
      try {
        const raw = await api<TaskDetailResponse>(`/api/tasks/${taskId}`)
        if (selectedTaskIdRef.current !== taskId || loadSeqByTaskRef.current.get(taskId) !== nextSeq) return
        const data = normalizeTaskDetail(raw)
        const rawMaxSeq = data.events.reduce((max, ev) => Math.max(max, ev.seq), 0)
        const buffered = streamEventsByTaskRef.current.get(taskId) ?? []
        const mergedBuf = buffered.length
          ? mergeEventLists(data.events, data.importantEvents, buffered)
          : { events: data.events, importantEvents: data.importantEvents, eventStats: data.eventStats }
        const mergedDetail: TaskDetail = {
          ...data,
          events: mergedBuf.events,
          importantEvents: mergedBuf.importantEvents,
          eventStats: mergedBuf.eventStats,
        }
        setDetail((prev) => {
          if (prev && prev.task.id === taskId && prev.events.length > 0) {
            const withPrev = mergeEventLists(mergedDetail.events, mergedDetail.importantEvents, prev.events)
            return {
              ...mergedDetail,
              events: withPrev.events,
              importantEvents: withPrev.importantEvents,
              eventStats: withPrev.eventStats,
            }
          }
          return mergedDetail
        })
        const cutoff = streamOverflowCutoffByTaskRef.current.get(taskId) ?? 0
        if (cutoff > 0) {
          if (rawMaxSeq >= cutoff) {
            clearOverflowResync(taskId, { clearCutoff: true })
          } else {
            needsOverflowRetry = true
            immediateOverflowRetry = !options?.isOverflowResync
          }
        }
      } catch (err) {
        if (selectedTaskIdRef.current === taskId && (streamOverflowCutoffByTaskRef.current.get(taskId) ?? 0) > 0) {
          needsOverflowRetry = true
        }
        if (selectedTaskIdRef.current !== taskId || loadSeqByTaskRef.current.get(taskId) !== nextSeq) return
        if (!options?.silent) {
          setNotice({ tone: 'error', text: errorMessage(err, (m) => m.errors.loadReview) })
        }
      } finally {
        if (activeLoadSeqByTaskRef.current.get(taskId) === nextSeq) {
          activeLoadSeqByTaskRef.current.delete(taskId)
          if (
            needsOverflowRetry &&
            selectedTaskIdRef.current === taskId &&
            (streamOverflowCutoffByTaskRef.current.get(taskId) ?? 0) > 0
          ) {
            scheduleOverflowResync(taskId, { immediate: immediateOverflowRetry })
          }
        }
      }
    },
    [clearOverflowResync, scheduleOverflowResync],
  )
  loadTaskRef.current = loadTask

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
      // Non-blocking.
    } finally {
      if (configSeqRef.current === seq) setBackendsLoaded(true)
    }
  }, [])

  useEffect(() => {
    void refreshTasks()
    void loadConfigMeta()
    const timer = window.setInterval(() => void refreshTasks({ silent: true }), 4000)
    return () => window.clearInterval(timer)
  }, [refreshTasks, loadConfigMeta])

  useEffect(() => {
    if (!showSettings) void loadConfigMeta()
  }, [showSettings, loadConfigMeta])

  const activeDetail = detail && detail.task.id === selectedTaskId ? detail : null
  const selectedTask = useMemo(
    () => tasks.find((item) => item.id === selectedTaskId) ?? activeDetail?.task ?? null,
    [tasks, selectedTaskId, activeDetail?.task],
  )
  const orderedRounds = useMemo(
    () => (activeDetail ? [...activeDetail.rounds].sort((a, b) => a.round_index - b.round_index) : []),
    [activeDetail],
  )
  const latestRound = orderedRounds.length ? orderedRounds[orderedRounds.length - 1] : undefined
  const latestResult = activeDetail?.latestReviewResult ?? selectedTask?.latest_review_result ?? null

  const recentRepos = useMemo(() => {
    const seen = new Set<string>()
    const out: string[] = []
    for (const item of tasks) {
      if (item.repo_path && !seen.has(item.repo_path)) {
        seen.add(item.repo_path)
        out.push(item.repo_path)
      }
    }
    return out
  }, [tasks])

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
      .catch(() => {})
  }, [selectedTask?.id, selectedTask?.repo_path, selectedTask?.backend, savedConfig])

  const activeTaskEffective =
    selectedTask && taskEffective?.taskId === selectedTask.id ? taskEffective.data : null

  useEffect(() => {
    setRunDraft(createRunDraft(selectedTask, latestRound, savedConfig, activeTaskEffective))
    setFocusOverride('')
    setConfirmDelete(false)
    setShowRunPopover(false)
  }, [selectedTask?.id, latestRound?.id])

  useEffect(() => {
    if (!selectedTask) return
    setRunDraft((prev) =>
      mergeEffectiveIntoDraft(prev, selectedTask, latestRound, savedConfig, activeTaskEffective, backends),
    )
  }, [activeTaskEffective, savedConfig, backends, selectedTask?.id, latestRound?.id])

  useEffect(() => {
    if (!selectedTaskId) return
    setActiveTab('findings')
    setFindingFilter('open')
    setSeverityFilter('')
    setConfirmDelete(false)
    void loadTask(selectedTaskId)
  }, [selectedTaskId, loadTask])

  useEffect(() => {
    if (!selectedTaskId) return
    eventSourceRef.current?.close()
    const source = new EventSource(`/api/tasks/${selectedTaskId}/stream`)
    eventSourceRef.current = source
    let hadError = false

    source.onopen = () => {
      if (hadError) {
        hadError = false
        void loadTask(selectedTaskId, { silent: true })
        void refreshTasks({ silent: true })
      }
    }

    source.onmessage = (msg) => {
      const parsed = parseStreamEventPayload(msg.data)
      if (!parsed) {
        void loadTask(selectedTaskId, { silent: true })
        void refreshTasks({ silent: true })
        return
      }
      const prevBuf = streamEventsByTaskRef.current.get(selectedTaskId) ?? []
      if (!prevBuf.some((ev) => ev.seq === parsed.seq || ev.id === parsed.id)) {
        const combined = [...prevBuf, parsed]
        if (combined.length > 500) {
          const evicted = combined.slice(0, combined.length - 500)
          const evictedMax = evicted.reduce((max, ev) => Math.max(max, ev.seq), 0)
          const prevCutoff = streamOverflowCutoffByTaskRef.current.get(selectedTaskId) ?? 0
          streamOverflowCutoffByTaskRef.current.set(selectedTaskId, Math.max(prevCutoff, evictedMax))
          streamEventsByTaskRef.current.set(selectedTaskId, combined.slice(-500))
        } else {
          streamEventsByTaskRef.current.set(selectedTaskId, combined)
        }
      }
      setDetail((prev) => {
        if (!prev || prev.task.id !== selectedTaskId) return prev
        const merged = mergeStreamEvent(prev.events, prev.importantEvents, parsed)
        if (!merged.changed) return prev
        return {
          ...prev,
          events: merged.events,
          importantEvents: merged.importantEvents,
          eventStats: merged.eventStats,
        }
      })
      if (shouldRefreshTaskOnEvent(parsed)) {
        void loadTask(selectedTaskId, { silent: true })
        void refreshTasks({ silent: true })
      } else if (
        (streamOverflowCutoffByTaskRef.current.get(selectedTaskId) ?? 0) > 0 &&
        !activeLoadSeqByTaskRef.current.has(selectedTaskId)
      ) {
        scheduleOverflowResync(selectedTaskId)
      }
    }

    source.onerror = () => {
      hadError = true
    }

    return () => {
      clearOverflowResync(selectedTaskId)
      source.close()
      if (eventSourceRef.current === source) eventSourceRef.current = null
    }
  }, [selectedTaskId, loadTask, refreshTasks, clearOverflowResync, scheduleOverflowResync])

  useEffect(() => {
    if (activeTab !== 'changes' || !selectedTaskId) return
    let cancelled = false
    setDiffState({ taskId: selectedTaskId, data: null, loading: true })
    api<DiffResponse>(`/api/tasks/${selectedTaskId}/diff`)
      .then((data) => {
        if (!cancelled) setDiffState({ taskId: selectedTaskId, data, loading: false })
      })
      .catch(() => {
        if (!cancelled) setDiffState({ taskId: selectedTaskId, data: null, loading: false })
      })
    return () => {
      cancelled = true
    }
  }, [activeTab, selectedTaskId, latestRound?.id])

  const startRound = async (
    taskId: string,
    opts?: { forceModelDefault?: boolean; customDraft?: RunDraft; customFocus?: string },
  ) => {
    if (startTrackerRef.current.isPending(taskId)) return
    setNotice(null)
    const req = startTrackerRef.current.begin(taskId)
    setStartingTaskIds(startTrackerRef.current.pendingTaskIds())
    const draftToUse = opts?.customDraft ?? runDraft
    const payload: Record<string, string | null> = buildStartRoundPayload(draftToUse)
    const pendingSpec: PendingRoundSpec = {
      backend: draftToUse.agent || selectedTask?.backend || '',
      model: draftToUse.model || null,
      reasoning: opts?.forceModelDefault ? null : draftToUse.reasoning || null,
    }
    setStartingSpecByTask((prev) => ({ ...prev, [taskId]: pendingSpec }))
    if (opts?.forceModelDefault) {
      payload.reasoning = null
      setRunDraft((prev) => ({
        ...prev,
        reasoning: '',
        explicit: { ...prev.explicit, reasoning: true },
        userEdited: { ...prev.userEdited, reasoning: true },
      }))
    }
    const focusText = (opts?.customFocus ?? focusOverride).trim()
    if (focusText) payload.review_focus = focusText
    try {
      await api(`/api/tasks/${taskId}/start`, {
        method: 'POST',
        body: JSON.stringify(payload),
      })
      setRunDraft((prev) => ({ ...prev, note: '' }))
      setFocusOverride('')
      await Promise.all([loadTask(taskId, { silent: true }), refreshTasks({ silent: true })])
    } catch (err) {
      const aborted = await startTrackerRef.current.wasAborted(req)
      if (!aborted) {
        setNotice({ tone: 'error', text: errorMessage(err, (m) => m.errors.startRound) })
      }
      await Promise.all([loadTask(taskId, { silent: true }), refreshTasks({ silent: true })])
    } finally {
      startTrackerRef.current.end(req)
      setStartingTaskIds(startTrackerRef.current.pendingTaskIds())
      setStartingSpecByTask((prev) => {
        if (!(taskId in prev)) return prev
        const next = { ...prev }
        delete next[taskId]
        return next
      })
    }
  }

  const abortTask = async (taskId: string) => {
    setNotice(null)
    let resolveAccepted!: (accepted: boolean) => void
    const acceptedPromise = new Promise<boolean>((resolve) => {
      resolveAccepted = resolve
    })
    startTrackerRef.current.noteAbort(taskId, acceptedPromise)
    try {
      await api(`/api/tasks/${taskId}/abort`, { method: 'POST' })
      resolveAccepted(true)
      await Promise.all([loadTask(taskId, { silent: true }), refreshTasks({ silent: true })])
    } catch (err) {
      resolveAccepted(false)
      setNotice({ tone: 'error', text: errorMessage(err, (m) => m.errors.cancelRound) })
    }
  }

  const handleUpdateFinding = async (findingId: string, action: FindingAction) => {
    if (!selectedTaskId) return
    const taskId = selectedTaskId
    const seq = (findingSeqRef.current.get(findingId) ?? 0) + 1
    findingSeqRef.current.set(findingId, seq)
    setUpdatingFindingIds((prev) => (prev.includes(findingId) ? prev : [...prev, findingId]))
    try {
      const res = await api<{ finding: TrackedFinding; counts: TaskDetail['findingCounts'] }>(
        `/api/tasks/${taskId}/findings/${findingId}`,
        { method: 'PATCH', body: JSON.stringify({ action }) },
      )
      if (findingSeqRef.current.get(findingId) !== seq) return
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
      setNotice({ tone: 'error', text: errorMessage(err, (m) => m.errors.updateFinding) })
    } finally {
      setUpdatingFindingIds((prev) => prev.filter((id) => id !== findingId))
    }
  }

  const handleOpenInEditor = async (path: string, line?: number | null) => {
    if (!selectedTaskId) return
    try {
      const res = await api<{ editor: string; path: string; line?: number | null }>(
        `/api/tasks/${selectedTaskId}/open-in-editor`,
        { method: 'POST', body: JSON.stringify({ path, line: line ?? null }) },
      )
      const loc = res.line ? `${res.path}:${res.line}` : res.path
      setNotice({ tone: 'info', text: (m) => m.workbench.openedInEditor(res.editor, loc) })
    } catch (err) {
      setNotice({ tone: 'error', text: errorMessage(err, (m) => m.workbench.openInEditorFailed) })
    }
  }

  const handleCopyPrompt = async (prompt: string) => {
    const ok = await copyText(prompt)
    setNotice(
      ok
        ? { tone: 'info', text: (m) => m.findings.copiedFixPrompt }
        : { tone: 'error', text: (m) => m.errors.copyFailed },
    )
  }

  const handleExportSummary = async () => {
    if (!selectedTaskId) return
    try {
      const res = await api<{ path: string }>(`/api/tasks/${selectedTaskId}/export-summary`, { method: 'POST' })
      setNotice({ tone: 'info', text: (m) => m.notices.exported(res.path) })
      void loadTask(selectedTaskId, { silent: true })
    } catch (err) {
      setNotice({ tone: 'error', text: errorMessage(err, (m) => m.errors.exportSummary) })
    }
  }

  const handleExportDiff = async () => {
    if (!selectedTaskId) return
    try {
      const res = await api<{ diff_path: string }>(`/api/tasks/${selectedTaskId}/export-diff`, { method: 'POST' })
      setNotice({ tone: 'info', text: (m) => m.notices.exported(res.diff_path) })
      void loadTask(selectedTaskId, { silent: true })
    } catch (err) {
      setNotice({ tone: 'error', text: errorMessage(err, (m) => m.errors.exportSummary) })
    }
  }

  const handleDeleteReview = async () => {
    if (!selectedTaskId) return
    if (!confirmDelete) {
      setConfirmDelete(true)
      return
    }
    const targetId = selectedTaskId
    try {
      await api(`/api/tasks/${targetId}`, { method: 'DELETE' })
      listSeqRef.current += 1
      setConfirmDelete(false)
      setTasks((prev) => prev.filter((item) => item.id !== targetId))
      setDetail((prev) => (prev?.task.id === targetId ? null : prev))
      if (selectedTaskIdRef.current === targetId) {
        setSelectedTaskId(null)
      }
      setNotice({ tone: 'info', text: (m) => m.workbench.deletedReview })
      await refreshTasks({ silent: true })
    } catch (err) {
      setConfirmDelete(false)
      setNotice({ tone: 'error', text: errorMessage(err, (m) => m.workbench.deleteFailed) })
    }
  }

  const handleVerifyFindings = async () => {
    if (!selectedTaskId || verifyingFindings) return
    setVerifyingFindings(true)
    try {
      const res = await api<{ verifier_backend: string }>(`/api/tasks/${selectedTaskId}/verify`, {
        method: 'POST',
        body: JSON.stringify({
          language: language === 'zh-CN' ? 'zh-CN' : 'en',
        }),
      })
      setNotice({ tone: 'info', text: (m) => m.findings.verificationStarted(res.verifier_backend) })
      void loadTask(selectedTaskId, { silent: true })
      void refreshTasks({ silent: true })
    } catch (err) {
      setNotice({ tone: 'error', text: errorMessage(err, (m) => m.findings.verificationFailed) })
    } finally {
      setVerifyingFindings(false)
    }
  }

  const handleRunBlindJury = async () => {
    if (!selectedTaskId || runningJury) return
    setRunningJury(true)
    try {
      const res = await api<{ jurors: string[] }>(`/api/tasks/${selectedTaskId}/jury`, {
        method: 'POST',
        body: JSON.stringify({
          language: language === 'zh-CN' ? 'zh-CN' : 'en',
        }),
      })
      setNotice({ tone: 'info', text: (m) => m.findings.juryStarted((res.jurors || []).join(' + ')) })
      void loadTask(selectedTaskId, { silent: true })
      void refreshTasks({ silent: true })
    } catch (err) {
      setNotice({ tone: 'error', text: errorMessage(err, (m) => m.findings.juryFailed) })
    } finally {
      setRunningJury(false)
    }
  }

  const handleRunChecks = async (mode: 'standard' | 'full') => {
    if (!selectedTaskId || runningValidation) return
    setRunningValidation(true)
    try {
      await api(`/api/tasks/${selectedTaskId}/validate`, {
        method: 'POST',
        body: JSON.stringify({ mode }),
      })
      setNotice({ tone: 'info', text: (m) => m.workbench.checksStarted })
      void loadTask(selectedTaskId, { silent: true })
    } catch (err) {
      setNotice({ tone: 'error', text: errorMessage(err, (m) => m.workbench.checksFailed) })
    } finally {
      setRunningValidation(false)
    }
  }

  const isVerifyingSelected = Boolean(
    selectedTask &&
      selectedTask.status === 'validating' &&
      (selectedTask.current_stage === 'verifying_findings' ||
        selectedTask.current_stage === 'blind_jury_running'),
  )
  const isStartingSelected = Boolean(selectedTask && startingTaskIds.includes(selectedTask.id))
  const isSelectedActive = Boolean(
    selectedTask && (isRoundActive(selectedTask.status) || isVerifyingSelected || isStartingSelected),
  )
  const nextRoundNumber = orderedRounds.length + 1
  const activeRoundNumber =
    latestRound && isRoundActive(selectedTask?.status) ? latestRound.round_index : nextRoundNumber

  const trackedFindings = activeDetail?.findings ?? []
  const findingCounts = activeDetail?.findingCounts ?? computeFindingCounts(trackedFindings)
  const visibleFindings = useMemo(() => {
    const byStatus = filterFindings(trackedFindings, findingFilter)
    if (!severityFilter) return byStatus
    return byStatus.filter((f) => f.severity.toLowerCase() === severityFilter)
  }, [trackedFindings, findingFilter, severityFilter])

  const latestErrorEvent = useMemo(() => {
    if (!activeDetail) return null
    for (let i = activeDetail.events.length - 1; i >= 0; i -= 1) {
      const ev = activeDetail.events[i]
      if (ev.level === 'error' || ev.kind === 'error') return ev
    }
    return null
  }, [activeDetail])

  const headerBackend =
    latestRound?.settings?.agent || latestRound?.backend || selectedTask?.backend || ''
  const headerHasSandbox = hasReadOnlySandbox(headerBackend)

  const headerSettingsLabel = useMemo(() => {
    if (!selectedTask) return ''
    const roundRef: ReviewRound = latestRound ?? {
      id: 'task',
      backend: selectedTask.backend,
      model: selectedTask.model,
      reasoning: selectedTask.reasoning,
      language: selectedTask.language,
      round_index: 0,
      status: selectedTask.status,
      started_at: selectedTask.updated_at,
    }
    return formatRoundSettingsLabel(roundRef, backends, {
      cliDefault: t.settings.agentDefault,
      modelDefault: t.settings.modelDefault,
    })
  }, [selectedTask, latestRound, backends, t])

  return (
    <div className="wb-root">
      <header className="wb-topbar">
        <div className="wb-topbar-left">
          {!showSettings && (
            <button
              type="button"
              className="wb-icon-btn wb-mobile-only"
              aria-label={t.topbar.toggleReviewList}
              onClick={() => setMobileNavOpen((v) => !v)}
            >
              <MenuIcon />
            </button>
          )}
          <button
            type="button"
            className="wb-brand"
            onClick={() => setShowSettings(false)}
          >
            <BrandMarkIcon />
            <span className="wb-brand-name">Mission Control</span>
          </button>

          {!showSettings && recentRepos.length > 0 && (
            <select
              aria-label={t.detail.repository}
              className="wb-select wb-select-xs wb-repo-switcher"
              value={repoFilter}
              onChange={(e) => setRepoFilter(e.target.value)}
            >
              <option value="">{t.workbench.allRepos}</option>
              {recentRepos.map((repo) => (
                <option key={repo} value={repo}>
                  {repoBasename(repo)}
                </option>
              ))}
            </select>
          )}
        </div>

        <div className="wb-topbar-right">
          <button
            type="button"
            className="wb-btn wb-btn-ghost wb-btn-sm"
            title={messages[languageOffer.code].topbar.switchToThisLanguage}
            onClick={() => chooseLanguage(languageOffer.code)}
          >
            {languageOffer.label}
          </button>
          <button
            type="button"
            className="wb-btn wb-btn-secondary wb-btn-sm"
            onClick={() => setShowSettings((v) => !v)}
          >
            <SlidersIcon size={14} />
            <span className="wb-hide-xs">{t.topbar.modelSelection}</span>
          </button>
          <button
            type="button"
            className="wb-btn wb-btn-primary wb-btn-sm"
            onClick={() => {
              setShowSettings(false)
              setShowNewModal(true)
            }}
          >
            <PlusIcon size={13} />
            <span>{t.topbar.newReview}</span>
          </button>
        </div>
      </header>

      {showSettings ? (
        <WorkbenchSettings
          repoPaths={recentRepos}
          t={t}
          onBack={() => setShowSettings(false)}
          onSaved={(cfg) => setSavedConfig(cfg)}
        />
      ) : (
        <div className="wb-workspace">
          <WorkbenchSidebar
            tasks={tasks}
            selectedTaskId={selectedTaskId}
            repoFilter={repoFilter}
            statusFilter={statusFilter}
            backends={backends}
            savedConfig={savedConfig}
            mobileOpen={mobileNavOpen}
            t={t}
            onSelectTask={(id) => setSelectedTaskId(id)}
            onChangeFilter={setStatusFilter}
            onOpenSettings={() => {
              setMobileNavOpen(false)
              setShowSettings(true)
            }}
            onCloseMobile={() => setMobileNavOpen(false)}
          />

          <main className="wb-main">
            {notice && (
              <div
                role={notice.tone === 'error' ? 'alert' : 'status'}
                className={`wb-alert wb-main-notice ${notice.tone === 'error' ? 'wb-alert-error' : 'wb-alert-info'}`}
              >
                <span>{notice.text(t)}</span>
                <button
                  type="button"
                  className="wb-icon-btn"
                  aria-label={t.common.dismiss}
                  onClick={() => setNotice(null)}
                >
                  <CloseIcon size={13} />
                </button>
              </div>
            )}

            {!selectedTask ? (
              <div className="wb-empty-state">
                {backendsLoaded && backends.length === 0 ? (
                  <div className="wb-card wb-empty-box">
                    <WarningIcon size={20} className="wb-warn-text" />
                    <h2>{t.workbench.noAgentsTitle}</h2>
                    <p className="wb-t2">{t.workbench.noAgentsBody}</p>
                    <button
                      type="button"
                      className="wb-btn wb-btn-primary"
                      onClick={() => setShowSettings(true)}
                    >
                      {t.workbench.openModelSelection}
                    </button>
                  </div>
                ) : (
                  <div className="wb-card wb-empty-box">
                    <h2>{tasks.length === 0 ? 'Mission Control' : t.empty.title}</h2>
                    <p className="wb-t2">
                      {tasks.length === 0 ? t.workbench.welcomeTagline : t.empty.body}
                    </p>
                    <div className="wb-empty-actions">
                      <button
                        type="button"
                        className="wb-btn wb-btn-primary"
                        onClick={() => setShowNewModal(true)}
                      >
                        <PlusIcon size={13} />
                        <span>{t.topbar.newReview}</span>
                      </button>
                      <button
                        type="button"
                        className="wb-btn wb-btn-secondary"
                        onClick={() => setShowSettings(true)}
                      >
                        {t.workbench.openModelSelection}
                      </button>
                    </div>
                  </div>
                )}
              </div>
            ) : (
              <div className="wb-detail-wrap">
                <div className="wb-detail-header">
                  <div className="wb-detail-title-col">
                    <div className="wb-detail-title-row">
                      <h1>{selectedTask.title}</h1>
                      <span className="wb-pill wb-pill-neutral">
                        {selectedTask.source_type === 'pull_request' && selectedTask.pr_number
                          ? `PR #${selectedTask.pr_number}`
                          : t.source.localChanges(selectedTask.pr_base_ref || null)}
                      </span>
                    </div>
                    <div className="wb-detail-meta">
                      <span className="wb-meta-chip wb-mono">{headerSettingsLabel}</span>
                      {headerHasSandbox && (
                        <span className="wb-meta-chip">
                          <LockIcon size={12} />
                          <span>{t.workbench.sandboxReadOnly}</span>
                        </span>
                      )}
                      <span className="wb-meta-chip">
                        {(selectedTask.language || 'en') === 'zh' ? t.detail.reviewLangZh : t.detail.reviewLangEn}
                      </span>
                      <span className="wb-xs wb-t3 wb-mono wb-truncate" title={selectedTask.repo_path}>
                        {selectedTask.repo_path}
                      </span>
                    </div>
                  </div>

                  <div className="wb-detail-actions">
                    {isSelectedActive ? (
                      <button
                        type="button"
                        className="wb-btn wb-btn-danger"
                        onClick={() => void abortTask(selectedTask.id)}
                      >
                        {t.detail.cancelRound}
                      </button>
                    ) : (
                      <div className="wb-split-wrap">
                        <div className="wb-split-btn">
                          <button
                            type="button"
                            className="wb-btn wb-btn-primary wb-split-main"
                            onClick={() => void startRound(selectedTask.id)}
                          >
                            {orderedRounds.length === 0
                              ? t.detail.runFirstRound
                              : t.workbench.runRoundN(nextRoundNumber)}
                          </button>
                          <button
                            type="button"
                            className="wb-btn wb-btn-primary wb-split-caret"
                            aria-label={t.workbench.runRoundTooltip}
                            aria-expanded={showRunPopover}
                            title={t.workbench.runRoundTooltip}
                            onClick={() => setShowRunPopover((v) => !v)}
                          >
                            <ChevronDownIcon size={13} />
                          </button>
                        </div>
                        <RunRoundPopover
                          open={showRunPopover}
                          nextRoundNumber={nextRoundNumber}
                          backends={backends}
                          draft={runDraft}
                          note={runDraft.note}
                          focusOverride={focusOverride}
                          language={runDraft.language}
                          t={t}
                          onClose={() => setShowRunPopover(false)}
                          onSelectAgent={(nextAgent) =>
                            setRunDraft((prev) => switchDraftAgent(prev, nextAgent, backends, savedConfig))
                          }
                          onSelectModel={(nextModel) => {
                            const cap = backends.find((b) => b.name === runDraft.agent)
                            setRunDraft((prev) => selectDraftModel(prev, nextModel, cap))
                          }}
                          onSelectReasoning={(nextReasoning) =>
                            setRunDraft((prev) => ({
                              ...prev,
                              reasoning: nextReasoning,
                              explicit: { ...prev.explicit, reasoning: true },
                              userEdited: { ...prev.userEdited, reasoning: true },
                            }))
                          }
                          onChangeLanguage={(nextLang: ReviewLanguage) =>
                            setRunDraft((prev) => ({
                              ...prev,
                              language: nextLang,
                              explicit: { ...prev.explicit, language: true },
                              userEdited: { ...prev.userEdited, language: true },
                            }))
                          }
                          onChangeNote={(note) => setRunDraft((prev) => ({ ...prev, note }))}
                          onChangeFocus={setFocusOverride}
                          onRunNow={() => void startRound(selectedTask.id)}
                        />
                      </div>
                    )}

                    {!isSelectedActive && backends.length >= 2 && (
                      <button
                        type="button"
                        className="wb-btn wb-btn-secondary wb-btn-sm"
                        disabled={runningJury}
                        onClick={() => void handleRunBlindJury()}
                      >
                        {runningJury ? t.findings.runningJury : t.findings.blindJury}
                      </button>
                    )}

                    {!isSelectedActive && findingCounts.open > 0 && (
                      <button
                        type="button"
                        className="wb-btn wb-btn-secondary wb-btn-sm"
                        disabled={verifyingFindings}
                        onClick={() => void handleVerifyFindings()}
                      >
                        {verifyingFindings ? t.findings.verifying : t.findings.verifyFindings}
                      </button>
                    )}

                    <button
                      type="button"
                      className="wb-btn wb-btn-secondary wb-btn-sm"
                      onClick={() => void handleExportSummary()}
                    >
                      {t.workbench.export}
                    </button>
                    {selectedTask.worktree_path && (
                      <button
                        type="button"
                        className="wb-btn wb-btn-secondary wb-btn-sm"
                        onClick={() => void handleExportDiff()}
                      >
                        {t.workbench.exportDiff}
                      </button>
                    )}
                    <button
                      type="button"
                      className={`wb-btn wb-btn-sm ${confirmDelete ? 'wb-btn-danger' : 'wb-btn-ghost'}`}
                      disabled={isSelectedActive}
                      onClick={() => void handleDeleteReview()}
                      onBlur={() => setConfirmDelete(false)}
                    >
                      <TrashIcon size={13} />
                      <span>{confirmDelete ? t.workbench.confirmDelete : t.workbench.deleteReview}</span>
                    </button>
                  </div>
                </div>

                {isSelectedActive && (
                  <ProgressStepper
                    task={selectedTask}
                    activeRound={latestRound ?? null}
                    pendingSpec={startingSpecByTask[selectedTask.id] ?? null}
                    roundNumber={activeRoundNumber}
                    t={t}
                  />
                )}

                {orderedRounds.length > 0 && (
                  <div className="wb-rounds-strip" aria-label={t.detail.rounds}>
                    {orderedRounds.map((round) => {
                      const v = round.review_result?.verdict
                      const tone =
                        round.status === 'running'
                          ? 'accent'
                          : round.status === 'aborted'
                            ? 'neutral'
                            : v === 'clear'
                              ? 'ok'
                              : v === 'concerns'
                                ? 'warn'
                                : v === 'failed' || round.status === 'failed'
                                  ? 'error'
                                  : 'neutral'
                      const label =
                        round.status === 'aborted'
                          ? t.status.aborted
                          : v
                            ? labelFor(t.verdict, v)
                            : labelFor(t.status, round.status)
                      const dur = round.ended_at ? formatElapsed(round.ended_at - round.started_at) : null
                      return (
                        <div key={round.id} className={`wb-round-card ${round.id === latestRound?.id ? 'latest' : ''}`}>
                          <div className="wb-round-card-top">
                            <span className="wb-b wb-sm">{t.rounds.title(round.round_index)}</span>
                            <span className={`wb-status-pill wb-tone-${tone}`}>{label}</span>
                          </div>
                          <div className="wb-xs wb-t2 wb-mono wb-truncate">
                            {formatRoundSettingsLabel(round, backends, {
                              cliDefault: t.settings.agentDefault,
                              modelDefault: t.settings.modelDefault,
                            })}
                          </div>
                          <div className="wb-round-card-foot wb-xs wb-t3">
                            <span>{t.counts.findings(round.review_result?.finding_count ?? 0)}</span>
                            {dur && <span>{dur}</span>}
                          </div>
                        </div>
                      )
                    })}
                  </div>
                )}

                {selectedTask.status === 'failed' ? (
                  <div className="wb-card wb-verdict-card wb-verdict-failed">
                    <div className="wb-verdict-head">
                      <div>
                        <span className="wb-status-pill wb-tone-error">{t.verdict.failed}</span>
                        <span className="wb-sm wb-t2 wb-ml-sm">
                          {t.workbench.failedAtStage(labelFor(t.stage, selectedTask.current_stage))}
                        </span>
                      </div>
                      <div className="wb-verdict-actions">
                        <button
                          type="button"
                          className="wb-btn wb-btn-secondary wb-btn-sm"
                          disabled={isSelectedActive}
                          onClick={() => void startRound(selectedTask.id)}
                        >
                          {t.workbench.retryRound}
                        </button>
                        <button
                          type="button"
                          className="wb-btn wb-btn-primary wb-btn-sm"
                          disabled={isSelectedActive}
                          onClick={() => void startRound(selectedTask.id, { forceModelDefault: true })}
                        >
                          {t.workbench.retryModelDefault}
                        </button>
                      </div>
                    </div>
                    <p className="wb-verdict-summary">
                      {String(
                        latestErrorEvent?.payload?.message ||
                          latestErrorEvent?.payload?.error ||
                          latestResult?.summary ||
                          t.result.noSummary,
                      )}
                    </p>
                  </div>
                ) : selectedTask.status === 'aborted' ? (
                  <div className="wb-card wb-verdict-card wb-verdict-neutral">
                    <div className="wb-verdict-head">
                      <span className="wb-status-pill wb-tone-neutral">{t.status.aborted}</span>
                      <button
                        type="button"
                        className="wb-btn wb-btn-secondary wb-btn-sm"
                        onClick={() => void startRound(selectedTask.id)}
                      >
                        {t.workbench.retryRound}
                      </button>
                    </div>
                  </div>
                ) : latestResult ? (
                  <div className={`wb-card wb-verdict-card wb-verdict-${latestResult.verdict}`}>
                    <div className="wb-verdict-head">
                      <div className="wb-verdict-title-row">
                        <span
                          className={`wb-status-pill wb-tone-${
                            latestResult.verdict === 'clear'
                              ? 'ok'
                              : latestResult.verdict === 'concerns'
                                ? 'warn'
                                : latestResult.verdict === 'failed'
                                  ? 'error'
                                  : 'neutral'
                          }`}
                        >
                          {labelFor(t.verdict, latestResult.verdict)}
                        </span>
                        <span className="wb-sm wb-t2">
                          {t.workbench.openCount(findingCounts.open)}
                          {findingCounts.likely_fixed > 0
                            ? ` · ${t.workbench.likelyFixedCount(findingCounts.likely_fixed)}`
                            : ''}
                        </span>
                      </div>
                      <div className="wb-sev-summary">
                        {SEVERITY_ORDER.map((sev) => {
                          const count = latestResult.severity_counts?.[sev] ?? 0
                          if (!count) return null
                          return (
                            <span key={sev} className={`wb-sev-pill wb-sev-${sev}`}>
                              {labelFor(t.severity, sev)} {count}
                            </span>
                          )
                        })}
                      </div>
                    </div>
                    {latestResult.summary && <p className="wb-verdict-summary">{latestResult.summary}</p>}
                  </div>
                ) : !isSelectedActive ? (
                  <div className="wb-card wb-verdict-card wb-verdict-neutral">
                    <div className="wb-verdict-head">
                      <div>
                        <span className="wb-status-pill wb-tone-neutral">
                          {labelFor(t.stage, selectedTask.current_stage)}
                        </span>
                        <span className="wb-sm wb-t2 wb-ml-sm">
                          {t.workbench.filesChanged(selectedTask.review_paths.length)}
                        </span>
                      </div>
                    </div>
                    <p className="wb-verdict-summary">{t.findings.noResultYet}</p>
                  </div>
                ) : null}

                <div className="wb-tabs-bar" role="tablist">
                  <button
                    type="button"
                    role="tab"
                    aria-selected={activeTab === 'findings'}
                    className={`wb-tab ${activeTab === 'findings' ? 'active' : ''}`}
                    onClick={() => setActiveTab('findings')}
                  >
                    <span>{t.workbench.tabFindings}</span>
                    <span className="wb-tab-badge">{findingCounts.open}</span>
                  </button>
                  <button
                    type="button"
                    role="tab"
                    aria-selected={activeTab === 'activity'}
                    className={`wb-tab ${activeTab === 'activity' ? 'active' : ''}`}
                    onClick={() => setActiveTab('activity')}
                  >
                    <span>{t.workbench.tabActivity}</span>
                    <span className="wb-tab-badge">{activeDetail?.eventStats.important_count ?? 0}</span>
                  </button>
                  <button
                    type="button"
                    role="tab"
                    aria-selected={activeTab === 'changes'}
                    className={`wb-tab ${activeTab === 'changes' ? 'active' : ''}`}
                    onClick={() => setActiveTab('changes')}
                  >
                    <span>{t.workbench.tabChanges}</span>
                    <span className="wb-tab-badge">{selectedTask.review_paths.length}</span>
                  </button>
                  <button
                    type="button"
                    role="tab"
                    aria-selected={activeTab === 'checks'}
                    className={`wb-tab ${activeTab === 'checks' ? 'active' : ''}`}
                    onClick={() => setActiveTab('checks')}
                  >
                    <span>{t.workbench.tabChecks}</span>
                    <span className="wb-tab-badge">{activeDetail?.checks.length ?? 0}</span>
                  </button>
                  <button
                    type="button"
                    role="tab"
                    aria-selected={activeTab === 'artifacts'}
                    className={`wb-tab ${activeTab === 'artifacts' ? 'active' : ''}`}
                    onClick={() => setActiveTab('artifacts')}
                  >
                    <span>{t.workbench.tabArtifacts}</span>
                    <span className="wb-tab-badge">{activeDetail?.artifacts.length ?? 0}</span>
                  </button>
                </div>

                <div className="wb-tab-panel">
                  {activeTab === 'findings' && (
                    <div className="wb-findings-wrap">
                      <div className="wb-findings-toolbar">
                        <div className="wb-filter-pills">
                          {FINDING_FILTERS.map((key) => {
                            const count = findingCounts[key]
                            if (key !== 'open' && key !== 'all' && count === 0 && findingFilter !== key) return null
                            return (
                              <button
                                key={key}
                                type="button"
                                className={`wb-fpill ${findingFilter === key ? 'active' : ''}`}
                                onClick={() => setFindingFilter(key)}
                              >
                                {t.findings.filter[key]}
                                <span className="wb-fpill-count">{count}</span>
                              </button>
                            )
                          })}
                        </div>
                        <select
                          aria-label={t.workbench.allSeverities}
                          className="wb-select wb-select-xs"
                          value={severityFilter}
                          onChange={(e) => setSeverityFilter(e.target.value)}
                        >
                          <option value="">{t.workbench.allSeverities}</option>
                          {SEVERITY_ORDER.map((sev) => (
                            <option key={sev} value={sev}>
                              {labelFor(t.severity, sev)}
                            </option>
                          ))}
                        </select>
                      </div>

                      {findingCounts.likely_fixed > 0 && (
                        <p className="wb-xs wb-t2 wb-likely-legend">{t.workbench.likelyFixedLegend}</p>
                      )}

                      {visibleFindings.length === 0 ? (
                        <div className="wb-empty-panel">
                          {trackedFindings.length === 0
                            ? latestResult
                              ? t.findings.noneThisRound
                              : t.findings.noResultYet
                            : t.findings.noneMatchingFilter}
                        </div>
                      ) : (
                        <div className="wb-findings-list">
                          {visibleFindings.map((finding) => (
                            <FindingCard
                              key={finding.id}
                              taskId={selectedTask.id}
                              finding={finding}
                              updating={updatingFindingIds.includes(finding.id)}
                              t={t}
                              onUpdateStatus={(fid, action) => void handleUpdateFinding(fid, action)}
                              onCopyPrompt={(prompt) => void handleCopyPrompt(prompt)}
                              onOpenInEditor={(path, line) => void handleOpenInEditor(path, line)}
                            />
                          ))}
                        </div>
                      )}
                    </div>
                  )}

                  {activeTab === 'activity' && (
                    <ActivityFeed
                      events={activeDetail?.events ?? []}
                      importantEvents={activeDetail?.importantEvents ?? []}
                      eventStats={
                        activeDetail?.eventStats ?? { all_count: 0, important_count: 0, hidden_count: 0 }
                      }
                      isRunning={isSelectedActive}
                      t={t}
                    />
                  )}

                  {activeTab === 'changes' && (
                    <ChangesTab
                      diffData={diffState.taskId === selectedTask.id ? diffState.data : null}
                      loading={diffState.taskId === selectedTask.id && diffState.loading}
                      reviewPaths={selectedTask.review_paths}
                      t={t}
                      onOpenInEditor={(path, line) => void handleOpenInEditor(path, line)}
                    />
                  )}

                  {activeTab === 'checks' && (
                    <ChecksTab
                      checks={activeDetail?.checks ?? []}
                      runningValidation={runningValidation || selectedTask.status === 'validating'}
                      disabled={isSelectedActive}
                      t={t}
                      onRunChecks={(mode) => void handleRunChecks(mode)}
                    />
                  )}

                  {activeTab === 'artifacts' && (
                    <ArtifactsTab artifacts={activeDetail?.artifacts ?? []} t={t} />
                  )}
                </div>
              </div>
            )}
          </main>
        </div>
      )}

      <NewReviewModal
        open={showNewModal}
        backends={backends}
        savedConfig={savedConfig}
        recentRepos={recentRepos}
        defaultRepo={repoFilter || recentRepos[0] || ''}
        t={t}
        onClose={() => setShowNewModal(false)}
        onNotice={setNotice}
        onCreated={(task, startImmediately) => {
          listSeqRef.current += 1
          setTasks((prev) => [task, ...prev.filter((item) => item.id !== task.id)])
          setSelectedTaskId(task.id)
          if (startImmediately) {
            void startRound(task.id, {
              customDraft: createRunDraft(task, undefined, savedConfig, null),
              customFocus: '',
            })
          }
        }}
      />
    </div>
  )
}
