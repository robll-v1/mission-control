import { useEffect, useState } from 'react'
import {
  buildFixPrompt,
  findingLocationText,
  isClosedFinding,
  type FindingAction,
  type TrackedFinding,
} from '../findings'
import { labelFor, type Messages } from '../i18n'
import type { SnippetResponse } from './types'

type Props = {
  taskId: string
  finding: TrackedFinding
  updating: boolean
  t: Messages
  onUpdateStatus: (findingId: string, action: FindingAction) => void
  onCopyPrompt: (prompt: string) => void
  onOpenInEditor: (path: string, line?: number | null) => void
}

export function FindingCard({
  taskId,
  finding,
  updating,
  t,
  onUpdateStatus,
  onCopyPrompt,
  onOpenInEditor,
}: Props) {
  const snippetKey = `${taskId}:${finding.path ?? ''}:${finding.line ?? ''}`
  const [snippetState, setSnippetState] = useState<{ key: string; data: SnippetResponse } | null>(null)
  const [expanded, setExpanded] = useState(!isClosedFinding(finding.status) && finding.status !== 'likely_fixed')

  const loc = findingLocationText(finding)
  const sevKey = (finding.severity || 'low').toLowerCase()
  const closed = isClosedFinding(finding.status)
  const compact = !expanded && (finding.status === 'likely_fixed' || closed)
  const snippet = snippetState?.key === snippetKey ? snippetState.data : null

  useEffect(() => {
    setSnippetState((prev) => (prev?.key === snippetKey ? prev : null))
    if (compact || !finding.path || !finding.line || finding.line < 1) return
    let cancelled = false
    const params = new URLSearchParams({
      path: finding.path,
      line: String(finding.line),
      context: '1',
    })
    fetch(`/api/tasks/${taskId}/snippet?${params.toString()}`)
      .then(async (res) => {
        if (!res.ok || cancelled) return
        const parsed = (await res.json()) as SnippetResponse
        if (cancelled) return
        setSnippetState({ key: snippetKey, data: parsed })
      })
      .catch(() => {
        if (!cancelled) setSnippetState(null)
      })
    return () => {
      cancelled = true
    }
  }, [compact, snippetKey, taskId, finding.path, finding.line])

  const statusText = () => {
    const base = labelFor(t.findings.status, finding.status)
    if (finding.status === 'persistent') return `${base} · ${t.findings.roundSpan(finding.first_round, finding.last_round)}`
    if (finding.status === 'likely_fixed') return `${base}`
    return base
  }

  const statusTone = () => {
    switch (finding.status) {
      case 'likely_fixed':
      case 'resolved':
        return 'ok'
      case 'new':
        return 'accent'
      case 'persistent':
      case 'unverified':
        return 'neutral'
      case 'dismissed':
      default:
        return 'muted'
    }
  }

  if (compact) {
    return (
      <div className="wb-finding-card wb-finding-compact">
        <div className="wb-finding-compact-left" onClick={() => setExpanded(true)} role="button" tabIndex={0} onKeyDown={(e) => e.key === 'Enter' && setExpanded(true)}>
          <span className={`wb-sev-pill wb-sev-${sevKey}`}>
            {labelFor(t.severity, sevKey)}
          </span>
          <span className="wb-finding-title">{finding.title}</span>
          {loc && <span className="wb-mono wb-ac wb-sm">{loc}</span>}
        </div>
        <div className="wb-finding-compact-right">
          <span className={`wb-status-pill wb-tone-${statusTone()}`}>{statusText()}</span>
          {finding.status === 'likely_fixed' && (
            <button
              type="button"
              className="wb-btn wb-btn-secondary wb-btn-sm"
              disabled={updating}
              onClick={() => onUpdateStatus(finding.id, 'resolve')}
            >
              {t.findings.markResolved}
            </button>
          )}
          {closed && (
            <button
              type="button"
              className="wb-btn wb-btn-secondary wb-btn-sm"
              disabled={updating}
              onClick={() => onUpdateStatus(finding.id, 'reopen')}
            >
              {t.findings.undo}
            </button>
          )}
        </div>
      </div>
    )
  }

  return (
    <div className="wb-finding-card">
      <div className="wb-finding-head">
        <div className="wb-finding-title-group">
          <span className={`wb-sev-pill wb-sev-${sevKey}`}>
            {labelFor(t.severity, sevKey)}
          </span>
          <h4 className="wb-finding-title">{finding.title}</h4>
        </div>
        <div className="wb-finding-badges">
          {finding.reopened && (
            <span className="wb-status-pill wb-tone-warn">{t.findings.reopened}</span>
          )}
          {finding.location_changed && (
            <span className="wb-status-pill wb-tone-neutral">{t.findings.locationChanged}</span>
          )}
          <span className={`wb-status-pill wb-tone-${statusTone()}`}>{statusText()}</span>
        </div>
      </div>

      <div className="wb-finding-meta">
        {loc ? (
          finding.path ? (
            <button
              type="button"
              className="wb-loc-link wb-mono"
              onClick={() => onOpenInEditor(finding.path!, finding.line)}
            >
              {loc}
            </button>
          ) : (
            <span className="wb-mono wb-t2">{loc}</span>
          )
        ) : (
          <span className="wb-xs wb-t3">{t.findings.noLocation}</span>
        )}
        <span className="wb-xs wb-t3">
          {t.findings.roundSpan(finding.first_round, finding.last_round)}
        </span>
        {finding.location_changed && finding.previous_path && (
          <span className="wb-xs wb-t3">
            {t.findings.previousLocation(
              finding.previous_line ? `${finding.previous_path}:${finding.previous_line}` : finding.previous_path,
            )}
          </span>
        )}
        {finding.mentioned_after_dismiss && (
          <span className="wb-xs wb-warn-text">
            {t.findings.mentionedAfterDismiss(finding.last_round)}
          </span>
        )}
      </div>

      {finding.detail && finding.detail !== finding.title && (
        <p className="wb-finding-detail">{finding.detail}</p>
      )}

      {snippet && snippet.lines.length > 0 && (
        <div className="wb-snippet" aria-label={snippet.path}>
          {snippet.lines.map((ln) => (
            <div
              key={ln.number}
              className={`wb-snippet-line ${ln.highlighted ? 'highlighted' : ''}`}
            >
              <span className="wb-snippet-num wb-mono">{ln.number}</span>
              <code className="wb-snippet-code wb-mono">{ln.text || ' '}</code>
            </div>
          ))}
        </div>
      )}

      {finding.status === 'likely_fixed' && (
        <p className="wb-xs wb-t2 wb-finding-hint">{t.findings.likelyFixedHint}</p>
      )}

      <div className="wb-finding-actions">
        <div className="wb-finding-actions-left">
          <button
            type="button"
            className="wb-btn wb-btn-secondary wb-btn-sm"
            onClick={() => onCopyPrompt(buildFixPrompt(finding))}
          >
            {t.findings.copyFixPrompt}
          </button>
          {finding.path && (
            <button
              type="button"
              className="wb-btn wb-btn-secondary wb-btn-sm"
              onClick={() => onOpenInEditor(finding.path!, finding.line)}
            >
              {t.workbench.openInEditor}
            </button>
          )}
          {!closed ? (
            <>
              <button
                type="button"
                className="wb-btn wb-btn-secondary wb-btn-sm"
                disabled={updating}
                onClick={() => onUpdateStatus(finding.id, 'resolve')}
              >
                {t.findings.markResolved}
              </button>
              <button
                type="button"
                className="wb-btn wb-btn-ghost wb-btn-sm"
                disabled={updating}
                onClick={() => onUpdateStatus(finding.id, 'dismiss')}
              >
                {t.findings.dismiss}
              </button>
            </>
          ) : (
            <button
              type="button"
              className="wb-btn wb-btn-secondary wb-btn-sm"
              disabled={updating}
              onClick={() => onUpdateStatus(finding.id, 'reopen')}
            >
              {t.findings.undo}
            </button>
          )}
        </div>
        {(finding.status === 'likely_fixed' || closed) && (
          <button type="button" className="wb-text-btn wb-xs" onClick={() => setExpanded(false)}>
            Collapse
          </button>
        )}
      </div>
    </div>
  )
}
