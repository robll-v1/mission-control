// Run with: npm test (from frontend/; Node >= 22.18 strips types natively)
import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  applyUpdatedFinding,
  buildFixPrompt,
  computeAttestationCounts,
  computeFindingCounts,
  filterFindings,
  findingLocationText,
  independentReporters,
  isClosedFinding,
  isConsensusFinding,
  latestAttestation,
  latestFalsification,
  type TrackedFinding,
} from '../src/findings.ts'

const SAMPLE: TrackedFinding[] = [
  {
    id: 'f1',
    task_id: 't1',
    fingerprint: 'fp1',
    category: 'sql-injection',
    severity: 'critical',
    path: 'notes.py',
    line: 48,
    title: 'SQL injection in search()',
    detail: 'Bind the keyword as a SQL parameter.',
    first_round: 1,
    last_round: 1,
    status: 'likely_fixed',
    system_status: 'likely_fixed',
    user_status: null,
  },
  {
    id: 'f2',
    task_id: 't1',
    fingerprint: 'fp2',
    category: 'resource-leak',
    severity: 'high',
    path: 'notes.py',
    line: 12,
    title: 'DB connection never closed',
    detail: 'Close connection in context manager.',
    first_round: 1,
    last_round: 2,
    status: 'persistent',
    system_status: 'persistent',
    user_status: null,
    location_changed: true,
    previous_path: 'notes.py',
    previous_line: 10,
  },
  {
    id: 'f3',
    task_id: 't1',
    fingerprint: 'fp3',
    category: 'style',
    severity: 'low',
    path: 'notes.py',
    line: 90,
    title: 'Unused helper',
    first_round: 1,
    last_round: 2,
    status: 'dismissed',
    system_status: 'persistent',
    user_status: 'dismissed',
    mentioned_after_dismiss: true,
  },
]

test('computeFindingCounts tallies open, closed, and per-status totals', () => {
  assert.deepEqual(computeFindingCounts(SAMPLE), {
    all: 3,
    open: 2,
    new: 0,
    persistent: 1,
    unverified: 0,
    likely_fixed: 1,
    resolved: 0,
    dismissed: 1,
  })
  assert.equal(isClosedFinding('likely_fixed'), false)
  assert.equal(isClosedFinding('resolved'), true)
  assert.equal(isClosedFinding('dismissed'), true)
})

test('filterFindings supports open, all, and individual statuses', () => {
  assert.deepEqual(filterFindings(SAMPLE, 'open').map((f) => f.id), ['f1', 'f2'])
  assert.deepEqual(filterFindings(SAMPLE, 'all').map((f) => f.id), ['f1', 'f2', 'f3'])
  assert.deepEqual(filterFindings(SAMPLE, 'likely_fixed').map((f) => f.id), ['f1'])
  assert.deepEqual(filterFindings(SAMPLE, 'dismissed').map((f) => f.id), ['f3'])
})

test('applyUpdatedFinding updates status and re-sorts open items before closed', () => {
  const resolvedF1: TrackedFinding = {
    ...SAMPLE[0],
    status: 'resolved',
    user_status: 'resolved',
  }
  const afterResolve = applyUpdatedFinding(SAMPLE, resolvedF1)
  // f2 (open, high) now sorts ahead of resolved f1 and dismissed f3
  assert.deepEqual(afterResolve.map((f) => f.id), ['f2', 'f1', 'f3'])

  // Undo f1 back to likely_fixed -> critical open f1 moves back to first
  const reopenedF1: TrackedFinding = {
    ...resolvedF1,
    status: resolvedF1.system_status,
    user_status: null,
  }
  const afterUndo = applyUpdatedFinding(afterResolve, reopenedF1)
  assert.deepEqual(afterUndo.map((f) => f.id), ['f1', 'f2', 'f3'])
})

test('buildFixPrompt formats severity, location, title, and detail', () => {
  assert.equal(findingLocationText(SAMPLE[0]), 'notes.py:48')
  const prompt = buildFixPrompt(SAMPLE[0])
  assert.ok(prompt.includes('[CRITICAL] notes.py:48 — SQL injection in search()'))
  assert.ok(prompt.includes('Bind the keyword as a SQL parameter.'))
})

test('latestAttestation and computeAttestationCounts summarize verification stances', () => {
  const withAttestations: TrackedFinding[] = [
    {
      ...SAMPLE[0],
      attestations: [
        {
          id: 'a1',
          task_id: 't1',
          finding_id: 'f1',
          stance: 'uncertain',
          backend: 'codex',
          cross_model: false,
          rationale: 'initial',
        },
        {
          id: 'a2',
          task_id: 't1',
          finding_id: 'f1',
          stance: 'confirmed',
          backend: 'claude-code',
          cross_model: true,
          rationale: 'verified',
        },
      ],
    },
    {
      ...SAMPLE[1],
      attestations: [
        {
          id: 'a3',
          task_id: 't1',
          finding_id: 'f2',
          stance: 'disputed',
          backend: 'claude-code',
          cross_model: true,
          rationale: 'guarded upstream',
        },
      ],
    },
    SAMPLE[2],
  ]
  assert.equal(latestAttestation(withAttestations[0])?.id, 'a2')
  assert.equal(latestAttestation(withAttestations[2]), null)
  assert.deepEqual(computeAttestationCounts(withAttestations), {
    confirmed: 1,
    disputed: 1,
    uncertain: 0,
    unattested: 1,
    cross_model_verified: 2,
  })
})

test('independentReporters, isConsensusFinding, and latestFalsification distinguish blind jury consensus from falsification', () => {
  const consensusFinding: TrackedFinding = {
    ...SAMPLE[0],
    attestations: [
      {
        id: 'ir1',
        task_id: 't1',
        finding_id: 'f1',
        kind: 'independent_review',
        stance: 'confirmed',
        backend: 'codex',
        cross_model: true,
        rationale: 'Reported independently by codex during blind dual-review jury.',
      },
      {
        id: 'ir2',
        task_id: 't1',
        finding_id: 'f1',
        kind: 'independent_review',
        stance: 'confirmed',
        backend: 'claude-code',
        cross_model: true,
        rationale: 'Reported independently by claude-code during blind dual-review jury.',
      },
    ],
  }
  assert.deepEqual(independentReporters(consensusFinding), ['codex', 'claude-code'])
  assert.equal(isConsensusFinding(consensusFinding), true)
  assert.equal(latestFalsification(consensusFinding), null)

  const uniqueWithFalsification: TrackedFinding = {
    ...SAMPLE[1],
    attestations: [
      {
        id: 'ir3',
        task_id: 't1',
        finding_id: 'f2',
        kind: 'independent_review',
        stance: 'confirmed',
        backend: 'codex',
        cross_model: false,
        rationale: 'Reported independently by codex.',
      },
      {
        id: 'fal1',
        task_id: 't1',
        finding_id: 'f2',
        kind: 'falsification',
        stance: 'disputed',
        backend: 'claude-code',
        cross_model: true,
        rationale: 'Connection is closed in caller context manager.',
      },
    ],
  }
  assert.deepEqual(independentReporters(uniqueWithFalsification), ['codex'])
  assert.equal(isConsensusFinding(uniqueWithFalsification), false)
  assert.equal(latestFalsification(uniqueWithFalsification)?.id, 'fal1')
})
