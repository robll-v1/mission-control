import { useEffect, useState } from 'react'
import type { Messages } from '../i18n'
import { formatElapsed, formatShortTime } from './api'
import { CheckIcon } from './Icons'
import type { ReviewRound, Task } from './types'

export type PendingRoundSpec = {
  backend: string
  model?: string | null
  reasoning?: string | null
}

/** Only Codex CLI passes an explicit `--sandbox read-only` OS sandbox flag. */
export function hasReadOnlySandbox(backend?: string | null): boolean {
  return (backend || '').trim().toLowerCase() === 'codex'
}

type Props = {
  task: Task
  activeRound: ReviewRound | null
  pendingSpec?: PendingRoundSpec | null
  roundNumber: number
  t: Messages
}

function stepIndexForStage(stage: string, status: string): number {
  if (stage === 'preflight') return 0
  if (stage === 'prepare_review' || status === 'ingesting') return 1
  if (stage === 'review_in_progress' || status === 'running') return 2
  if (stage === 'review_done' || status === 'completed') return 3
  return 2
}

export function ProgressStepper({ task, activeRound, pendingSpec, roundNumber, t }: Props) {
  const [nowSec, setNowSec] = useState(() => Date.now() / 1000)

  useEffect(() => {
    const id = setInterval(() => setNowSec(Date.now() / 1000), 1000)
    return () => clearInterval(id)
  }, [])

  const activeIdx = stepIndexForStage(task.current_stage, task.status)
  // During preflight/prepare of a new round, `activeRound` may still be a prior
  // completed round; prefer the pending round spec or task defaults instead.
  const isRoundRunningNow = Boolean(activeRound && activeRound.status === 'running')
  const roundSource = isRoundRunningNow ? activeRound : null
  const currentBackend =
    roundSource?.settings?.agent || roundSource?.backend || pendingSpec?.backend || task.backend
  const currentModel =
    roundSource?.settings?.model ?? roundSource?.model ?? pendingSpec?.model ?? task.model
  const currentReasoning =
    roundSource?.settings?.reasoning ?? roundSource?.reasoning ?? pendingSpec?.reasoning ?? task.reasoning

  const startedAt = roundSource?.started_at ?? task.updated_at
  const elapsed = Math.max(0, nowSec - startedAt)
  const modelLabel = [
    currentModel || t.settings.agentDefault,
    currentReasoning,
    formatElapsed(elapsed),
  ]
    .filter(Boolean)
    .join(' · ')

  const steps = [
    { label: t.workbench.stepCheckAgent, sub: currentBackend },
    { label: t.workbench.stepPrepareContext, sub: t.workbench.filesChanged(task.review_paths.length) },
    { label: t.workbench.stepReview, sub: modelLabel },
    { label: t.workbench.stepReadResults, sub: t.workbench.stepFindingsVerdict },
  ]

  const sandboxed = hasReadOnlySandbox(currentBackend)

  return (
    <div className="wb-card wb-progress-card">
      <div className="wb-progress-top">
        <h3>{t.workbench.roundRunningTitle(roundNumber)}</h3>
        <span className="wb-sm wb-t3">
          {formatShortTime(startedAt)}
          {sandboxed ? ` · ${t.workbench.sandboxReadOnly}` : ''}
        </span>
      </div>
      <div className="wb-stepper" role="list">
        {steps.map((step, idx) => {
          const state = idx < activeIdx ? 'done' : idx === activeIdx ? 'active' : 'pending'
          return (
            <div key={step.label} className={`wb-step wb-step-${state}`} role="listitem">
              <div className="wb-step-track">
                {idx > 0 && <span className={`wb-step-line ${idx <= activeIdx ? 'done' : ''}`} />}
                <span className={`wb-step-node wb-step-node-${state}`}>
                  {state === 'done' ? <CheckIcon size={12} /> : state === 'active' ? <span className="wb-step-dot" /> : null}
                </span>
                {idx < steps.length - 1 && <span className={`wb-step-line ${idx < activeIdx ? 'done' : ''}`} />}
              </div>
              <div className="wb-step-label">{step.label}</div>
              <div className="wb-step-sub">{step.sub}</div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
