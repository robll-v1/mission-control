import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { labelFor, type Messages } from '../i18n'
import {
  buildSaveConfigPayload,
  resolveReasoningOptions,
  savedForAgent,
  type BackendCapability,
  type CheckAgentResponse,
  type EffectiveSettingsResponse,
  type SavedConfig,
} from '../runSettings'
import { ChevronLeftIcon, InfoIcon, WarningIcon } from './Icons'
import type { StorageInfo } from './types'

type Section = 'model' | 'storage' | 'about'

type Props = {
  repoPaths: string[]
  t: Messages
  onBack: () => void
  onSaved?: (config: SavedConfig) => void
}

const KNOWN_AGENTS: { name: string; display: string; install: string }[] = [
  { name: 'codex', display: 'Codex CLI', install: 'npm install -g @openai/codex' },
  { name: 'claude-code', display: 'Claude Code', install: 'npm install -g @anthropic-ai/claude-code' },
  { name: 'opencode', display: 'OpenCode', install: 'npm install -g opencode-ai' },
  { name: 'copilot', display: 'Copilot CLI', install: 'gh extension install github/gh-copilot' },
]

function displayNameFor(agent: string): string {
  const found = KNOWN_AGENTS.find((a) => a.name === agent)
  return found ? found.display : agent
}

function modelSourceText(t: Messages, source?: BackendCapability['models_source'], count = 0): string {
  switch (source) {
    case 'cli':
      return t.settings.modelSource.cli
    case 'catalog_cache':
      return t.settings.modelSource.catalog_cache
    case 'cache':
      return t.settings.modelSource.cache
    case 'config':
      return t.settings.modelSource.config
    default:
      return count > 0 ? t.settings.modelSource.unknown : t.settings.modelSource.unreadable
  }
}

function reasoningSourceText(t: Messages, source?: string): string {
  switch (source) {
    case 'cache':
      return t.settings.reasoningSource.cache
    case 'cli_help':
      return t.settings.reasoningSource.cli_help
    case 'config':
      return t.settings.reasoningSource.config
    case 'unsupported':
      return t.settings.reasoningSource.unsupported
    default:
      return t.settings.reasoningSource.unknown
  }
}

