// Run with: npm test (from frontend/; Node >= 22.18 strips types natively)
import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  isImportantEvent,
  mergeEventLists,
  mergeStreamEvent,
  parseStreamEventPayload,
  shouldRefreshTaskOnEvent,
  type StreamEventRecord,
} from '../src/eventStream.ts'

const ev = (seq: number, kind = 'agent.text', level: 'info' | 'warning' | 'error' = 'info', payload = {}): StreamEventRecord => ({
  id: `e${seq}`,
  seq,
  ts: seq,
  kind,
  level,
  payload,
})

test('parseStreamEventPayload validates JSON SSE payloads safely', () => {
  const parsed = parseStreamEventPayload(JSON.stringify(ev(3, 'agent.text', 'info', { text: 'hi' })))
  assert.ok(parsed)
  assert.equal(parsed.seq, 3)
  assert.equal(parsed.kind, 'agent.text')
  assert.equal(parseStreamEventPayload('not-json'), null)
  assert.equal(parseStreamEventPayload(JSON.stringify({ kind: 'missing-seq' })), null)
})

test('mergeStreamEvent deduplicates by seq/id, preserves order, and filters noisy events', () => {
  const e1 = ev(1, 'task.created')
  const e2 = ev(2, 'agent.step_start')
  const e3 = ev(3, 'agent.tool_use', 'info', { tool: 'read' })
  const e4 = ev(4, 'agent.tool_use', 'info', { tool: 'bash', policy_violation: true })
  const e5 = ev(5, 'review.round_finished')

  let events: StreamEventRecord[] = []
  let important: StreamEventRecord[] = []

  for (const item of [e1, e5, e2, e3, e4]) {
    const merged = mergeStreamEvent(events, important, item)
    assert.equal(merged.changed, true)
    events = merged.events
    important = merged.importantEvents
  }

  assert.deepEqual(events.map((e) => e.seq), [1, 2, 3, 4, 5])
  assert.deepEqual(important.map((e) => e.seq), [1, 4, 5])

  // Duplicate seq from reconnect/boundary overlap must be ignored
  const dup = mergeStreamEvent(events, important, ev(4, 'agent.tool_use'))
  assert.equal(dup.changed, false)
  assert.equal(dup.events.length, 5)
  assert.deepEqual(dup.eventStats, { all_count: 5, important_count: 3, hidden_count: 2 })
})

test('isImportantEvent and shouldRefreshTaskOnEvent avoid HTTP storms on noisy agent output', () => {
  assert.equal(isImportantEvent(ev(1, 'agent.step_start')), false)
  assert.equal(isImportantEvent(ev(2, 'agent.tool_use', 'info', { tool: 'grep' })), false)
  assert.equal(isImportantEvent(ev(3, 'agent.tool_use', 'warning', { warning_reason: 'blocked' })), true)

  assert.equal(shouldRefreshTaskOnEvent(ev(10, 'agent.text')), false)
  assert.equal(shouldRefreshTaskOnEvent(ev(11, 'agent.step_start')), false)
  assert.equal(shouldRefreshTaskOnEvent(ev(12, 'agent.tool_use')), false)
  assert.equal(shouldRefreshTaskOnEvent(ev(13, 'review.round_finished')), true)
  assert.equal(shouldRefreshTaskOnEvent(ev(14, 'finding.status_changed')), true)
  assert.equal(shouldRefreshTaskOnEvent(ev(15, 'task.stage_changed')), true)
  assert.equal(shouldRefreshTaskOnEvent(ev(16, 'validation.finished')), true)
  assert.equal(shouldRefreshTaskOnEvent(ev(17, 'agent.text', 'error')), true)
})

test('mergeEventLists preserves buffered SSE events when initial or stale GET snapshot arrives', () => {
  const snapshot = [ev(1, 'task.created')]
  const buffered = [ev(2, 'agent.text'), ev(3, 'agent.step_start')]
  const merged = mergeEventLists(snapshot, snapshot, buffered)
  assert.deepEqual(merged.events.map((e) => e.seq), [1, 2, 3])
  assert.deepEqual(merged.importantEvents.map((e) => e.seq), [1, 2])
  assert.deepEqual(merged.eventStats, { all_count: 3, important_count: 2, hidden_count: 1 })
})
