import { useMemo } from 'react'
import { labelFor, type Messages } from '../i18n'
import type { BackendCapability, SavedConfig } from '../runSettings'
import { formatShortTime, repoBasename } from './api'
import type { Task } from './types'

export type SidebarFilter = 'all' | 'attention' | 'running'

type Props = {
  tasks: Task[]
  selectedTaskId: string | null
  repoFilter: string
  statusFilter: SidebarFilter
  backends: BackendCapability[]
  savedConfig: SavedConfig | null
  mobileOpen: boolean
  t: Messages
  onSelectTask: (taskId: string) => void
  onChangeFilter: (filter: SidebarFilter) => void
  onOpenSettings: () => void
  onCloseMobile: () => void
}

function isRunningStatus(status: string): boolean {
  return status === 'ingesting' || status === 'running' || status === 'validating'
}

function needsAttention(task: Task): boolean {
  if (task.status === 'failed') return true
  const verdict = task.latest_review_result?.verdict
  return verdict === 'concerns' || verdict === 'failed'
}

function statusDotTone(task: Task): 'ok' | 'warn' | 'error' | 'accent' | 'muted' {
  if (isRunningStatus(task.status)) return 'accent'
  if (task.status === 'failed') return 'error'
  const verdict = task.latest_review_result?.verdict
  if (verdict === 'clear') return 'ok'
  if (verdict === 'concerns') return 'warn'
  if (verdict === 'failed') return 'error'
  return 'muted'
}

export function WorkbenchSidebar({
  tasks,
  selectedTaskId,
  repoFilter,
  statusFilter,
  backends,
  savedConfig,
  mobileOpen,
  t,
  onSelectTask,
  onChangeFilter,
  onOpenSettings,
  onCloseMobile,
}: Props) {
  const repoScoped = useMemo(
    () => (repoFilter ? tasks.filter((item) => item.repo_path === repoFilter) : tasks),
    [tasks, repoFilter],
  )

  const counts = useMemo(
    () => ({
      all: repoScoped.length,
      attention: repoScoped.filter(needsAttention).length,
      running: repoScoped.filter((item) => isRunningStatus(item.status)).length,
    }),
    [repoScoped],
  )

  const visibleTasks = useMemo(() => {
    if (statusFilter === 'attention') return repoScoped.filter(needsAttention)
    if (statusFilter === 'running') return repoScoped.filter((item) => isRunningStatus(item.status))
    return repoScoped
  }, [repoScoped, statusFilter])

  const grouped = useMemo(() => {
    const map = new Map<string, Task[]>()
    for (const item of visibleTasks) {
      const key = item.repo_path || ''
      const list = map.get(key)
      if (list) list.push(item)
      else map.set(key, [item])
    }
    return [...map.entries()]
  }, [visibleTasks])

  const defaultAgentName = savedConfig?.backend || backends[0]?.name || ''

  return (
    <>
      {mobileOpen && (
        <div className="wb-drawer-scrim" role="presentation" onClick={onCloseMobile} />
      )}
      <aside className={`wb-sidebar ${mobileOpen ? 'open' : ''}`} aria-label={t.sidebar.heading}>
        <div className="wb-sidebar-top">
          <div className="wb-sidebar-head">
            <span className="wb-cap">{t.sidebar.heading}</span>
            <span className="wb-xs wb-t3">{counts.all}</span>
          </div>
          <div className="wb-filter-pills" role="tablist" aria-label={t.sidebar.heading}>
            <button
              type="button"
              role="tab"
              aria-selected={statusFilter === 'all'}
              className={`wb-fpill ${statusFilter === 'all' ? 'active' : ''}`}
              onClick={() => onChangeFilter('all')}
            >
              {t.workbench.filterAll}
              <span className="wb-fpill-count">{counts.all}</span>
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={statusFilter === 'attention'}
              className={`wb-fpill ${statusFilter === 'attention' ? 'active' : ''}`}
              onClick={() => onChangeFilter('attention')}
            >
              {t.workbench.filterAttention}
              {counts.attention > 0 && <span className="wb-fpill-count">{counts.attention}</span>}
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={statusFilter === 'running'}
              className={`wb-fpill ${statusFilter === 'running' ? 'active' : ''}`}
              onClick={() => onChangeFilter('running')}
            >
              {t.workbench.filterRunning}
              {counts.running > 0 && <span className="wb-fpill-count">{counts.running}</span>}
            </button>
          </div>
        </div>

        <div className="wb-sidebar-list">
          {grouped.length === 0 ? (
            <div className="wb-sidebar-empty">{t.sidebar.empty}</div>
          ) : (
            grouped.map(([repo, items]) => (
              <div key={repo || 'default'} className="wb-repo-group">
                {!repoFilter && (
                  <div className="wb-repo-group-head" title={repo}>
                    <span className="wb-mono wb-xs wb-t3 wb-truncate">{repoBasename(repo)}</span>
                  </div>
                )}
                {items.map((task) => {
                  const selected = task.id === selectedTaskId
                  const tone = statusDotTone(task)
                  const verdict = task.latest_review_result?.verdict
                  const findingCount = task.latest_review_result?.finding_count ?? 0
                  const subtitle = isRunningStatus(task.status)
                    ? labelFor(t.stage, task.current_stage)
                    : verdict
                      ? labelFor(t.verdict, verdict)
                      : labelFor(t.status, task.status)
                  return (
                    <button
                      key={task.id}
                      type="button"
                      className={`wb-task-item ${selected ? 'selected' : ''}`}
                      onClick={() => {
                        onSelectTask(task.id)
                        onCloseMobile()
                      }}
                    >
                      <div className="wb-task-item-top">
                        <span className={`wb-dot ${tone}`} />
                        <span className="wb-task-item-title wb-truncate">{task.title}</span>
                        <span className="wb-xs wb-t3 wb-mono">{formatShortTime(task.updated_at)}</span>
                      </div>
                      <div className="wb-task-item-meta">
                        <span className="wb-xs wb-t2 wb-truncate">{subtitle}</span>
                        <span className="wb-xs wb-t3 wb-mono">
                          {task.source_type === 'pull_request' && task.pr_number
                            ? `PR #${task.pr_number}`
                            : task.backend}
                          {findingCount > 0 ? ` · ${findingCount}` : ''}
                        </span>
                      </div>
                    </button>
                  )
                })}
              </div>
            ))
          )}
        </div>

        <button type="button" className="wb-sidebar-footer" onClick={onOpenSettings}>
          <span className={`wb-dot ${backends.length > 0 ? 'ok' : 'warn'}`} />
          <span className="wb-xs wb-t2 wb-truncate">
            {backends.length > 0 && defaultAgentName
              ? t.workbench.agentsFooter(backends.length, defaultAgentName)
              : t.workbench.noAgentsFooter}
          </span>
        </button>
      </aside>
    </>
  )
}
