/**
 * Pure helpers for incremental SSE event consumption and deduplication (P4).
 *
 * Imports nothing and touches no browser globals so `node --test` runs it directly.
 */

export type StreamEventRecord = {
  id: string
  kind: string
  level: 'info' | 'warning' | 'error'
  seq: number
  ts: number
  payload: Record<string, unknown>
}

export type StreamEventStats = {
  all_count: number
  important_count: number
  hidden_count: number
}

const NOISY_EVENT_KINDS = new Set(['agent.step_start', 'agent.step_finish'])

const LIFECYCLE_REFRESH_KINDS = new Set([
  'task.created',
  'task.stage_changed',
  'task.pull_request_ingested',
  'task.pull_request_refreshed',
  'task.local_diff_ingested',
  'task.local_snapshot_refreshed',
  'task.context_compiled',
  'task.review_context_compiled',
  'task.worktree_cleaned',
  'task.worktree_cleanup_failed',
  'review.round_started',
  'review.result_extracted',
  'review.round_finished',
  'finding.status_changed',
  'control.accepted',
  'error',
])

export function isImportantEvent(event: StreamEventRecord): boolean {
  if (NOISY_EVENT_KINDS.has(event.kind)) return false
  if (event.kind === 'agent.tool_use' && event.level === 'info') {
    const payload = event.payload || {}
    if (!payload.policy_violation && !payload.warning_reason) {
      return false
    }
  }
  return true
}

export function shouldRefreshTaskOnEvent(event: StreamEventRecord): boolean {
  return (
    LIFECYCLE_REFRESH_KINDS.has(event.kind) ||
    event.kind.startsWith('validation.') ||
    event.level === 'error'
  )
}

export function parseStreamEventPayload(raw: string): StreamEventRecord | null {
  try {
    const obj = JSON.parse(raw) as Record<string, unknown>
    if (!obj || typeof obj !== 'object') return null
    if (typeof obj.id !== 'string' || typeof obj.kind !== 'string' || typeof obj.seq !== 'number') {
      return null
    }
    const level =
      obj.level === 'warning' || obj.level === 'error' || obj.level === 'info' ? obj.level : 'info'
    return {
      id: obj.id,
      kind: obj.kind,
      level,
      seq: obj.seq,
      ts: typeof obj.ts === 'number' ? obj.ts : 0,
      payload:
        obj.payload && typeof obj.payload === 'object' && !Array.isArray(obj.payload)
          ? (obj.payload as Record<string, unknown>)
          : {},
    }
  } catch {
    return null
  }
}

function insertOrderedBySeq(
  list: StreamEventRecord[],
  incoming: StreamEventRecord,
): { list: StreamEventRecord[]; added: boolean } {
  for (let i = list.length - 1; i >= 0; i -= 1) {
    if (list[i].seq === incoming.seq || list[i].id === incoming.id) {
      return { list, added: false }
    }
    if (list[i].seq < incoming.seq) {
      const next = [...list.slice(0, i + 1), incoming, ...list.slice(i + 1)]
      return { list: next, added: true }
    }
  }
  return { list: [incoming, ...list], added: true }
}

/**
 * Deduplicate and merge an incoming SSE event into the current task detail's
 * event arrays in ascending `seq` order, updating `eventStats` accurately.
 */
export function mergeStreamEvent(
  events: StreamEventRecord[],
  importantEvents: StreamEventRecord[],
  incoming: StreamEventRecord,
): {
  events: StreamEventRecord[]
  importantEvents: StreamEventRecord[]
  eventStats: StreamEventStats
  changed: boolean
} {
  const allResult = insertOrderedBySeq(events, incoming)
  if (!allResult.added) {
    return {
      events,
      importantEvents,
      eventStats: {
        all_count: events.length,
        important_count: importantEvents.length,
        hidden_count: Math.max(events.length - importantEvents.length, 0),
      },
      changed: false,
    }
  }
  const impResult = isImportantEvent(incoming)
    ? insertOrderedBySeq(importantEvents, incoming)
    : { list: importantEvents, added: false }

  return {
    events: allResult.list,
    importantEvents: impResult.list,
    eventStats: {
      all_count: allResult.list.length,
      important_count: impResult.list.length,
      hidden_count: Math.max(allResult.list.length - impResult.list.length, 0),
    },
    changed: true,
  }
}
