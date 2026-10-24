/* API client — same contract as before, plus a DEV-only demo fallback.
   Fallback triggers only when the backend is unreachable (network error
   or 5xx from the vite proxy) AND we are in a dev build; real 4xx
   business errors still surface to the user. */

import { DEMO_TASKS, DEMO_DETAILS, demoState } from './demo'
import type { Task, TaskDetailResponse } from './types'

const isDev = import.meta.env.DEV

/** `?demo=1` forces the demo dataset even when a backend is reachable —
    a design-review switch for previewing every console state at once. */
const forceDemo = () =>
  isDev &&
  typeof window !== 'undefined' &&
  new URLSearchParams(window.location.search).get('demo') === '1'

const isFallbackWorthy = (error: unknown, status?: number) => {
  if (!isDev) return false
  if (forceDemo()) return true
  if (error instanceof TypeError) return true // fetch itself failed (ECONNREFUSED etc.)
  return typeof status === 'number' && status >= 500 // vite proxy could not reach backend
}

export const api = async <T,>(path: string, init?: RequestInit): Promise<T> => {
  const method = (init?.method ?? 'GET').toUpperCase()

  // Forced demo mode short-circuits GETs entirely (design-review switch).
  if (isDev && forceDemo() && method === 'GET') {
    const forced = demoResponse<T>(path)
    if (forced !== undefined) return forced
  }

  let status: number | undefined
  try {
    const response = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...init,
    })
    if (!response.ok) {
      status = response.status
      const text = await response.text()
      let message = text
      try {
        const payload = JSON.parse(text) as { detail?: unknown }
        if (typeof payload.detail === 'string') message = payload.detail
      } catch {
        // Keep a non-JSON response as-is.
      }
      throw new ApiError(message || `Request failed: ${response.status}`, status)
    }
    return response.json() as Promise<T>
  } catch (err) {
    if (isFallbackWorthy(err, status)) {
      const demo = demoResponse<T>(path)
      if (demo !== undefined) return demo
    }
    throw err
  }
}

class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message)
    this.name = 'ApiError'
  }
}

/** Serve demo data for the two GET endpoints; mutations are not faked. */
function demoResponse<T>(path: string): T | undefined {
  if (path === '/api/tasks') {
    demoState.active = true
    return [...DEMO_TASKS].sort((a, b) => b.updated_at - a.updated_at) as unknown as T
  }
  const match = /^\/api\/tasks\/([\w-]+)$/.exec(path)
  if (match) {
    const detail = DEMO_DETAILS[match[1]]
    if (detail) {
      demoState.active = true
      return detail as unknown as T
    }
  }
  return undefined
}

export const demoActive = () => demoState.active

export type { Task, TaskDetailResponse }
