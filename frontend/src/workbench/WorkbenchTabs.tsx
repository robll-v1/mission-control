import { labelFor, type Messages } from '../i18n'
import { FileDocIcon } from './Icons'
import type { ArtifactEntry, CheckRun, DiffResponse } from './types'

export function ChangesTab({
  diffData,
  loading,
  reviewPaths,
  t,
  onOpenInEditor,
}: {
  diffData: DiffResponse | null
  loading: boolean
  reviewPaths: string[]
  t: Messages
  onOpenInEditor: (path: string, line?: number | null) => void
}) {
  const files = diffData?.changed_files.length ? diffData.changed_files : reviewPaths

  if (loading && !diffData) {
    return <div className="wb-empty-panel">Loading…</div>
  }

  if (!diffData?.diff && files.length === 0) {
    return <div className="wb-empty-panel">{t.workbench.changesEmpty}</div>
  }

  const lines = diffData?.diff ? diffData.diff.split('\n') : []

  return (
    <div className="wb-changes-wrap">
      {files.length > 0 && (
        <div className="wb-file-pills">
          {files.map((file) => (
            <button
              key={file}
              type="button"
              className="wb-file-pill wb-mono"
              onClick={() => onOpenInEditor(file, 1)}
              title={t.workbench.openInEditor}
            >
              <FileDocIcon size={12} />
              <span>{file}</span>
            </button>
          ))}
        </div>
      )}

      {diffData?.truncated && (
        <div className="wb-alert wb-alert-warn">{t.workbench.changesTruncated}</div>
      )}

      {lines.length > 0 ? (
        <div className="wb-diff-box wb-mono">
          {lines.map((line, idx) => {
            let cls = 'wb-diff-line'
            if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('diff ')) {
              cls += ' meta'
            } else if (line.startsWith('@@')) {
              cls += ' hunk'
            } else if (line.startsWith('+')) {
              cls += ' add'
            } else if (line.startsWith('-')) {
              cls += ' del'
            }
            return (
              <div key={idx} className={cls}>
                {line || ' '}
              </div>
            )
          })}
        </div>
      ) : (
        <div className="wb-empty-panel">{t.workbench.changesEmpty}</div>
      )}
    </div>
  )
}

export function ChecksTab({
  checks,
  runningValidation,
  disabled,
  t,
  onRunChecks,
}: {
  checks: CheckRun[]
  runningValidation: boolean
  disabled: boolean
  t: Messages
  onRunChecks: (mode: 'standard' | 'full') => void
}) {
  return (
    <div className="wb-checks-wrap">
      <div className="wb-checks-bar">
        <span className="wb-cap">{t.workbench.checksHeading}</span>
        <div className="wb-checks-actions">
          <button
            type="button"
            className="wb-btn wb-btn-secondary wb-btn-sm"
            disabled={disabled || runningValidation}
            onClick={() => onRunChecks('standard')}
          >
            {t.workbench.runChecksStandard}
          </button>
          <button
            type="button"
            className="wb-btn wb-btn-secondary wb-btn-sm"
            disabled={disabled || runningValidation}
            onClick={() => onRunChecks('full')}
          >
            {t.workbench.runChecksFull}
          </button>
        </div>
      </div>

      {checks.length === 0 ? (
        <div className="wb-empty-panel">{t.workbench.checksEmpty}</div>
      ) : (
        <div className="wb-checks-list">
          {checks.map((check) => {
            const ok = check.status === 'passed' || check.exit_code === 0
            const failed = check.status === 'failed' || (typeof check.exit_code === 'number' && check.exit_code !== 0)
            const tone = ok ? 'ok' : failed ? 'error' : 'neutral'
            const logFile = check.stdout_path || check.stderr_path || check.log_path
            return (
              <div key={check.id} className="wb-card wb-check-card">
                <div className="wb-check-card-head">
                  <span className="wb-b">{check.name}</span>
                  <span className={`wb-status-pill wb-tone-${tone}`}>
                    {labelFor(t.status, check.status)}
                    {typeof check.exit_code === 'number' ? ` (${check.exit_code})` : ''}
                  </span>
                </div>
                <div className="wb-mono wb-xs wb-t2 wb-check-cmd">{check.command}</div>
                <div className="wb-check-meta wb-xs wb-t3">
                  {typeof check.duration_sec === 'number' && (
                    <span>{check.duration_sec.toFixed(1)}s</span>
                  )}
                  {logFile && <span className="wb-mono wb-truncate">{logFile}</span>}
                </div>
                {check.output_excerpt && (
                  <pre className="wb-raw-json wb-mono">{check.output_excerpt}</pre>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

export function ArtifactsTab({
  artifacts,
  t,
}: {
  artifacts: ArtifactEntry[]
  t: Messages
}) {
  if (artifacts.length === 0) {
    return <div className="wb-empty-panel">{t.artifacts.empty}</div>
  }
  return (
    <div className="wb-card wb-artifacts-list">
      {artifacts.map((art) => (
        <div key={art.path} className="wb-artifact-row">
          <FileDocIcon size={14} className="wb-t3" />
          <div className="wb-artifact-paths">
            <span className="wb-mono wb-sm wb-b">{art.relative_path}</span>
            <span className="wb-mono wb-xs wb-t3 wb-truncate">{art.path}</span>
          </div>
        </div>
      ))}
    </div>
  )
}
