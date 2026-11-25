import { useState } from 'react'
import type { Messages } from '../i18n'
import { formatClockTime } from './api'
import {
  CheckIcon,
  CommentIcon,
  FolderIcon,
  InfoIcon,
  PlayIcon,
  TerminalIcon,
  WarningIcon,
} from './Icons'
import type { EventRecord, EventStats } from './types'

type Props = {
  events: EventRecord[]
  importantEvents: EventRecord[]
  eventStats: EventStats
  isRunning: boolean
  t: Messages
}

function summarizeEvent(ev: EventRecord): { title: string; meta: string; tone: 'normal' | 'ok' | 'warn' | 'error' | 'accent'; icon: string } {
  const p = ev.payload || {}
  const kind = ev.kind
  if (ev.level === 'error' || kind === 'error') {
    const msg = String(p.message || p.error || p.reason || kind)
    return { title: msg, meta: kind, tone: 'error', icon: 'warn' }
  }
  if (p.policy_violation || p.warning_reason) {
    const cmd = String(p.command || p.tool || p.text || kind)
    const reason = String(p.warning_reason || 'blocked by static review policy')
    return { title: `Flagged ${cmd}`, meta: reason, tone: 'warn', icon: 'warn' }
  }
  if (kind === 'review.round_started') {
    const parts = [p.backend || p.agent, p.model, p.reasoning ? `reasoning ${p.reasoning}` : null, p.round_index ? `round ${p.round_index}` : null].filter(Boolean)
    return { title: 'Review started', meta: parts.join(' · '), tone: 'accent', icon: 'play' }
  }
  if (kind === 'review.round_finished' || kind === 'review.result_extracted') {
    const verdict = p.verdict ? String(p.verdict) : ''
    const count = typeof p.finding_count === 'number' ? `${p.finding_count} findings` : ''
    return { title: kind === 'review.round_finished' ? 'Round finished' : 'Review results parsed', meta: [verdict, count].filter(Boolean).join(' · '), tone: 'ok', icon: 'check' }
  }
  if (kind === 'task.local_diff_ingested' || kind === 'task.local_snapshot_refreshed') {
    const files = typeof p.changed_files === 'number' ? `${p.changed_files} file(s)` : ''
    const base = p.base_branch ? `against ${p.base_branch}` : ''
    return { title: 'Snapshot captured', meta: [files, base].filter(Boolean).join(' '), tone: 'normal', icon: 'folder' }
  }
  if (kind === 'task.context_compiled' || kind === 'task.review_context_compiled') {
    const files = typeof p.changed_files === 'number' ? `${p.changed_files} changed file(s)` : ''
    return { title: 'Review context ready', meta: files, tone: 'normal', icon: 'folder' }
  }
  if (kind === 'agent.tool_use') {
    const cmd = String(p.command || p.tool || 'tool call')
    const exit = typeof p.exit_code === 'number' ? `exit ${p.exit_code}` : ''
    return { title: `Ran ${cmd}`, meta: exit, tone: 'normal', icon: 'terminal' }
  }
  if (kind === 'agent.text' || kind === 'agent.message') {
    const text = String(p.text || p.message || '').trim()
    const preview = text.length > 140 ? `${text.slice(0, 140)}…` : text || 'Agent output'
    return { title: preview, meta: 'agent commentary', tone: 'normal', icon: 'comment' }
  }
  if (kind === 'task.stage_changed') {
    const stage = String(p.stage || p.status || '')
    return { title: `Stage: ${stage}`, meta: String(p.status || ''), tone: 'normal', icon: 'info' }
  }
  if (kind === 'finding.status_changed') {
    const title = String(p.title || p.finding_id || '')
    const st = String(p.status || '')
    return { title: `Finding marked ${st}`, meta: title, tone: 'normal', icon: 'check' }
  }
  return { title: kind, meta: '', tone: 'normal', icon: 'info' }
}

function EventIcon({ type }: { type: string }) {
  switch (type) {
    case 'check':
      return <span className="wb-ev-icon ok"><CheckIcon size={11} /></span>
    case 'play':
      return <span className="wb-ev-icon accent"><PlayIcon size={10} /></span>
    case 'warn':
      return <span className="wb-ev-icon warn"><WarningIcon size={13} /></span>
    case 'terminal':
      return <span className="wb-ev-icon"><TerminalIcon size={13} /></span>
    case 'comment':
      return <span className="wb-ev-icon"><CommentIcon size={13} /></span>
    case 'folder':
      return <span className="wb-ev-icon"><FolderIcon size={13} /></span>
    default:
      return <span className="wb-ev-icon"><InfoIcon size={13} /></span>
  }
}

export function ActivityFeed({ events, importantEvents, eventStats, isRunning, t }: Props) {
  const [showAll, setShowAll] = useState(false)
  const [showRawJson, setShowRawJson] = useState(false)

  const list = showAll ? events : importantEvents

  if (events.length === 0) {
    return <div className="wb-empty-panel">{t.timeline.empty}</div>
  }

  return (
    <div className="wb-activity-wrap">
      <div className="wb-activity-list">
        {list.map((ev) => {
          const info = summarizeEvent(ev)
          return (
            <div key={ev.id} className={`wb-activity-row wb-activity-${info.tone}`}>
              <span className="wb-activity-time wb-mono">{formatClockTime(ev.ts)}</span>
              <EventIcon type={info.icon} />
              <div className="wb-activity-body">
                <div className="wb-activity-main">
                  <span className="wb-activity-title">{info.title}</span>
                  {info.meta && <span className="wb-activity-meta">{info.meta}</span>}
                </div>
                {showRawJson && (
                  <pre className="wb-raw-json wb-mono">{JSON.stringify(ev, null, 2)}</pre>
                )}
              </div>
            </div>
          )
        })}

        {isRunning && (
          <div className="wb-activity-row wb-activity-live">
            <span className="wb-activity-time wb-mono">{formatClockTime(Date.now() / 1000)}</span>
            <span className="wb-live-pulse" />
            <div className="wb-activity-body">
              <div className="wb-activity-main">
                <span className="wb-t2">{t.status.running}…</span>
                <span className="wb-ac wb-b wb-sm">{t.workbench.live}</span>
              </div>
            </div>
          </div>
        )}
      </div>

      <div className="wb-activity-footer">
        {eventStats.hidden_count > 0 && (
          <button
            type="button"
            className="wb-text-btn"
            onClick={() => setShowAll((v) => !v)}
          >
            {showAll ? t.timeline.importantOnly : t.timeline.showAll(eventStats.all_count)}
          </button>
        )}
        <button
          type="button"
          className="wb-text-btn wb-t2"
          onClick={() => setShowRawJson((v) => !v)}
        >
          {t.workbench.rawJson}
        </button>
      </div>
    </div>
  )
}
