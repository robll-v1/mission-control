import { useMemo, useState } from 'react'
import { CloseIcon, SearchIcon } from './Icons'
import {
  RAIL_FILTERS,
  matchesFilter,
  missionId,
  relativeTimeText,
  severityCount,
  statusLabel,
  statusTone,
  verdictLabel,
  verdictEnLabel,
  type RailFilter,
} from '../lib/format'
import type { Task } from '../lib/types'

type TaskRailProps = {
  tasks: Task[]
  selectedTaskId: string | null
  onSelect: (id: string) => void
  open: boolean
  streamLive: boolean
  demoMode: boolean
}

/* Task rail — the launch queue. Search + status filters over the live
   task list; each row reads like a telemetry strip: lamp, title,
   mission id, verdict chip and finding count. */
export function TaskRail({
  tasks,
  selectedTaskId,
  onSelect,
  open,
  streamLive,
  demoMode,
}: TaskRailProps) {
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<RailFilter>('all')

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase()
    return tasks.filter(
      (task) =>
        matchesFilter(task, filter) &&
        (q === '' ||
          task.title.toLowerCase().includes(q) ||
          task.id.toLowerCase().includes(q) ||
          task.repo_path.toLowerCase().includes(q)),
    )
  }, [filter, query, tasks])

  return (
    <nav className={`rail ${open ? 'open' : ''}`} aria-label="审查任务队列">
      <div className="rail-head">
        <span className="micro">
          任务队列<i>/</i>task queue
        </span>
        <span className="rail-count">{String(tasks.length).padStart(2, '0')}</span>
      </div>

      <div className="rail-tools">
        <div className="rail-search">
          <span className="search-icon">
            <SearchIcon />
          </span>
          <input
            type="text"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索任务 / 仓库 / ID"
            aria-label="搜索任务"
          />
          {query && (
            <button className="search-clear" onClick={() => setQuery('')} aria-label="清空搜索">
              <CloseIcon />
            </button>
          )}
        </div>
        <div className="filter-chips" role="group" aria-label="按状态筛选">
          {RAIL_FILTERS.map((item) => (
            <button
              key={item.id}
              className={`chip-filter ${filter === item.id ? 'active' : ''}`}
              aria-pressed={filter === item.id}
              onClick={() => setFilter(item.id)}
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>

      <div className="rail-list">
        {visible.map((task) => {
          const result = task.latest_review_result
          const count = result?.finding_count ?? 0
          const tone = statusTone(task.status)
          return (
            <button
              key={task.id}
              className={`task-row ${task.id === selectedTaskId ? 'active' : ''}`}
              onClick={() => onSelect(task.id)}
              aria-current={task.id === selectedTaskId}
            >
              <span className={`lamp lamp-${tone}`} aria-hidden="true" />
              <span className="task-row-main">
                <span className="task-row-title">{task.title}</span>
                <span className="task-row-meta">
                  {missionId(task.id)} · {statusLabel(task.status)} ·{' '}
                  {relativeTimeText(task.updated_at)}
                </span>
              </span>
              <span className="task-row-side">
                {result ? (
                  <span className={`chip chip-${result.verdict}`}>{verdictEnLabel(result.verdict)}</span>
                ) : (
                  <span className="chip">idle</span>
                )}
                {count > 0 ? (
                  <span className="find-count" title={`${verdictLabel(result?.verdict)} · ${count} 条问题`}>
                    {String(count).padStart(2, '0')} findings
                  </span>
                ) : (
                  <span className="find-count" style={{ opacity: 0.5 }}>
                    {result ? '00 findings' : '—'}
                  </span>
                )}
              </span>
            </button>
          )
        })}
        {!visible.length && (
          <div className="empty-note" style={{ marginTop: 12 }}>
            {tasks.length ? '没有符合筛选条件的任务' : '队列为空 — 新建一次审查开始'}
          </div>
        )}
      </div>

      <div className="rail-foot">
        <span className={`lamp ${streamLive ? 'lamp-live' : 'lamp-danger'}`} aria-hidden="true" />
        <span>{streamLive ? 'SSE 实时流已连接' : '实时流已断开 · 4s 轮询兜底'}</span>
        {demoMode && <span className="demo-chip">demo</span>}
      </div>
    </nav>
  )
}
