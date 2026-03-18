import { useEffect, useMemo, useRef, useState } from 'react'

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
  status: TaskStatus
  current_stage: string
  source_url?: string | null
  review_focus?: string
  pr_number?: number | null
  pr_owner?: string | null
  pr_repo?: string | null
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

const EVENT_LIMIT = 40
const SEVERITY_ORDER = ['critical', 'high', 'medium', 'low']

const api = async <T,>(path: string, init?: RequestInit): Promise<T> => {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!response.ok) {
    const text = await response.text()
    throw new Error(text || `Request failed: ${response.status}`)
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

const timeText = (ts?: number) => {
  if (!ts) return '-'
  return new Date(ts * 1000).toLocaleString()
}

const relativeTimeText = (ts?: number) => {
  if (!ts) return '-'
  const diffSec = Math.max(0, Math.floor(Date.now() / 1000 - ts))
  if (diffSec < 60) return `${diffSec}s ago`
  const diffMin = Math.floor(diffSec / 60)
  if (diffMin < 60) return `${diffMin}m ago`
  const diffHour = Math.floor(diffMin / 60)
  if (diffHour < 24) return `${diffHour}h ago`
  return `${Math.floor(diffHour / 24)}d ago`
}

const stringifyPayload = (payload: Record<string, unknown>) => {
  if (typeof payload.text === 'string') return payload.text
  if (typeof payload.message === 'string') return payload.message
  if (typeof payload.summary === 'string') return payload.summary
  return JSON.stringify(payload, null, 2)
}

const previewText = (value: string, limit = 180) => {
  if (value.length <= limit) return value
  return `${value.slice(0, limit)}…`
}

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
      return 'warning'
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
      return 'created'
    case 'context_ready':
      return 'review ready'
    case 'running':
      return 'reviewing'
    case 'waiting_human':
      return 'awaiting next round'
    case 'failed':
      return 'failed'
    case 'aborted':
      return 'aborted'
    case 'ingesting':
      return 'preparing'
    case 'completed':
      return 'completed'
    case 'validating':
      return 'checks'
    default:
      return status
  }
}