export function WorkbenchSettings({ repoPaths, t, onBack, onSaved }: Props) {
  const [section, setSection] = useState<Section>('model')
  const [backends, setBackends] = useState<BackendCapability[]>([])
  const [saved, setSaved] = useState<SavedConfig | null>(null)
  const [effective, setEffective] = useState<EffectiveSettingsResponse | null>(null)
  const [storage, setStorage] = useState<StorageInfo | null>(null)
  const [selectedRepo, setSelectedRepo] = useState(repoPaths[0] ?? '')
  const [defaultAgent, setDefaultAgent] = useState('')
  const [draftByAgent, setDraftByAgent] = useState<Record<string, { model: string; reasoning: string }>>({})
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [checkingAgent, setCheckingAgent] = useState<string | null>(null)
  const [checkResults, setCheckResults] = useState<Record<string, CheckAgentResponse>>({})
  const [banner, setBanner] = useState<{ tone: 'info' | 'error'; text: string } | null>(null)
  const checkSeqRef = useRef(0)

  const loadAll = useCallback(async () => {
    setLoading(true)
    setLoadError(null)
    try {
      const [detectRes, currentRes] = await Promise.all([
        fetch('/api/config/detect'),
        fetch('/api/config/current'),
      ])
      if (!detectRes.ok || !currentRes.ok) {
        setLoadError(t.settings.loadFailed)
        return
      }
      const detectData = (await detectRes.json()) as { backends: BackendCapability[] }
      const currentData = (await currentRes.json()) as { config: SavedConfig }
      const detected = detectData.backends ?? []
      setBackends(detected)
      setSaved(currentData.config)
      const initialDefault = currentData.config.backend || detected[0]?.name || ''
      setDefaultAgent(initialDefault)
      const nextDraft: Record<string, { model: string; reasoning: string }> = {}
      for (const b of detected) {
        const s = savedForAgent(currentData.config, b.name)
        nextDraft[b.name] = { model: s.model, reasoning: s.reasoning }
      }
      setDraftByAgent(nextDraft)
    } catch {
      setLoadError(t.settings.loadFailed)
    } finally {
      setLoading(false)
    }
  }, [t.settings.loadFailed])

  useEffect(() => {
    void loadAll()
  }, [loadAll])

  useEffect(() => {
    let cancelled = false
    fetch('/api/config/storage')
      .then(async (res) => {
        if (!res.ok || cancelled) return
        setStorage((await res.json()) as StorageInfo)
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    if (loadError) return
    let cancelled = false
    const params = new URLSearchParams()
    if (selectedRepo) params.set('repo_path', selectedRepo)
    fetch(`/api/config/effective?${params.toString()}`)
      .then(async (res) => {
        if (!res.ok || cancelled) return
        setEffective((await res.json()) as EffectiveSettingsResponse)
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [selectedRepo, saved, loadError])

  const detectedByName = useMemo(() => {
    const map = new Map<string, BackendCapability>()
    for (const b of backends) map.set(b.name, b)
    return map
  }, [backends])

  const cardRows = useMemo(() => {
    const seen = new Set<string>()
    const rows: { name: string; cap: BackendCapability | undefined; install?: string }[] = []
    for (const b of backends) {
      seen.add(b.name)
      const meta = KNOWN_AGENTS.find((a) => a.name === b.name)
      rows.push({ name: b.name, cap: b, install: meta?.install })
    }
    for (const known of KNOWN_AGENTS) {
      if (!seen.has(known.name)) {
        rows.push({ name: known.name, cap: undefined, install: known.install })
      }
    }
    return rows
  }, [backends])

  const savedBackendMissing = Boolean(
    saved?.backend && backends.length > 0 && !detectedByName.has(saved.backend),
  )
  const selectedIsInstalled = Boolean(defaultAgent && detectedByName.has(defaultAgent))
  const activeDraft = draftByAgent[defaultAgent] ?? { model: '', reasoning: '' }
  const savedDefaultDraft = useMemo(() => savedForAgent(saved, defaultAgent), [saved, defaultAgent])

  const isDirty = useMemo(() => {
    if (!saved || !selectedIsInstalled) return false
    if (defaultAgent !== (saved.backend || '')) return true
    if (activeDraft.model !== savedDefaultDraft.model) return true
    const cap = detectedByName.get(defaultAgent)
    const opts = resolveReasoningOptions(cap, activeDraft.model)
    if (opts.supported && activeDraft.reasoning !== savedDefaultDraft.reasoning) return true
    return false
  }, [saved, selectedIsInstalled, defaultAgent, activeDraft, savedDefaultDraft, detectedByName])

  const updateAgentDraft = (agentName: string, patch: Partial<{ model: string; reasoning: string }>) => {
    setDraftByAgent((prev) => {
      const curr = prev[agentName] ?? { model: '', reasoning: '' }
      const next = { ...curr, ...patch }
      if (patch.model !== undefined) {
        const cap = detectedByName.get(agentName)
        const opts = resolveReasoningOptions(cap, patch.model)
        if (next.reasoning && (!opts.supported || (opts.levels.length > 0 && !opts.levels.includes(next.reasoning)))) {
          next.reasoning = ''
        }
      }
      return { ...prev, [agentName]: next }
    })
  }

  const handleDiscard = () => {
    if (!saved) return
    setDefaultAgent(saved.backend || backends[0]?.name || '')
    const reset: Record<string, { model: string; reasoning: string }> = {}
    for (const b of backends) {
      const s = savedForAgent(saved, b.name)
      reset[b.name] = { model: s.model, reasoning: s.reasoning }
    }
    setDraftByAgent(reset)
    setBanner(null)
  }

  const handleSave = async () => {
    if (!selectedIsInstalled || saving) return
    setSaving(true)
    setBanner(null)
    try {
      const cap = detectedByName.get(defaultAgent)
      const opts = resolveReasoningOptions(cap, activeDraft.model)
      const payload = buildSaveConfigPayload(
        defaultAgent,
        activeDraft.model,
        activeDraft.reasoning,
        opts.supported,
      )
      const res = await fetch('/api/config/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (!res.ok) {
        const err = (await res.json().catch(() => ({}))) as { detail?: string }
        setBanner({ tone: 'error', text: err.detail || t.settings.saveFailed })
        return
      }
      const curRes = await fetch('/api/config/current')
      if (curRes.ok) {
        const curData = (await curRes.json()) as { config: SavedConfig }
        setSaved(curData.config)
        onSaved?.(curData.config)
      }
      const modelLabel = activeDraft.model || t.settings.agentDefault
      const suffix = opts.supported && activeDraft.reasoning ? `${modelLabel} · ${activeDraft.reasoning}` : modelLabel
      setBanner({
        tone: 'info',
        text: t.settings.saved(displayNameFor(defaultAgent), suffix),
      })
    } catch {
      setBanner({ tone: 'error', text: t.settings.saveFailed })
    } finally {
      setSaving(false)
    }
  }

  const handleCheckAgent = async (cap: BackendCapability) => {
    const seq = ++checkSeqRef.current
    setCheckingAgent(cap.name)
    const draft = draftByAgent[cap.name] ?? { model: '', reasoning: '' }
    const opts = resolveReasoningOptions(cap, draft.model)
    try {
      const res = await fetch('/api/config/check-agent', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          backend: cap.name,
          model: draft.model || null,
          reasoning: opts.supported ? draft.reasoning || null : null,
        }),
      })
      if (checkSeqRef.current !== seq) return
      if (res.ok) {
        const data = (await res.json()) as CheckAgentResponse
        setCheckResults((prev) => ({ ...prev, [cap.name]: data }))
      } else {
        setBanner({ tone: 'error', text: t.settings.checkError })
      }
    } catch {
      if (checkSeqRef.current === seq) {
        setBanner({ tone: 'error', text: t.settings.checkError })
      }
    } finally {
      if (checkSeqRef.current === seq) setCheckingAgent(null)
    }
  }

  const effectiveAgent = effective?.settings.agent || effective?.backend || ''
  const effectiveModel = effective?.settings.model ?? effective?.model ?? null
  const effectiveReasoning = effective?.settings.reasoning ?? effective?.reasoning ?? null
  const effectiveSources = effective?.sources ?? effective?.settings.sources ?? {}

  return (
    <div className="wb-settings-shell">
      <aside className="wb-settings-nav">
        <button type="button" className="wb-back-link" onClick={onBack}>
          <ChevronLeftIcon size={14} />
          <span>{t.settings.backToReviews}</span>
        </button>

        <div className="wb-cap wb-settings-nav-label">{t.settings.eyebrow}</div>
        <nav className="wb-settings-links" aria-label={t.settings.eyebrow}>
          <button
            type="button"
            className={section === 'model' ? 'active' : ''}
            onClick={() => setSection('model')}
          >
            {t.workbench.settingsNavModel}
          </button>
          <button
            type="button"
            className={section === 'storage' ? 'active' : ''}
            onClick={() => setSection('storage')}
          >
            {t.workbench.settingsNavStorage}
          </button>
          <button
            type="button"
            className={section === 'about' ? 'active' : ''}
            onClick={() => setSection('about')}
          >
            {t.workbench.settingsNavAbout}
          </button>
        </nav>

        <div className="wb-settings-nav-footer">
          <span className="wb-xs wb-t3">
            <span className="wb-mono">{storage?.config_path || '~/.config/amc/config.yaml'}</span>
          </span>
        </div>
      </aside>

      <div className="wb-settings-main">
        <div className="wb-settings-content">
          {banner && (
            <div role={banner.tone === 'error' ? 'alert' : 'status'} className={`wb-alert ${banner.tone === 'error' ? 'wb-alert-error' : 'wb-alert-info'}`}>
              {banner.text}
            </div>
          )}

          {section === 'model' && (
            <>
              <div className="wb-settings-heading">
                <h1>{t.settings.title}</h1>
                <p className="wb-t2">
                  {t.settings.ledeBeforeFile}
                  <code>.amc.yaml</code>
                  {t.settings.ledeAfterFile}
                </p>
              </div>

              {loadError ? (
                <div role="alert" className="wb-alert wb-alert-error">
                  <WarningIcon size={15} />
                  <span>{loadError}</span>
                </div>
              ) : loading ? (
                <div className="wb-card wb-muted-card">{t.settings.detecting}</div>
              ) : backends.length === 0 ? (
                <div className="wb-card wb-empty-agents-card">
                  <h3>{t.workbench.noAgentsTitle}</h3>
                  <p className="wb-t2">{t.workbench.noAgentsBody}</p>
                  <div className="wb-install-list">
                    {KNOWN_AGENTS.map((a) => (
                      <div key={a.name} className="wb-install-row">
                        <span className="wb-b">{a.display}</span>
                        <code className="wb-mono">{a.install}</code>
                      </div>
                    ))}
                  </div>
                </div>
              ) : (
                <>
                  {savedBackendMissing && saved?.backend && (
                    <div role="alert" className="wb-alert wb-alert-warn">
                      <WarningIcon size={15} />
                      <span>{t.settings.savedAgentMissing(displayNameFor(saved.backend))}</span>
                    </div>
                  )}

                  <div className="wb-section-bar">
                    <span className="wb-cap">{t.workbench.agentsOnMachine}</span>
                    <button type="button" className="wb-text-btn" onClick={() => void loadAll()} disabled={loading}>
                      {t.workbench.detectAgain}
                    </button>
                  </div>

                  <div className="wb-agent-grid">
                    {cardRows.map(({ name, cap, install }) => {
                      const available = Boolean(cap)
                      const isDefault = defaultAgent === name
                      const isSavedCurrent = saved?.backend === name
                      const draft = draftByAgent[name] ?? { model: '', reasoning: '' }
                      const opts = resolveReasoningOptions(cap, draft.model)
                      const check = checkResults[name]
                      const customSavedModel = Boolean(draft.model && cap && !cap.models.includes(draft.model))
                      return (
                        <div
                          key={name}
                          className={`wb-agent-card ${isDefault ? 'selected' : ''} ${!available ? 'uninstalled' : ''}`}
                        >
                          <div className="wb-agent-card-top">
                            <label className="wb-agent-radio">
                              <input
                                type="radio"
                                name="default-agent"
                                checked={isDefault}
                                disabled={!available}
                                onChange={() => setDefaultAgent(name)}
                              />
                              <span className="wb-agent-name">{displayNameFor(name)}</span>
                              {isSavedCurrent && (
                                <span className="wb-pill wb-pill-accent">{t.settings.current}</span>
                              )}
                              {isDefault && !isSavedCurrent && (
                                <span className="wb-pill wb-pill-warn">{t.workbench.unsaved}</span>
                              )}
                            </label>
                            <span className="wb-agent-installed">
                              <span className={`wb-dot ${available ? 'ok' : 'muted'}`} />
                              {available ? t.workbench.installed : t.workbench.notInstalled}
                            </span>
                          </div>
                          <div className="wb-mono wb-xs wb-t3 wb-agent-version">
                            {cap ? (cap.version || t.settings.versionUnknown) : (install || '')}
                          </div>

                          <div className="wb-agent-controls">
                            <div className="wb-agent-control">
                              <span className="wb-cap">{t.newReview.model}</span>
                              <select
                                aria-label={`${name} ${t.newReview.model}`}
                                className="wb-select wb-select-sm"
                                value={draft.model}
                                disabled={!available}
                                onChange={(e) => {
                                  setDefaultAgent(name)
                                  updateAgentDraft(name, { model: e.target.value })
                                }}
                              >
                                <option value="">{t.settings.agentDefault}</option>
                                {(cap?.models ?? []).map((m) => (
                                  <option key={m} value={m}>
                                    {m}
                                  </option>
                                ))}
                                {customSavedModel && (
                                  <option value={draft.model}>
                                    {draft.model} ({t.settings.customModelHint})
                                  </option>
                                )}
                              </select>
                            </div>

                            <div className="wb-agent-control">
                              <span className="wb-cap">{t.newReview.reasoning}</span>
                              {available && opts.supported ? (
                                <select
                                  aria-label={`${name} ${t.newReview.reasoning}`}
                                  className="wb-select wb-select-sm"
                                  value={draft.reasoning}
                                  onChange={(e) => {
                                    setDefaultAgent(name)
                                    updateAgentDraft(name, { reasoning: e.target.value })
                                  }}
                                >
                                  <option value="">
                                    {opts.defaultLevel
                                      ? t.settings.modelDefaultWithLevel(opts.defaultLevel)
                                      : t.settings.modelDefault}
                                  </option>
                                  {opts.levels.map((lvl) => (
                                    <option key={lvl} value={lvl}>
                                      {lvl}
                                    </option>
                                  ))}
                                </select>
                              ) : (
                                <div className="wb-disabled-field">{t.workbench.notSupported}</div>
                              )}
                            </div>

                            <div className="wb-agent-control wb-agent-check-col">
                              <span className="wb-cap">&nbsp;</span>
                              <button
                                type="button"
                                className="wb-btn wb-btn-secondary wb-btn-sm"
                                disabled={!cap || checkingAgent === name}
                                onClick={() => cap && void handleCheckAgent(cap)}
                              >
                                {checkingAgent === name ? t.settings.checkingAgent : t.settings.checkAgent}
                              </button>
                            </div>
                          </div>

                          {cap && (
                            <div className="wb-xs wb-t3 wb-agent-foot">
                              <span>{modelSourceText(t, cap.models_source, cap.model_count)}</span>
                              {' · '}
                              <span>
                                {opts.supported && opts.levels.length > 0
                                  ? `${t.workbench.levelsCount(opts.levels.length, opts.defaultLevel)} · ${reasoningSourceText(t, cap.reasoning_source)}`
                                  : reasoningSourceText(t, cap.reasoning_source)}
                              </span>
                            </div>
                          )}

                          {check && (
                            <div className={`wb-check-result ${check.ok ? 'ok' : 'error'}`}>
                              <strong>{check.ok ? t.settings.checkPassed : t.settings.checkFailed}:</strong>{' '}
                              {check.message}
                            </div>
                          )}
                        </div>
                      )
                    })}
                  </div>

                  <div className="wb-info-strip">
                    <InfoIcon size={15} />
                    <span>{t.settings.notCallable}</span>
                  </div>

                  <div className="wb-section-bar wb-mt-lg">
                    <div className="wb-section-bar-left">
                      <span className="wb-cap">{t.workbench.effectiveSettings}</span>
                      {repoPaths.length > 0 && (
                        <select
                          aria-label={t.workbench.effectiveSettings}
                          className="wb-select wb-select-xs wb-mono"
                          value={selectedRepo}
                          onChange={(e) => setSelectedRepo(e.target.value)}
                        >
                          <option value="">(global)</option>
                          {repoPaths.map((p) => (
                            <option key={p} value={p}>
                              {p}
                            </option>
                          ))}
                        </select>
                      )}
                    </div>
                    <span className="wb-xs wb-t3">{t.workbench.effectiveHierarchy}</span>
                  </div>

                  <div className="wb-card wb-kv-table">
                    <div className="wb-kv-row wb-kv-row-3">
                      <span className="wb-t2">{t.detail.agent}</span>
                      <span className="wb-b">
                        {defaultAgent || effectiveAgent || '—'}
                        {defaultAgent !== (saved?.backend || '') && (
                          <span className="wb-pill wb-pill-warn wb-ml-sm">{t.workbench.unsaved}</span>
                        )}
                      </span>
                      <span className="wb-t2">
                        {effective ? labelFor(t.settings.sourceLayer, effectiveSources.agent || 'global') : '—'}
                      </span>
                    </div>
                    <div className="wb-kv-row wb-kv-row-3">
                      <span className="wb-t2">{t.detail.roundModel}</span>
                      <span className="wb-b">
                        {activeDraft.model || effectiveModel || t.settings.agentDefault}
                        {activeDraft.model !== savedDefaultDraft.model && (
                          <span className="wb-pill wb-pill-warn wb-ml-sm">{t.workbench.unsaved}</span>
                        )}
                      </span>
                      <span className="wb-t2">
                        {effective ? labelFor(t.settings.sourceLayer, effectiveSources.model || 'global') : '—'}
                      </span>
                    </div>
                    <div className="wb-kv-row wb-kv-row-3">
                      <span className="wb-t2">{t.detail.roundReasoning}</span>
                      <span className="wb-b">
                        {activeDraft.reasoning || effectiveReasoning || t.settings.modelDefault}
                        {activeDraft.reasoning !== savedDefaultDraft.reasoning && (
                          <span className="wb-pill wb-pill-warn wb-ml-sm">{t.workbench.unsaved}</span>
                        )}
                      </span>
                      <span className="wb-t2">
                        {effective ? labelFor(t.settings.sourceLayer, effectiveSources.reasoning || 'default') : '—'}
                      </span>
                    </div>
                    <div className="wb-kv-row wb-kv-row-3">
                      <span className="wb-t2">{t.workbench.outputLanguage}</span>
                      <span className="wb-b">English</span>
                      <span className="wb-t2">{t.workbench.builtInDefault}</span>
                    </div>
                    <div className="wb-kv-row wb-kv-row-3">
                      <span className="wb-t2">{t.workbench.idleTimeout}</span>
                      <span className="wb-b">180 s</span>
                      <span className="wb-t2">{t.workbench.builtInDefault}</span>
                    </div>
                  </div>
                </>
              )}
            </>
          )}

          {section === 'storage' && (
            <>
              <div className="wb-settings-heading">
                <h1>{t.workbench.storageTitle}</h1>
                <p className="wb-t2">{t.workbench.storageLede}</p>
              </div>
              <div className="wb-card wb-kv-table">
                <div className="wb-kv-row">
                  <span className="wb-t2">{t.workbench.storageDataDir}</span>
                  <span className="wb-mono">{storage?.data_dir || '—'}</span>
                </div>
                <div className="wb-kv-row">
                  <span className="wb-t2">{t.workbench.storageDbPath}</span>
                  <span className="wb-mono">{storage?.db_path || '—'}</span>
                </div>
                <div className="wb-kv-row">
                  <span className="wb-t2">{t.workbench.storageWorktrees}</span>
                  <span className="wb-mono">{storage?.worktrees_dir || '—'}</span>
                </div>
                <div className="wb-kv-row">
                  <span className="wb-t2">{t.workbench.storageConfigPath}</span>
                  <span className="wb-mono">{storage?.config_path || '—'}</span>
                </div>
                <div className="wb-kv-row">
                  <span className="wb-t2">{t.workbench.storageBundledUi}</span>
                  <span>{storage?.bundled_ui ? t.workbench.yes : t.workbench.no}</span>
                </div>
              </div>
            </>
          )}

          {section === 'about' && (
            <>
              <div className="wb-settings-heading">
                <h1>{t.workbench.aboutTitle}</h1>
                <p className="wb-t2">{t.workbench.aboutBody}</p>
              </div>
              <div className="wb-card wb-kv-table">
                <div className="wb-kv-row">
                  <span className="wb-t2">{t.workbench.aboutVersion}</span>
                  <span className="wb-mono">0.1.0</span>
                </div>
                <div className="wb-kv-row">
                  <span className="wb-t2">License</span>
                  <span>MIT</span>
                </div>
              </div>
            </>
          )}
        </div>

        {section === 'model' && !loadError && !loading && backends.length > 0 && isDirty && (
          <div className="wb-sticky-save-bar">
            <div className="wb-sticky-save-left">
              <span className="wb-dot warn" />
              <span className="wb-t2">{t.workbench.unsavedSummary(displayNameFor(defaultAgent))}</span>
            </div>
            <div className="wb-sticky-save-actions">
              <button type="button" className="wb-btn wb-btn-secondary" onClick={handleDiscard} disabled={saving}>
                {t.workbench.discard}
              </button>
              <button type="button" className="wb-btn wb-btn-primary" onClick={() => void handleSave()} disabled={saving}>
                {saving ? t.settings.saving : t.settings.save}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
