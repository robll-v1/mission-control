import { computeFindingCounts } from '../findings'
import type { TaskDetail, TaskDetailResponse } from './types'

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
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
      // Keep non-JSON text as-is.
    }
    throw new Error(message || `Request failed (${response.status})`)
  }
  return response.json() as Promise<T>
}

export function normalizeTaskDetail(payload: TaskDetailResponse): TaskDetail {
  const events = payload.events ?? []
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
    rounds: payload.review_rounds ?? payload.runs ?? [],
    checks: payload.checks ?? [],
    artifacts: payload.artifacts ?? [],
  }
}

export function repoBasename(repoPath: string): string {
  const cleaned = (repoPath || '').replace(/[\\/]+$/, '')
  if (!cleaned) return 'repository'
  const parts = cleaned.split(/[\\/]/)
  return parts[parts.length - 1] || cleaned
}

export function formatShortTime(ts: number): string {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  return `${hh}:${mm}`
}

export function formatClockTime(ts: number): string {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  const ss = String(d.getSeconds()).padStart(2, '0')
  return `${hh}:${mm}:${ss}`
}

export function formatElapsed(sec: number): string {
  const safe = Math.max(0, Math.floor(sec))
  const m = Math.floor(safe / 60)
  const s = safe % 60
  return m > 0 ? `${m}:${String(s).padStart(2, '0')}` : `${s}s`
}