const stageLabel = (stage: string) => {
  switch (stage) {
    case 'review_ready':
      return 'review ready'
    case 'prepare_review':
      return 'prepare review'
    case 'review_in_progress':
      return 'review in progress'
    case 'awaiting_next_round':
      return 'awaiting next round'
    case 'review_failed':
      return 'review failed'
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
      return '评审失败'
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

function EventCard({ event }: { event: EventRecord }) {
  const fullText = stringifyPayload(event.payload)
  const shortText = previewText(fullText)
  const truncated = shortText !== fullText

  return (
    <article className={`event-card level-${event.level}`}>
      <header className="event-card-header">
        <div>
          <div className="event-kind">{event.kind}</div>
          <div className="event-meta">#{event.seq} · {timeText(event.ts)}</div>
        </div>
        <span className={`event-level ${event.level}`}>{event.level}</span>
      </header>
      <div className="event-preview">{shortText}</div>
      {truncated ? (
        <details className="event-details">
          <summary>查看完整输出</summary>
          <pre>{fullText}</pre>
        </details>
      ) : (
        <pre className="event-full">{fullText}</pre>
      )}
    </article>
  )
}

export function App() {
  const [tasks, setTasks] = useState<Task[]>([])
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null)
  const [detail, setDetail] = useState<TaskDetail | null>(null)
  const [repoPath, setRepoPath] = useState('')
  const [prUrl, setPrUrl] = useState('')
  const [reviewFocus, setReviewFocus] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [detailTab, setDetailTab] = useState<DetailTab>('results')
  const [showAllEvents, setShowAllEvents] = useState(false)
  const [showModal, setShowModal] = useState(false)
  const eventSourceRef = useRef<EventSource | null>(null)

  const selectedTask = useMemo(
    () => tasks.find((task) => task.id === selectedTaskId) ?? detail?.task ?? null,
    [detail?.task, selectedTaskId, tasks],
  )

  const visibleEvents = useMemo(() => {
    if (!detail) return []
    const source = showAllEvents ? detail.events : detail.importantEvents
    return showAllEvents ? source : source.slice(-EVENT_LIMIT)
  }, [detail, showAllEvents])

  const latestRound = detail && detail.reviewRounds.length ? detail.reviewRounds[detail.reviewRounds.length - 1] : undefined
  const latestResult = detail?.latestReviewResult ?? selectedTask?.latest_review_result ?? null

  useEffect(() => {
    void refreshTasks()
    const timer = window.setInterval(() => void refreshTasks(), 4000)
    return () => window.clearInterval(timer)
  }, [])

  useEffect(() => {
    if (!selectedTaskId) return
    setDetailTab('results')
    setShowAllEvents(false)
    void loadTask(selectedTaskId)
  }, [selectedTaskId])

  useEffect(() => {
    if (!selectedTaskId) return
    eventSourceRef.current?.close()
    const source = new EventSource(`/api/tasks/${selectedTaskId}/stream`)
    eventSourceRef.current = source
    source.onmessage = () => {
      void loadTask(selectedTaskId)
      void refreshTasks(false)
    }
    source.onerror = () => {
      source.close()
    }
    return () => {
      source.close()
      eventSourceRef.current = null
    }
  }, [selectedTaskId])

  const refreshTasks = async (clearErr = true) => {
    try {
      const data = await api<Task[]>('/api/tasks')
      setTasks(data.sort((a, b) => b.updated_at - a.updated_at))
      if (clearErr) setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load tasks')
    }
  }

  const loadTask = async (taskId: string) => {
    try {
      const data = normalizeTaskDetail(await api<TaskDetailResponse>(`/api/tasks/${taskId}`))
      setDetail(data)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load task detail')
    }
  }

  const createTask = async () => {
    try {
      if (!repoPath.trim() || !prUrl.trim()) {
        setError('请提供仓库路径和 GitHub PR 链接。')
        return
      }
      const task = await api<Task>('/api/tasks', {
        method: 'POST',
        body: JSON.stringify({
          repo_path: repoPath.trim(),
          pr_url: prUrl.trim(),
          review_focus: reviewFocus.trim(),
        }),
      })
      setRepoPath(repoPath.trim())
      setPrUrl('')
      setReviewFocus('')
      setShowModal(false)
      await refreshTasks()
      setSelectedTaskId(task.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create review task')
    }
  }

  const startReviewRound = async (taskId: string) => {
    try {
      await api(`/api/tasks/${taskId}/start`, {
        method: 'POST',
        body: JSON.stringify({}),
      })
      await loadTask(taskId)
      await refreshTasks()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start review round')
    }
  }

  const abortTask = async (taskId: string) => {
    try {
      await api(`/api/tasks/${taskId}/abort`, { method: 'POST' })
      await loadTask(taskId)
      await refreshTasks()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to abort review round')
    }
  }

  const exportSummary = async (taskId: string) => {
    try {
      const result = await api<{ path: string }>(`/api/tasks/${taskId}/export-summary`, { method: 'POST' })
      setError(`Review summary exported to: ${result.path}`)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to export review summary')
    }
  }

  const tabCounts: Record<DetailTab, number> = {
    results: latestResult?.finding_count ?? 0,
    timeline: detail?.eventStats.important_count ?? 0,
    rounds: detail?.reviewRounds.length ?? 0,
    artifacts: detail?.artifacts.length ?? 0,
  }

  const startLabel = latestRound ? '发起下一轮 Review' : '开始首轮 Review'

  return (
    <div className="layout">
      <header className="topbar">
        <span className="topbar-brand">PR Review Control</span>
        <button className="btn-primary" onClick={() => setShowModal(true)}>+ New Review</button>
      </header>

      <div className="body">
        <nav>
          {tasks.map((task) => (
            <button
              key={task.id}
              className={`task-tile ${task.id === selectedTaskId ? 'active' : ''}`}
              onClick={() => setSelectedTaskId(task.id)}
            >
              <div className="task-tile-top">
                <span className="task-title">{task.title}</span>
                <span className={`status-dot tone-${statusTone(task.status)}`}>{statusLabel(task.status)}</span>
              </div>
              <div className="task-tile-meta">
                <span>{stageLabel(task.current_stage)}</span>
                <span>{relativeTimeText(task.updated_at)}</span>
              </div>
              <div className="task-verdict">
                <span className={`mini-pill tone-${verdictTone(task.latest_review_result?.verdict)}`}>
                  {verdictLabel(task.latest_review_result?.verdict)}
                </span>
                <span>{task.latest_review_result?.finding_count ?? 0} findings</span>
              </div>
            </button>
          ))}
          {!tasks.length && <div className="empty-note">暂无 PR review 任务</div>}
        </nav>

        <main>
          {error && <div className="error-banner">{error}</div>}

          {!selectedTask && <div className="empty-state">选择一个 PR review 任务查看详情</div>}

          {selectedTask && detail && (
            <>
              <div className="detail-header">
                <h2>{selectedTask.title}</h2>
                <span className={`status-dot tone-${statusTone(selectedTask.status)}`}>{statusLabel(selectedTask.status)}</span>
                <div className="detail-actions">
                  <button className="btn-primary" onClick={() => startReviewRound(selectedTask.id)} disabled={selectedTask.status === 'running'}>
                    {startLabel}
                  </button>
                  <button className="btn-ghost" onClick={() => exportSummary(selectedTask.id)}>导出结论</button>
                  <button className="btn-danger" onClick={() => abortTask(selectedTask.id)} disabled={selectedTask.status !== 'running'}>
                    终止本轮
                  </button>
                </div>
              </div>

              <div className="detail-meta">
                <span>PR<strong>{selectedTask.source_url || '-'}</strong></span>
                <span>Repo<strong>{selectedTask.repo_path}</strong></span>
                <span>Rounds<strong>{detail.reviewRounds.length}</strong></span>
                <span>Stage<strong>{stageLabel(selectedTask.current_stage)}</strong></span>
                <span>Updated<strong>{relativeTimeText(selectedTask.updated_at)}</strong></span>
              </div>

              <section className="result-strip">
                <article className="result-card">
                  <span>最新评审结论</span>
                  <strong className={`tone-${verdictTone(latestResult?.verdict)}`}>{verdictLabel(latestResult?.verdict)}</strong>
                </article>
                <article className="result-card">
                  <span>问题数</span>
                  <strong>{latestResult?.finding_count ?? 0}</strong>
                </article>
                <article className="result-card">
                  <span>严重级别统计</span>
                  <strong>{SEVERITY_ORDER.map((severity) => `${severityLabel(severity)} ${latestResult?.severity_counts?.[severity] ?? 0}`).join(' · ')}</strong>
                </article>
                <article className="result-card result-card-wide">
                  <span>摘要</span>
                  <strong>{latestResult?.summary || '暂无结构化评审结论。'}</strong>
                </article>
              </section>

              {selectedTask.description && <div className="task-description">{selectedTask.description}</div>}

              <div className="tab-bar">
                {(['results', 'timeline', 'rounds', 'artifacts'] as DetailTab[]).map((tab) => (
                  <button
                    key={tab}
                    className={`tab-item ${detailTab === tab ? 'active' : ''}`}
                    onClick={() => setDetailTab(tab)}
                  >
                    {tab === 'results'
                      ? '结果'
                      : tab === 'timeline'
                        ? '评审事件'
                        : tab === 'rounds'
                          ? '评审轮次'
                          : '产物'}
                    <span className="tab-badge">{tabCounts[tab]}</span>
                  </button>
                ))}
              </div>

              <div className="tab-content">
                {detailTab === 'results' && (
                  <>
                    <div className="section-header"><h3>最新评审结论</h3><span>{verdictLabel(latestResult?.verdict)}</span></div>
                    {latestResult ? (
                      <>
                        <div className="result-summary">
                          <div className="result-summary-line">
                            <span className={`mini-pill tone-${verdictTone(latestResult.verdict)}`}>{verdictLabel(latestResult.verdict)}</span>
                            <span>{latestResult.finding_count} 条问题</span>
                            {latestResult.supersedes_round_index ? <span>来自第 {latestResult.supersedes_round_index} 轮</span> : null}
                          </div>
                          <p>{latestResult.summary || '暂无摘要。'}</p>
                        </div>
                        <div className="section-header"><h3>问题列表</h3><span>{latestResult.finding_count} 条</span></div>
                        <div className="findings-list">
                          {latestResult.findings.map((finding, index) => (
                            <article className="finding-card" key={`${finding.severity}-${finding.path ?? 'na'}-${index}`}>
                              <div className="finding-top">
                                <span className={`mini-pill tone-${finding.severity === 'critical' || finding.severity === 'high' ? 'danger' : finding.severity === 'medium' ? 'warning' : 'neutral'}`}>
                                  {severityLabel(finding.severity)}
                                </span>
                                <span>{finding.path ? `${finding.path}${finding.line ? `:${finding.line}` : ''}` : '路径缺失'}</span>
                              </div>
                              <strong>{finding.summary}</strong>
                              {finding.detail && finding.detail !== finding.summary ? <p>{finding.detail}</p> : null}
                            </article>
                          ))}
                          {!latestResult.findings.length && <div className="empty-note">这一轮没有结构化问题</div>}
                        </div>
                      </>
                    ) : (
                      <div className="empty-note">还没有结构化评审结果</div>
                    )}
                  </>
                )}

                {detailTab === 'timeline' && (
                  <>
                    <div className="timeline-toolbar">
                      <span>
                        {showAllEvents
                          ? `${detail.eventStats.all_count} 条事件`
                          : `${detail.eventStats.important_count} 条重要事件`}
                      </span>
                      {detail.eventStats.hidden_count > 0 && (
                        <button className="btn-ghost" onClick={() => setShowAllEvents((value) => !value)}>
                          {showAllEvents ? '仅看重要事件' : `显示全部 ${detail.eventStats.all_count} 条`}
                        </button>
                      )}
                    </div>
                    <div className="timeline-scroll">
                      {visibleEvents.map((event) => <EventCard key={event.id} event={event} />)}
                      {!visibleEvents.length && <div className="empty-note">暂无事件</div>}
                    </div>
                  </>
                )}

                {detailTab === 'rounds' && (
                  <>
                    <div className="section-header"><h3>评审轮次</h3><span>{detail.reviewRounds.length} 轮</span></div>
                    <div className="stack-list">
                      {detail.reviewRounds.map((round) => (
                        <article className="stack-card" key={round.id}>
                          <strong>第 {round.round_index} 轮</strong>
                          <div>后端: {round.backend}</div>
                          <div>状态: {round.status}</div>
                          <div>结论: {verdictLabel(round.review_result?.verdict)}</div>
                          <div>问题数: {round.review_result?.finding_count ?? 0}</div>
                          <div>摘要: {round.review_result?.summary || '-'}</div>
                          <div>Session: {round.backend_session_id ?? '-'}</div>
                          <div>退出码: {String(round.exit_code ?? '-')}</div>
                          <div>开始时间: {timeText(round.started_at)}</div>
                          <div>结束时间: {timeText(round.ended_at)}</div>
                        </article>
                      ))}
                      {!detail.reviewRounds.length && <div className="empty-note">暂无评审轮次</div>}
                    </div>
                  </>
                )}

                {detailTab === 'artifacts' && (
                  <>
                    <div className="section-header"><h3>产物</h3><span>{detail.artifacts.length} 个文件</span></div>
                    <div className="artifact-grid">
                      {detail.artifacts.map((artifact) => (
                        <article className="artifact-card" key={artifact.path}>
                          <strong>{artifact.relative_path}</strong>
                          <div>{artifact.path}</div>
                        </article>
                      ))}
                      {!detail.artifacts.length && <div className="empty-note">暂无产物</div>}
                    </div>
                  </>
                )}
              </div>
            </>
          )}
        </main>
      </div>

      {showModal && (
        <div className="modal-overlay" onClick={() => setShowModal(false)}>
          <div className="modal" onClick={(event) => event.stopPropagation()}>
            <h3>创建 PR Review</h3>
            <label>仓库路径<input value={repoPath} onChange={(event) => setRepoPath(event.target.value)} placeholder="/path/to/repo" /></label>
            <label>PR 链接<input value={prUrl} onChange={(event) => setPrUrl(event.target.value)} placeholder="https://github.com/org/repo/pull/123" /></label>
            <label>Review Focus<textarea value={reviewFocus} onChange={(event) => setReviewFocus(event.target.value)} rows={4} placeholder="关注风险点、特定模块、兼容性或测试缺口" /></label>
            <div className="modal-actions">
              <button className="btn-ghost" onClick={() => setShowModal(false)}>取消</button>
              <button className="btn-primary" disabled={!repoPath.trim() || !prUrl.trim()} onClick={createTask}>创建</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
