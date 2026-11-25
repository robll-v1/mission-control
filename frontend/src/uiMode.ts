/**
 * UI mode selector for gradual P6 Direction A workbench rollout.
 *
 * - Default (and `?ui=next`): Direction A Lean Workbench (`WorkbenchApp`)
 * - `?ui=legacy`: Previous console UI (`App`) kept as an escape hatch
 *
 * Imports nothing and touches no browser globals at import time so `node --test`
 * runs it directly.
 */

export type UiMode = 'next' | 'legacy'

export function resolveUiMode(search?: string): UiMode {
  const query =
    search !== undefined
      ? search
      : typeof window !== 'undefined'
        ? window.location.search
        : ''
  if (!query) return 'next'
  const params = new URLSearchParams(query.startsWith('?') ? query : `?${query}`)
  const raw = (params.get('ui') || '').trim().toLowerCase()
  if (raw === 'legacy' || raw === 'v1' || raw === 'classic') return 'legacy'
  return 'next'
}
