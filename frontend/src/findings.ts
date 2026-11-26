/**
 * Pure helpers for first-class cross-round Findings (P3).
 *
 * Imports nothing and touches no browser globals so `node --test` runs it directly.
 */

export type FindingStatus =
  | 'new'
  | 'persistent'
  | 'unverified'
  | 'likely_fixed'
  | 'resolved'
  | 'dismissed'

export type FindingAction = 'resolve' | 'dismiss' | 'reopen'

export type FindingFilter = 'open' | 'all' | FindingStatus

export type AttestationStance = 'confirmed' | 'disputed' | 'uncertain'

export type FindingAttestation = {
  id: string
  task_id: string
  finding_id: string
  kind?: string
  stance: AttestationStance
  backend: string
  model?: string | null
  reasoning?: string | null
  cross_model: boolean
  review_revision?: string | null
  rationale: string
  evidence?: string
  trigger_path?: string
  created_at?: number
}

export type AttestationCounts = {
  confirmed: number
  disputed: number
  uncertain: number
  unattested: number
  cross_model_verified: number
}

export type TrackedFinding = {
  id: string
  task_id: string
  fingerprint: string
  category?: string
  severity: string
  path?: string | null
  line?: number | null
  title: string
  detail?: string
  first_round: number
  last_round: number
  status: FindingStatus
  system_status: FindingStatus
  user_status?: FindingStatus | null
  location_changed?: boolean
  reopened?: boolean
  mentioned_after_dismiss?: boolean
  previous_path?: string | null
  previous_line?: number | null
  created_at?: number
  updated_at?: number
  attestations?: FindingAttestation[]
}

export type FindingCounts = {
  all: number
  open: number
  new: number
  persistent: number
  unverified: number
  likely_fixed: number
  resolved: number
  dismissed: number
}

const SEVERITY_RANK: Record<string, number> = {
  critical: 0,
  high: 1,
  medium: 2,
  low: 3,
}

const STATUS_RANK: Record<FindingStatus, number> = {
  new: 0,
  persistent: 1,
  unverified: 2,
  likely_fixed: 3,
  resolved: 4,
  dismissed: 5,
}

export function isClosedFinding(status: FindingStatus): boolean {
  return status === 'resolved' || status === 'dismissed'
}

export function computeFindingCounts(findings: TrackedFinding[]): FindingCounts {
  const counts: FindingCounts = {
    all: findings.length,
    open: 0,
    new: 0,
    persistent: 0,
    unverified: 0,
    likely_fixed: 0,
    resolved: 0,
    dismissed: 0,
  }
  for (const item of findings) {
    if (Object.prototype.hasOwnProperty.call(counts, item.status)) {
      counts[item.status] += 1
    }
    if (!isClosedFinding(item.status)) {
      counts.open += 1
    }
  }
  return counts
}

export function sortTrackedFindings(findings: TrackedFinding[]): TrackedFinding[] {
  return [...findings].sort((a, b) => {
    const closedDiff = Number(isClosedFinding(a.status)) - Number(isClosedFinding(b.status))
    if (closedDiff !== 0) return closedDiff
    const sevA = SEVERITY_RANK[a.severity.toLowerCase()] ?? 9
    const sevB = SEVERITY_RANK[b.severity.toLowerCase()] ?? 9
    if (sevA !== sevB) return sevA - sevB
    const statusDiff = (STATUS_RANK[a.status] ?? 9) - (STATUS_RANK[b.status] ?? 9)
    if (statusDiff !== 0) return statusDiff
    if (a.first_round !== b.first_round) return a.first_round - b.first_round
    return a.id.localeCompare(b.id)
  })
}

export function filterFindings(findings: TrackedFinding[], filter: FindingFilter): TrackedFinding[] {
  if (filter === 'all') return findings
  if (filter === 'open') return findings.filter((item) => !isClosedFinding(item.status))
  return findings.filter((item) => item.status === filter)
}

export function applyUpdatedFinding(
  list: TrackedFinding[],
  updated: TrackedFinding,
): TrackedFinding[] {
  let replaced = false
  const next = list.map((item) => {
    if (item.id === updated.id) {
      replaced = true
      return updated
    }
    return item
  })
  if (!replaced) next.push(updated)
  return sortTrackedFindings(next)
}

export function findingLocationText(finding: Pick<TrackedFinding, 'path' | 'line'>): string | null {
  if (!finding.path) return null
  return finding.line && finding.line > 0 ? `${finding.path}:${finding.line}` : finding.path
}

export function latestAttestation(finding: TrackedFinding): FindingAttestation | null {
  const list = finding.attestations
  if (!list || list.length === 0) return null
  return list[list.length - 1]
}

export function computeAttestationCounts(findings: TrackedFinding[]): AttestationCounts {
  const counts: AttestationCounts = {
    confirmed: 0,
    disputed: 0,
    uncertain: 0,
    unattested: 0,
    cross_model_verified: 0,
  }
  for (const item of findings) {
    const att = latestAttestation(item)
    if (!att) {
      counts.unattested += 1
      continue
    }
    if (att.stance === 'confirmed') counts.confirmed += 1
    else if (att.stance === 'disputed') counts.disputed += 1
    else counts.uncertain += 1
    if (att.cross_model) counts.cross_model_verified += 1
  }
  return counts
}

export function buildFixPrompt(finding: TrackedFinding): string {
  const loc = findingLocationText(finding)
  const header = `[${finding.severity.toUpperCase()}]${loc ? ` ${loc}` : ''} — ${finding.title}`
  const body = finding.detail && finding.detail !== finding.title ? `\n\n${finding.detail}` : ''
  return `Please fix the following code review finding and add/update tests to verify the fix:\n\n${header}${body}`
}
