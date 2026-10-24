import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { MissionPanel } from './components/MissionPanel'
import { NewReviewModal } from './components/NewReviewModal'
import { TaskRail } from './components/TaskRail'
import { TopBar } from './components/TopBar'
import { MenuIcon } from './components/Icons'
import { Settings } from './Settings'
import { api, demoActive } from './lib/api'
import { normalizeDetail } from './lib/normalize'
import { THEME_KEY } from './lib/format'
import type {
  DetailTab,
  Notice,
  ReviewSource,
  Task,
  TaskDetailResponse,
  Theme,
} from './lib/types'

/* Mission Control — app shell.
   Data flow is unchanged from the previous version (4s poll + SSE
   detail refresh); everything here is presentation + orchestration. */
export function App() {
  const [tasks, setTasks] = useState<Task[]>([])
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null)
  const [detail, setDetail] = useState<ReturnType<typeof normalizeDetail> | null>(null)
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
  const [streamLive, setStreamLive] = useState(false)
  const [demoMode, setDemoMode] = useState(false)
  const [theme, setTheme] = useState<Theme>(() => {
    if (typeof window === 'undefined') return 'dark'
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

  // `refreshTasks` runs on a 4s timer. It must never clear a notice raised by a
  // user action, otherwise failed actions silently look like no-ops.
  const refreshTasks = useCallback(async (options?: { silent?: boolean }) => {
    try {
      const data = await api<Task[]>('/api/tasks')
      setTasks(data.sort((a, b) => b.updated_at - a.updated_at))
      setDemoMode(demoActive())
    } catch (err) {
      if (!options?.silent) {
        setNotice({ tone: 'error', text: err instanceof Error ? err.message : '任务列表加载失败' })
      }
    }
  }, [])

  const loadTask = useCallback(async (taskId: string, options?: { silent?: boolean }) => {
    try {
      const data = normalizeDetail(await api<TaskDetailResponse>(`/api/tasks/${taskId}`))
      setDetail(data)
      setDemoMode(demoActive())
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

  // Live event stream; on any error we fall back to the 4s poll above.
  useEffect(() => {
    if (!selectedTaskId) return
    setStreamLive(false)
    eventSourceRef.current?.close()
    const source = new EventSource(`/api/tasks/${selectedTaskId}/stream`)
    eventSourceRef.current = source
    source.onopen = () => setStreamLive(true)
    source.onmessage = () => {
      void loadTask(selectedTaskId, { silent: true })
      void refreshTasks({ silent: true })
    }
    source.onerror = () => {
      setStreamLive(false)
      source.close()
    }
    return () => {
      source.close()
      eventSourceRef.current = null
    }
  }, [selectedTaskId, loadTask, refreshTasks])

  // Info notices self-dismiss; errors stay until acknowledged.
  useEffect(() => {
    if (!notice || notice.tone !== 'info') return
    const timer = window.setTimeout(() => setNotice(null), 5000)
    return () => window.clearTimeout(timer)
  }, [notice])

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
      const result = await api<{ path: string }>(`/api/tasks/${taskId}/export-summary`, {
        method: 'POST',
      })
      setNotice({ tone: 'info', text: `摘要已导出到 ${result.path}` })
    } catch (err) {
      setNotice({ tone: 'error', text: err instanceof Error ? err.message : '导出摘要失败' })
    }
  }

  const selectTask = (id: string) => {
    setSelectedTaskId(id)
    setNavOpen(false)
  }

  if (showSettings) return <Settings onBack={() => setShowSettings(false)} />

  return (
    <div className="console">
      <div className="atmosphere" aria-hidden="true" />
      <div className="grain" aria-hidden="true" />

      <TopBar
        theme={theme}
        onToggleTheme={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
        onOpenSettings={() => setShowSettings(true)}
        onNewReview={() => setShowModal(true)}
        onToggleRail={() => setNavOpen((v) => !v)}
        railOpen={navOpen}
        demoMode={demoMode}
        menuIcon={<MenuIcon />}
      />

      <div className="console-body">
        {navOpen && <div className="scrim" onClick={() => setNavOpen(false)} />}

        <TaskRail
          tasks={tasks}
          selectedTaskId={selectedTaskId}
          onSelect={selectTask}
          open={navOpen}
          streamLive={streamLive}
          demoMode={demoMode}
        />

        <main className="stage">
          <div className="stage-inner">
            {notice && (
              <div
                className={`banner ${notice.tone === 'error' ? 'banner-error' : 'banner-info'}`}
                role="status"
              >
                <span>{notice.text}</span>
                <button className="banner-close" onClick={() => setNotice(null)} aria-label="关闭提示">
                  ✕
                </button>
              </div>
            )}

            {!selectedTask && (
              <div className="empty-screen">
                <span className="empty-mark" aria-hidden="true" />
                <span className="micro">no signal / 无信号</span>
                <h2>等待任务接入</h2>
                <p>从左侧队列选择一个审查任务查看结论与问题列表，或发起一次新的 PR / 本地变更审查。</p>
                <button className="btn btn-primary" onClick={() => setShowModal(true)}>
                  新建审查
                </button>
              </div>
            )}

            {selectedTask && !detail && <div className="empty-note">正在载入任务遥测数据…</div>}

            {selectedTask && detail && (
              <MissionPanel
                detail={detail}
                tab={detailTab}
                onTabChange={setDetailTab}
                showAllEvents={showAllEvents}
                onToggleAllEvents={() => setShowAllEvents((v) => !v)}
                onStartReview={() => void startReviewRound(selectedTask.id)}
                onExport={() => void exportSummary(selectedTask.id)}
                onAbort={() => void abortTask(selectedTask.id)}
              />
            )}
          </div>
        </main>
      </div>

      {showModal && (
        <NewReviewModal
          repoPath={repoPath}
          setRepoPath={setRepoPath}
          reviewSource={reviewSource}
          setReviewSource={setReviewSource}
          prUrl={prUrl}
          setPrUrl={setPrUrl}
          baseBranch={baseBranch}
          setBaseBranch={setBaseBranch}
          reviewFocus={reviewFocus}
          setReviewFocus={setReviewFocus}
          isCreating={isCreating}
          onCreate={() => void createTask()}
          onClose={() => setShowModal(false)}
        />
      )}
    </div>
  )
}
