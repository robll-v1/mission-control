// Run with: npm test   (from frontend/; Node >= 22.18 strips types natively)
//
// Drives StartTracker with the same calls App.startReviewRound/abortTask
// make, under controlled response orderings.
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { StartTracker } from '../src/startTracker.ts'

type Deferred<T> = { promise: Promise<T>; resolve: (v: T) => void; reject: (e: Error) => void }
const deferred = <T>(): Deferred<T> => {
  let resolve!: (v: T) => void
  let reject!: (e: Error) => void
  const promise = new Promise<T>((a, b) => { resolve = a; reject = b })
  return { promise, resolve, reject }
}

/** Mirrors App: one fake endpoint per request, and the notices shown. */
const harness = () => {
  const tracker = new StartTracker()
  const errors: string[] = []
  const requests = new Map<string, Deferred<unknown>>()
  const api = (path: string) => {
    const d = deferred<unknown>()
    requests.set(path, d)
    return d.promise
  }
  const start = async (taskId: string) => {
    const request = tracker.begin(taskId)
    try {
      await api(`start:${taskId}`)
    } catch (err) {
      if (!(await tracker.wasAborted(request))) errors.push(`${taskId}: ${(err as Error).message}`)
    } finally {
      tracker.end(request)
    }
  }
  const abort = async (taskId: string) => {
    let settle: (accepted: boolean) => void = () => {}
    tracker.noteAbort(taskId, new Promise<boolean>((r) => { settle = r }))
    try {
      await api(`abort:${taskId}`)
      settle(true)
    } catch (err) {
      settle(false)
      errors.push(`${taskId} abort: ${(err as Error).message}`)
    }
  }
  const respond = (path: string, ok: boolean) => {
    const d = requests.get(path)
    assert.ok(d, `no pending request ${path}`)
    if (ok) d.resolve({ ok: true })
    else d.reject(new Error(path.startsWith('start') ? 'review round was cancelled' : 'task is not actively running'))
  }
  return { tracker, errors, start, abort, respond }
}

const flush = () => new Promise((r) => setTimeout(r, 0))

test('starting another task does not turn an aborted start into an error', async () => {
  const h = harness()
  const a = h.start('A')
  void h.abort('A')
  h.respond('abort:A', true)
  void h.start('B') // before A's start returns
  h.respond('start:A', false)
  await a
  assert.deepEqual(h.errors, [])
  assert.deepEqual(h.tracker.pendingTaskIds(), ['B'])
})

test('the start failing before the abort answers is still treated as aborted', async () => {
  const h = harness()
  const a = h.start('A')
  void h.abort('A')
  h.respond('start:A', false)
  await flush()
  h.respond('abort:A', true)
  await a
  assert.deepEqual(h.errors, [])
})

test('a real start failure is shown when the abort was refused', async () => {
  const h = harness()
  const a = h.start('A')
  void h.abort('A')
  h.respond('abort:A', false)
  h.respond('start:A', false)
  await a
  assert.deepEqual(h.errors, ['A abort: task is not actively running', 'A: review round was cancelled'])
})

test('a refused second click does not undo an accepted abort', async () => {
  const h = harness()
  const a = h.start('A')
  const first = h.abort('A')
  h.respond('abort:A', true)
  await first
  void h.abort('A') // double click; the backend answers 409
  h.respond('abort:A', false)
  h.respond('start:A', false)
  await a
  assert.deepEqual(h.errors, ['A abort: task is not actively running'])
})

test('aborting one task does not hide another task failing', async () => {
  const h = harness()
  const a = h.start('A')
  const b = h.start('B')
  void h.abort('A')
  h.respond('abort:A', true)
  h.respond('start:A', false)
  h.respond('start:B', false)
  await Promise.all([a, b])
  assert.deepEqual(h.errors, ['B: review round was cancelled'])
})

test('a restart of the same task does not inherit the earlier abort', async () => {
  const h = harness()
  const first = h.start('A')
  void h.abort('A')
  h.respond('abort:A', true)
  h.respond('start:A', false)
  await first
  const second = h.start('A')
  h.respond('start:A', false)
  await second
  assert.deepEqual(h.errors, ['A: review round was cancelled'])
  assert.equal(h.tracker.isPending('A'), false)
})

test('an abort with no pending start attaches to nothing', () => {
  const tracker = new StartTracker()
  assert.equal(tracker.noteAbort('A', Promise.resolve(true)), undefined)
})
