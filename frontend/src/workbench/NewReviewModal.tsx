import { useEffect, useMemo, useRef, useState } from 'react'
import {
  DEFAULT_REVIEW_LANGUAGE,
  errorMessage,
  type Localized,
  type Messages,
  type ReviewLanguage,
} from '../i18n'
import {
  resolveReasoningOptions,
  savedForAgent,
  type BackendCapability,
  type SavedConfig,
} from '../runSettings'
import { api } from './api'
import { CheckIcon, CloseIcon, InfoIcon, WarningIcon } from './Icons'
import type { RepoStatus, Task } from './types'

type Props = {
  open: boolean
  backends: BackendCapability[]
  savedConfig: SavedConfig | null
  recentRepos: string[]
  defaultRepo: string
  t: Messages
  onClose: () => void
  onCreated: (task: Task, startImmediately: boolean) => void
  onNotice: (notice: { tone: 'error' | 'info'; text: Localized }) => void
}

const FOCUS_PRESETS = ['Security', 'Correctness', 'Performance', 'Tests'] as const

export function NewReviewModal({
  open,
  backends,
  savedConfig,
  recentRepos,
  defaultRepo,
  t,
  onClose,
  onCreated,
  onNotice,
}: Props) {
  const [sourceType, setSourceType] = useState<'local_diff' | 'pull_request'>('local_diff')
  const [repoPath, setRepoPath] = useState(defaultRepo)
  const [prUrl, setPrUrl] = useState('')
  const [baseBranch, setBaseBranch] = useState('')
  const [language, setLanguage] = useState<ReviewLanguage>(DEFAULT_REVIEW_LANGUAGE)
  const [agent, setAgent] = useState('')
  const [model, setModel] = useState('')
  const [reasoning, setReasoning] = useState('')
  const [selectedPresets, setSelectedPresets] = useState<string[]>([])
  const [customFocus, setCustomFocus] = useState('')
  const [repoStatus, setRepoStatus] = useState<RepoStatus | null>(null)
  const [submitting, setSubmitting] = useState<'create' | 'start' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const wasOpenRef = useRef(false)

  useEffect(() => {
    const justOpened = open && !wasOpenRef.current
    wasOpenRef.current = open
    if (!justOpened) return
    setError(null)
    setRepoPath(defaultRepo)
    setBaseBranch('')
    setLanguage(DEFAULT_REVIEW_LANGUAGE)
    // Default to Auto so repo `.amc.yaml` / global config resolve unless overridden.
    setAgent('')
    setModel('')
    setReasoning('')
  }, [open, defaultRepo])

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !submitting) onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, submitting, onClose])

  useEffect(() => {
    if (!open) return
    let cancelled = false
    const trimmed = repoPath.trim()
    const timer = setTimeout(() => {
      const params = new URLSearchParams()
      if (trimmed) params.set('repo_path', trimmed)
      fetch(`/api/config/repo-status?${params.toString()}`)
        .then(async (res) => {
          if (!res.ok || cancelled) return
          setRepoStatus((await res.json()) as RepoStatus)
        })
        .catch(() => {
          if (!cancelled) setRepoStatus(null)
        })
    }, 220)
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [open, repoPath])

  const activeCap = useMemo(
    () => backends.find((b) => b.name === agent),
    [backends, agent],
  )
  const reasoningOpts = useMemo(
    () => resolveReasoningOptions(activeCap, model),
    [activeCap, model],
  )

  if (!open) return null

  const handleAgentChange = (nextAgent: string) => {
    setAgent(nextAgent)
    const saved = savedForAgent(savedConfig, nextAgent)
    setModel(saved.model)
    const cap = backends.find((b) => b.name === nextAgent)
    const opts = resolveReasoningOptions(cap, saved.model)
    setReasoning(
      saved.reasoning && opts.supported && (opts.levels.length === 0 || opts.levels.includes(saved.reasoning))
        ? saved.reasoning
        : '',
    )
  }

  const handleModelChange = (nextModel: string) => {
    setModel(nextModel)
    const opts = resolveReasoningOptions(activeCap, nextModel)
    if (reasoning && (!opts.supported || (opts.levels.length > 0 && !opts.levels.includes(reasoning)))) {
      setReasoning('')
    }
  }

  const togglePreset = (preset: string) => {
    setSelectedPresets((prev) =>
      prev.includes(preset) ? prev.filter((item) => item !== preset) : [...prev, preset],
    )
  }

  const submit = async (startImmediately: boolean) => {
    const trimmedRepo = repoPath.trim()
    const trimmedPr = prUrl.trim()
    if (!trimmedRepo || (sourceType === 'pull_request' && !trimmedPr)) {
      setError(sourceType === 'pull_request' ? t.errors.missingRepoAndPr : t.errors.missingRepo)
      return
    }
    if (submitting) return
    setSubmitting(startImmediately ? 'start' : 'create')
    setError(null)
    try {
      const focusParts = [...selectedPresets, customFocus.trim()].filter(Boolean)
      const body: Record<string, string | null> = {
        repo_path: trimmedRepo,
        source_type: sourceType,
        pr_url: sourceType === 'pull_request' ? trimmedPr : null,
        base: sourceType === 'local_diff' ? baseBranch.trim() || null : null,
        review_focus: focusParts.join('; '),
        language,
      }
      if (agent.trim()) {
        body.backend = agent.trim()
        if (model.trim()) body.model = model.trim()
        if (reasoningOpts.supported && reasoning.trim()) body.reasoning = reasoning.trim()
      }
      const task = await api<Task>('/api/tasks', {
        method: 'POST',
        body: JSON.stringify(body),
      })
      setPrUrl('')
      setCustomFocus('')
      setSelectedPresets([])
      onCreated(task, startImmediately)
      onClose()
    } catch (err) {
      const msg = errorMessage(err, (m) => m.errors.createReview)(t)
      setError(msg)
      onNotice({ tone: 'error', text: errorMessage(err, (m) => m.errors.createReview) })
    } finally {
      setSubmitting(null)
    }
  }

  const presetLabel = (preset: (typeof FOCUS_PRESETS)[number]) => {
    switch (preset) {
      case 'Security':
        return t.workbench.focusSecurity
      case 'Correctness':
        return t.workbench.focusCorrectness
      case 'Performance':
        return t.workbench.focusPerformance
      case 'Tests':
        return t.workbench.focusTests
    }
  }

  return (
    <div className="wb-backdrop" role="presentation" onClick={() => !submitting && onClose()}>
      <div
        className="wb-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="wb-new-review-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="wb-modal-header">
          <div>
            <h2 id="wb-new-review-title">{t.newReview.title}</h2>
            <p className="wb-modal-lede">{t.workbench.modalLede}</p>
          </div>
          <button type="button" className="wb-icon-btn" onClick={onClose} aria-label={t.common.cancel}>
            <CloseIcon />
          </button>
        </div>

        <div className="wb-modal-body">
          {error && <div className="wb-alert wb-alert-error">{error}</div>}

          <div className="wb-field-group">
            <span className="wb-cap">{t.newReview.source}</span>
            <div className="wb-segmented wb-segmented-full">
              <button
                type="button"
                className={sourceType === 'local_diff' ? 'active' : ''}
                onClick={() => setSourceType('local_diff')}
              >
                {t.newReview.localChanges}
              </button>
              <button
                type="button"
                className={sourceType === 'pull_request' ? 'active' : ''}
                onClick={() => setSourceType('pull_request')}
              >
                {t.newReview.pullRequest}
              </button>
            </div>
          </div>

          <div className="wb-field-group">
            <label className="wb-label" htmlFor="wb-repo-input">
              {t.newReview.repoPath}
            </label>
            <div className="wb-input-combo">
              <input
                id="wb-repo-input"
                type="text"
                className="wb-input wb-mono"
                value={repoPath}
                onChange={(e) => setRepoPath(e.target.value)}
                placeholder="/path/to/repository"
                autoFocus
              />
              {recentRepos.length > 0 && (
                <select
                  aria-label={t.workbench.recentRepos}
                  className="wb-recent-select"
                  value=""
                  onChange={(e) => {
                    if (e.target.value) setRepoPath(e.target.value)
                  }}
                >
                  <option value="">{t.workbench.recentRepos}</option>
                  {recentRepos.map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </select>
              )}
            </div>
            {repoPath.trim() && repoStatus && (
              <div className={`wb-repo-status ${repoStatus.is_git_repo ? 'ok' : 'warn'}`}>
                {repoStatus.is_git_repo ? (
                  <>
                    <span className="wb-status-badge-icon ok"><CheckIcon size={11} /></span>
                    <span>
                      {t.workbench.repoStatusGit(repoStatus.branch || 'HEAD', repoStatus.changed_files)}
                    </span>
                  </>
                ) : (
                  <>
                    <WarningIcon size={13} />
                    <span>{repoStatus.exists ? t.workbench.repoStatusNotGit : t.workbench.repoStatusMissing}</span>
                  </>
                )}
              </div>
            )}
          </div>

          {sourceType === 'pull_request' && (
            <div className="wb-field-group">
              <label className="wb-label" htmlFor="wb-pr-url-input">
                {t.newReview.prUrl}
              </label>
              <input
                id="wb-pr-url-input"
                type="url"
                className="wb-input wb-mono"
                value={prUrl}
                onChange={(e) => setPrUrl(e.target.value)}
                placeholder="https://github.com/owner/repo/pull/123"
              />
              {repoStatus && (
                <div className={`wb-repo-status ${repoStatus.gh_authenticated ? 'ok' : 'warn'}`}>
                  {repoStatus.gh_authenticated ? (
                    <>
                      <span className="wb-status-badge-icon ok"><CheckIcon size={11} /></span>
                      <span>{t.workbench.ghAuthOk}</span>
                    </>
                  ) : (
                    <>
                      <InfoIcon size={13} />
                      <span>{t.workbench.ghAuthMissing}</span>
                    </>
                  )}
                </div>
              )}
            </div>
          )}

          <div className="wb-grid-2">
            {sourceType === 'local_diff' ? (
              <div className="wb-field-group">
                <label className="wb-label" htmlFor="wb-base-input">
                  {t.newReview.baseBranch}
                </label>
                <input
                  id="wb-base-input"
                  type="text"
                  className="wb-input"
                  value={baseBranch}
                  onChange={(e) => setBaseBranch(e.target.value)}
                  placeholder={repoStatus?.branch ? `${repoStatus.branch} (detected)` : t.newReview.baseBranchPlaceholder}
                />
              </div>
            ) : (
              <div className="wb-field-group" />
            )}

            <div className="wb-field-group">
              <label className="wb-label" htmlFor="wb-lang-select">
                {t.newReview.language}
              </label>
              <select
                id="wb-lang-select"
                className="wb-select"
                value={language}
                onChange={(e) => setLanguage(e.target.value as ReviewLanguage)}
              >
                <option value="en">{t.detail.reviewLangEn}</option>
                <option value="zh">{t.detail.reviewLangZh}</option>
              </select>
            </div>
          </div>

          <div className={`wb-agent-row ${reasoningOpts.supported ? 'cols-3' : 'cols-2'}`}>
            <div className="wb-field-group">
              <label className="wb-cap" htmlFor="wb-agent-select">{t.newReview.agent}</label>
              <select
                id="wb-agent-select"
                className="wb-select"
                value={agent}
                onChange={(e) => handleAgentChange(e.target.value)}
              >
                <option value="">{t.newReview.agentAuto}</option>
                {backends.map((b) => (
                  <option key={b.name} value={b.name}>
                    {b.name}{b.version ? ` (${b.version})` : ''}
                  </option>
                ))}
              </select>
            </div>

            <div className="wb-field-group">
              <label className="wb-cap" htmlFor="wb-model-select">{t.newReview.model}</label>
              <select
                id="wb-model-select"
                className="wb-select"
                value={model}
                onChange={(e) => handleModelChange(e.target.value)}
                disabled={!agent}
              >
                <option value="">{t.settings.agentDefault}</option>
                {(activeCap?.models ?? []).map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
                {model && !(activeCap?.models ?? []).includes(model) && (
                  <option value={model}>{model}</option>
                )}
              </select>
            </div>

            {reasoningOpts.supported && (
              <div className="wb-field-group">
                <label className="wb-cap" htmlFor="wb-reasoning-select">{t.newReview.reasoning}</label>
                <select
                  id="wb-reasoning-select"
                  className="wb-select"
                  value={reasoning}
                  onChange={(e) => setReasoning(e.target.value)}
                >
                  <option value="">
                    {reasoningOpts.defaultLevel
                      ? t.settings.modelDefaultWithLevel(reasoningOpts.defaultLevel)
                      : t.settings.modelDefault}
                  </option>
                  {reasoningOpts.levels.map((lvl) => (
                    <option key={lvl} value={lvl}>
                      {lvl}
                    </option>
                  ))}
                </select>
              </div>
            )}
          </div>
          {reasoningOpts.supported && (
            <p className="wb-micro-hint">{t.workbench.higherIsSlower}</p>
          )}

          <div className="wb-field-group">
            <span className="wb-label">{t.newReview.focus}</span>
            <div className="wb-chip-row">
              {FOCUS_PRESETS.map((preset) => (
                <button
                  key={preset}
                  type="button"
                  className={`wb-focus-chip ${selectedPresets.includes(preset) ? 'active' : ''}`}
                  onClick={() => togglePreset(preset)}
                >
                  {presetLabel(preset)}
                </button>
              ))}
            </div>
            <input
              type="text"
              className="wb-input"
              value={customFocus}
              onChange={(e) => setCustomFocus(e.target.value)}
              placeholder={t.newReview.focusPlaceholder}
            />
          </div>

          {sourceType === 'local_diff' && (
            <div className="wb-info-box">
              <InfoIcon size={15} />
              <span>{t.workbench.snapshotNote}</span>
            </div>
          )}
        </div>

        <div className="wb-modal-footer">
          <button type="button" className="wb-btn wb-btn-ghost" onClick={onClose} disabled={!!submitting}>
            {t.common.cancel}
          </button>
          <div className="wb-modal-footer-actions">
            <button
              type="button"
              className="wb-btn wb-btn-secondary"
              disabled={!!submitting}
              onClick={() => void submit(false)}
            >
              {submitting === 'create' ? t.newReview.creating : t.workbench.createOnly}
            </button>
            <button
              type="button"
              className="wb-btn wb-btn-primary"
              disabled={!!submitting}
              onClick={() => void submit(true)}
            >
              {submitting === 'start' ? t.newReview.creating : t.workbench.createAndStart}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
