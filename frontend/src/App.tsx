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

type Task = {
  id: string
  title: string
  description: string
  repo_path: string
  backend: string
  status: TaskStatus
  current_stage: string
  source_url?: string | null
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

type TaskDetail = {
  task: Task
  events: EventRecord[]
  runs: Array<Record<string, unknown>>
  checks: Array<Record<string, unknown>>
  artifacts: ArtifactRecord[]
}

type DetailTab = 'timeline' | 'runs' | 'artifacts'

const EVENT_LIMIT = 40

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
  const [title, setTitle] = useState('')
  const [repoPath, setRepoPath] = useState('')
  const [sourceUrl, setSourceUrl] = useState('')
  const [description, setDescription] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [detailTab, setDetailTab] = useState<DetailTab>('timeline')
  const [showAllEvents, setShowAllEvents] = useState(false)
  const [showModal, setShowModal] = useState(false)
  const eventSourceRef = useRef<EventSource | null>(null)

  const selectedTask = useMemo(
    () => tasks.find((task) => task.id === selectedTaskId) ?? detail?.task ?? null,
    [detail?.task, selectedTaskId, tasks],
  )

  const visibleEvents = useMemo(() => {
    if (!detail) return []
    return showAllEvents ? detail.events : detail.events.slice(-EVENT_LIMIT)
  }, [detail, showAllEvents])

  const latestRun = detail?.runs.at(-1)

  useEffect(() => {
    void refreshTasks()
    const timer = window.setInterval(() => void refreshTasks(), 4000)
    return () => window.clearInterval(timer)
  }, [])

  useEffect(() => {
    if (!selectedTaskId) return
    setDetailTab('timeline')
    setShowAllEvents(false)
    void loadTask(selectedTaskId)
  }, [selectedTaskId])

  useEffect(() => {
    if (!selectedTaskId) return
    eventSourceRef.current?.close()
    const source = new EventSource(`/api/tasks/${selectedTaskId}/stream`)
    eventSourceRef.current = source
    source.onmessage = (message) => {
      const event = JSON.parse(message.data) as EventRecord
      setDetail((current) => {
        if (!current || current.task.id !== selectedTaskId) return current
        const deduped = current.events.some((item) => item.id === event.id)
        if (deduped) return current
        return { ...current, events: [...current.events, event] }
      })
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
      const data = await api<TaskDetail>(`/api/tasks/${taskId}`)
      setDetail(data)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load task detail')
    }
  }

  const createTask = async () => {
    try {
      if (!repoPath || (!title.trim() && !sourceUrl.trim())) {
        setError('请至少提供仓库路径，以及任务标题或 GitHub issue 链接。')
        return
      }
      const task = await api<Task>('/api/tasks', {
        method: 'POST',
        body: JSON.stringify({
          title,
          repo_path: repoPath,
          source_url: sourceUrl || null,
          description,
          source_type: sourceUrl ? 'issue_url' : 'manual',
        }),
      })
      setTitle('')
      setRepoPath(repoPath)
      setSourceUrl('')
      setDescription('')
      setShowModal(false)
      await refreshTasks()
      setSelectedTaskId(task.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create task')
    }
  }

  const startTask = async (taskId: string) => {
    try {
      await api(`/api/tasks/${taskId}/start`, {
        method: 'POST',
        body: JSON.stringify({}),
      })
      await loadTask(taskId)
      await refreshTasks()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start task')
    }
  }

  const abortTask = async (taskId: string) => {
    try {
      await api(`/api/tasks/${taskId}/abort`, { method: 'POST' })
      await loadTask(taskId)
      await refreshTasks()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to abort task')
    }
  }

  const validateTask = async (taskId: string) => {
    try {
      await api(`/api/tasks/${taskId}/validate`, { method: 'POST', body: JSON.stringify({}) })
      await loadTask(taskId)
      await refreshTasks()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start validation')
    }
  }

  const exportSummary = async (taskId: string) => {
    try {
      const result = await api<{ path: string }>(`/api/tasks/${taskId}/export-summary`, { method: 'POST' })
      setError(`Summary exported to: ${result.path}`)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to export summary')
    }
  }

  const tabCounts: Record<DetailTab, number> = {
    timeline: detail?.events.length ?? 0,
    runs: (detail?.runs.length ?? 0) + (detail?.checks.length ?? 0),
    artifacts: detail?.artifacts.length ?? 0,
  }

  return (
    <div className="layout">
      <header className="topbar">
        <span className="topbar-brand">Mission Control</span>
        <button className="btn-primary" onClick={() => setShowModal(true)}>+ New Task</button>
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
                <span className={`status-dot tone-${statusTone(task.status)}`}>{task.status}</span>
              </div>
              <div className="task-tile-meta">
                <span>{task.current_stage}</span>
                <span>{relativeTimeText(task.updated_at)}</span>
              </div>
            </button>
          ))}
          {!tasks.length && <div className="empty-note">暂无任务</div>}
        </nav>

        <main>
          {error && <div className="error-banner">{error}</div>}

          {!selectedTask && <div className="empty-state">选择一个任务查看详情</div>}

          {selectedTask && detail && (
            <>
              <div className="detail-header">
                <h2>{selectedTask.title}</h2>
                <span className={`status-dot tone-${statusTone(selectedTask.status)}`}>{selectedTask.status}</span>
                <div className="detail-actions">
                  <button className="btn-primary" onClick={() => startTask(selectedTask.id)} disabled={selectedTask.status === 'running'}>启动</button>
                  <button className="btn-ghost" onClick={() => validateTask(selectedTask.id)}>验证</button>
                  <button className="btn-ghost" onClick={() => exportSummary(selectedTask.id)}>导出总结</button>
                  <button className="btn-danger" onClick={() => abortTask(selectedTask.id)} disabled={selectedTask.status !== 'running'}>终止</button>
                </div>
              </div>

              <div className="detail-meta">
                <span>Repo<strong>{selectedTask.repo_path}</strong></span>
                <span>Backend<strong>{selectedTask.backend}</strong></span>
                <span>Stage<strong>{selectedTask.current_stage}</strong></span>
                <span>Updated<strong>{relativeTimeText(selectedTask.updated_at)}</strong></span>
              </div>

              {selectedTask.description && <div className="task-description">{selectedTask.description}</div>}

              <div className="tab-bar">
                {(['timeline', 'runs', 'artifacts'] as DetailTab[]).map((tab) => (
                  <button
                    key={tab}
                    className={`tab-item ${detailTab === tab ? 'active' : ''}`}
                    onClick={() => setDetailTab(tab)}
                  >
                    {tab === 'timeline' ? 'Timeline' : tab === 'runs' ? 'Runs & Checks' : 'Artifacts'}
                    <span className="tab-badge">{tabCounts[tab]}</span>
                  </button>
                ))}
              </div>

              <div className="tab-content">
                {detailTab === 'timeline' && (
                  <>
                    <div className="timeline-toolbar">
                      <span>{detail.events.length} events</span>
                      {detail.events.length > EVENT_LIMIT && (
                        <button className="btn-ghost" onClick={() => setShowAllEvents((v) => !v)}>
                          {showAllEvents ? '仅看最近' : '查看全部'}
                        </button>
                      )}
                    </div>
                    <div className="timeline-scroll">
                      {visibleEvents.map((event) => <EventCard key={event.id} event={event} />)}
                      {!visibleEvents.length && <div className="empty-note">暂无事件</div>}
                    </div>
                  </>
                )}

                {detailTab === 'runs' && (
                  <>
                    <div className="section-header"><h3>Runs</h3><span>{detail.runs.length} items</span></div>
                    <div className="stack-list">
                      {detail.runs.map((run) => (
                        <article className="stack-card" key={String(run.id)}>
                          <strong>{String(run.backend)}</strong>
                          <div>Status: {String(run.status)}</div>
                          <div>Session: {String(run.backend_session_id ?? '-')}</div>
                          <div>Exit: {String(run.exit_code ?? '-')}</div>
                        </article>
                      ))}
                      {!detail.runs.length && <div className="empty-note">暂无 run 记录</div>}
                    </div>
                    <div className="section-header"><h3>Checks</h3><span>{detail.checks.length} items</span></div>
                    <div className="stack-list">
                      {detail.checks.map((check, index) => (
                        <article className="stack-card" key={String(check.id ?? index)}>
                          <strong>{String(check.name ?? 'check')}</strong>
                          <div>Status: {String(check.status ?? '-')}</div>
                          <div>Exit: {String(check.exit_code ?? '-')}</div>
                        </article>
                      ))}
                      {!detail.checks.length && <div className="empty-note">暂无验证记录</div>}
                    </div>
                  </>
                )}

                {detailTab === 'artifacts' && (
                  <>
                    <div className="section-header"><h3>Artifacts</h3><span>{detail.artifacts.length} files</span></div>
                    <div className="artifact-grid">
                      {detail.artifacts.map((artifact) => (
                        <article className="artifact-card" key={artifact.path}>
                          <strong>{artifact.relative_path}</strong>
                          <div>{artifact.path}</div>
                        </article>
                      ))}
                      {!detail.artifacts.length && <div className="empty-note">暂无 artifact</div>}
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
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3>创建任务</h3>
            <label>标题<input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Fix issue #123" /></label>
            <label>仓库路径<input value={repoPath} onChange={(e) => setRepoPath(e.target.value)} placeholder="/path/to/repo" /></label>
            <label>Issue 链接<input value={sourceUrl} onChange={(e) => setSourceUrl(e.target.value)} placeholder="https://github.com/org/repo/issues/1" /></label>
            <label>附加说明<textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={3} /></label>
            <div className="modal-actions">
              <button className="btn-ghost" onClick={() => setShowModal(false)}>取消</button>
              <button className="btn-primary" disabled={!repoPath || (!title.trim() && !sourceUrl.trim())} onClick={createTask}>创建</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
