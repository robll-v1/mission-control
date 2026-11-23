import { useEffect, useMemo, useState } from 'react'
import { errorMessage, labelFor, type Localized, type Messages } from './i18n'

type Backend = {
  name: string
  version: string
  models: string[]
  command: string
  model_count: number
  grouped_models?: Record<string, string[]>
  models_source?: 'cli' | 'catalog_cache' | 'cache' | 'config' | 'unknown' | string
  models_note?: string | null
}

type DetectResponse = {
  backends: Backend[]
}

type SavedConfig = { backend: string; model: string | null; variant: string | null }

type CurrentConfig = {
  config: SavedConfig
}

// Text is resolved when rendered, so a banner on screen follows a language
// switch. It stays inside an object: React calls a bare function passed to a
// state setter as an updater.
type Status = { tone: 'info' | 'error'; text: Localized } | null

const LOAD_FAILED: Localized = (t) => t.settings.loadFailed
const SAVE_FAILED: Localized = (t) => t.settings.saveFailed

const DISPLAY_NAMES: Record<string, string> = {
  opencode: 'OpenCode',
  'claude-code': 'Claude Code',
  codex: 'Codex CLI',
  copilot: 'Copilot CLI',
}

const INSTALL_HINTS: { name: string; command: string }[] = [
  { name: 'OpenCode', command: 'npm install -g opencode-ai' },
  { name: 'Claude Code', command: 'npm install -g @anthropic-ai/claude-code' },
  { name: 'Codex CLI', command: 'npm install -g @openai/codex' },
  { name: 'Copilot CLI', command: 'gh extension install github/gh-copilot' },
]

const SEARCH_THRESHOLD = 8

function displayName(name: string) {
  return labelFor(DISPLAY_NAMES, name)
}

function modelSourceDescription(t: Messages, source: Backend['models_source'], modelCount: number) {
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
      return modelCount > 0 ? t.settings.modelSource.unknown : t.settings.modelSource.unreadable
  }
}

function CheckIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="M3.5 8.5l3 3 6-7" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function BackIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="M10 3.5L5.5 8l4.5 4.5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

export function Settings({ onBack, t }: { onBack: () => void; t: Messages }) {
  const [backends, setBackends] = useState<Backend[]>([])
  const [saved, setSaved] = useState<SavedConfig | null>(null)
  const [selectedBackend, setSelectedBackend] = useState('')
  const [selectedModel, setSelectedModel] = useState('')
  const [modelSearch, setModelSearch] = useState('')
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<Status>(null)
  const [saving, setSaving] = useState(false)
  const [status, setStatus] = useState<Status>(null)

  useEffect(() => {
    Promise.all([fetch('/api/config/detect'), fetch('/api/config/current')])
      .then(async ([detectRes, currentRes]) => {
        if (!detectRes.ok || !currentRes.ok) {
          setLoadError({ tone: 'error', text: LOAD_FAILED })
          return
        }
        const [data, current]: [DetectResponse, CurrentConfig] = await Promise.all([detectRes.json(), currentRes.json()])
        setBackends(data.backends)
        setSaved(current.config)
        setSelectedBackend(current.config.backend || data.backends[0]?.name || '')
        setSelectedModel(current.config.model || '')
      })
      .catch((err) => setLoadError({ tone: 'error', text: errorMessage(err, LOAD_FAILED) }))
      .finally(() => setLoading(false))
  }, [])

  const currentBackend = backends.find((b) => b.name === selectedBackend)
  const query = modelSearch.trim().toLowerCase()
  const customModel = Boolean(selectedModel && currentBackend && !currentBackend.models.includes(selectedModel))
  const dirty = !saved || saved.backend !== selectedBackend || (saved.model || '') !== selectedModel

  const modelGroups = useMemo(() => {
    if (!currentBackend) return []
    const grouped = currentBackend.grouped_models
    if (!query && grouped && Object.keys(grouped).length > 0) return Object.entries(grouped)
    const matches = currentBackend.models.filter((model) => model.toLowerCase().includes(query))
    return matches.length ? [['', matches] as [string, string[]]] : []
  }, [currentBackend, query])

  const chooseBackend = (name: string) => {
    setSelectedBackend(name)
    setModelSearch('')
    // Coming back to the saved backend restores its saved model instead of wiping it.
    setSelectedModel(saved?.backend === name ? saved.model || '' : '')
    setStatus(null)
  }

  const handleSave = async () => {
    setSaving(true)
    setStatus(null)
    try {
      const res = await fetch('/api/config/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ backend: selectedBackend, model: selectedModel }),
      })
      if (!res.ok) {
        setStatus({ tone: 'error', text: SAVE_FAILED })
        return
      }
      setSaved({ backend: selectedBackend, model: selectedModel || null, variant: saved?.variant ?? null })
      setStatus({
        tone: 'info',
        text: (t) => t.settings.saved(displayName(selectedBackend), selectedModel || t.settings.agentDefault),
      })
    } catch (err) {
      setStatus({ tone: 'error', text: errorMessage(err, SAVE_FAILED) })
    } finally {
      setSaving(false)
    }
  }

  const header = (
    <div className="detail-head">
      <div className="detail-eyebrow">
        <button className="link-back" onClick={onBack}>
          <BackIcon />
          {t.settings.backToReviews}
        </button>
        <span aria-hidden="true">·</span>
        <span>{t.settings.eyebrow}</span>
      </div>
      <h1 className="detail-title">{t.settings.title}</h1>
      <p className="settings-lede">
        {t.settings.ledeBeforeFile}
        <code>.amc.yaml</code>
        {t.settings.ledeAfterFile}
      </p>
    </div>
  )

  if (loading) {
    return (
      <main className="main settings-main">
        <div className="main-inner settings-inner">
          {header}
          <div className="section-label">
            <span>{t.settings.agentHeading}</span>
            <span className="count">{t.settings.detecting}</span>
          </div>
          <div className="backend-grid" aria-busy="true">
            {[0, 1].map((i) => (
              <div key={i} className="backend-option is-skeleton">
                <span className="skeleton-line w-40" />
                <span className="skeleton-line w-70" />
              </div>
            ))}
          </div>
        </div>
      </main>
    )
  }

  if (loadError) {
    return (
      <main className="main settings-main">
        <div className="main-inner settings-inner">
          {header}
          <div className="banner is-error" role="alert">{loadError.text(t)}</div>
        </div>
      </main>
    )
  }

  if (backends.length === 0) {
    return (
      <main className="main settings-main">
        <div className="main-inner settings-inner">
          {header}
          <section className="settings-card install-card">
            <h2>{t.settings.noneDetected}</h2>
            <p>
              {t.settings.installBeforePath}
              <code>PATH</code>
              {t.settings.installAfterPath}
            </p>
            <ul className="install-list">
              {INSTALL_HINTS.map((hint) => (
                <li key={hint.name}>
                  <span className="install-name">{hint.name}</span>
                  <code>{hint.command}</code>
                </li>
              ))}
            </ul>
          </section>
        </div>
      </main>
    )
  }

  return (
    <main className="main settings-main">
      <div className="main-inner settings-inner">
        {header}

        {status && (
          <div className={`banner ${status.tone === 'error' ? 'is-error' : 'is-info'}`} role="status">
            <span>{status.text(t)}</span>
            <button onClick={() => setStatus(null)} aria-label={t.common.dismiss}>✕</button>
          </div>
        )}

        <section aria-labelledby="backend-heading">
          <div className="section-label">
            <span id="backend-heading">{t.settings.agentHeading}</span>
            <span className="count">{t.settings.detected(backends.length)}</span>
          </div>
          <div className="backend-grid" role="radiogroup" aria-labelledby="backend-heading">
            {backends.map((backend) => {
              const active = selectedBackend === backend.name
              return (
                <label
                  key={backend.name}
                  className={`backend-option ${active ? 'selected' : ''}`}
                  title={t.settings.command(backend.command)}
                >
                  <input
                    type="radio"
                    name="backend"
                    value={backend.name}
                    checked={active}
                    onChange={() => chooseBackend(backend.name)}
                  />
                  <span className="backend-top">
                    <span className="backend-title">{displayName(backend.name)}</span>
                    {saved?.backend === backend.name && <span className="pill">{t.settings.current}</span>}
                    <span className="backend-check" aria-hidden="true">{active && <CheckIcon />}</span>
                  </span>
                  <span className="backend-meta">
                    {backend.model_count ? t.settings.modelsFound(backend.model_count) : t.settings.usesAgentDefault}
                  </span>
                  <span className="backend-version mono">{backend.version || t.settings.versionUnknown}</span>
                </label>
              )
            })}
          </div>
        </section>

        {selectedBackend && !currentBackend && (
          <div className="banner is-error" role="alert">
            {t.settings.savedAgentMissing(displayName(selectedBackend))}
          </div>
        )}

        {currentBackend && (
          <section aria-labelledby="model-heading">
            <div className="section-label">
              <span id="model-heading">{t.settings.modelHeading}</span>
              {currentBackend.model_count > currentBackend.models.length && (
                <span className="count">
                  {t.settings.showingModels(currentBackend.models.length, currentBackend.model_count)}
                </span>
              )}
            </div>

            <div className="settings-card model-card">
              <p className="model-hint">
                {modelSourceDescription(t, currentBackend.models_source, currentBackend.models.length)} {currentBackend.models_note}
                {' '}{t.settings.notCallable}
              </p>
              {currentBackend.models.length > SEARCH_THRESHOLD && (
                <div className="model-search">
                  <input
                    type="search"
                    placeholder={t.settings.searchModels}
                    value={modelSearch}
                    onChange={(e) => setModelSearch(e.target.value)}
                    aria-label={t.settings.searchModels}
                  />
                </div>
              )}

              <div className="model-list" role="radiogroup" aria-labelledby="model-heading">
                {!query && (
                  <ModelOption
                    value=""
                    label={t.settings.agentDefault}
                    hint={currentBackend.models.length ? t.settings.agentDefaultHint : t.settings.noModelList}
                    checked={selectedModel === ''}
                    onSelect={setSelectedModel}
                  />
                )}
                {customModel && (
                  <ModelOption
                    value={selectedModel}
                    label={selectedModel}
                    hint={t.settings.customModelHint}
                    checked
                    onSelect={setSelectedModel}
                  />
                )}
                {modelGroups.map(([group, models]) => (
                  <div key={group || 'all'} role="group" aria-label={group || undefined}>
                    {group && (
                      <div className="model-group">
                        <span>{group}</span>
                        <span className="count">{models.length}</span>
                      </div>
                    )}
                    {models.map((model) => (
                      <ModelOption
                        key={model}
                        value={model}
                        label={model}
                        checked={selectedModel === model}
                        onSelect={setSelectedModel}
                      />
                    ))}
                  </div>
                ))}
                {query && modelGroups.length === 0 && (
                  <div className="model-empty">{t.settings.noMatch(modelSearch.trim())}</div>
                )}
              </div>
            </div>
          </section>
        )}

        <div className="settings-savebar">
          <div className="savebar-summary">
            <span className="meta-key">{dirty ? t.settings.willSave : t.settings.currentConfig}</span>
            <span className="savebar-value">
              {displayName(selectedBackend)}
              <span aria-hidden="true"> · </span>
              <span className="mono">{selectedModel || t.settings.agentDefault}</span>
            </span>
          </div>
          <button className="btn btn-ghost" onClick={onBack}>
            {t.common.back}
          </button>
          <button
            className="btn btn-primary"
            onClick={handleSave}
            disabled={saving || !currentBackend || !dirty}
          >
            {saving ? t.settings.saving : t.settings.save}
          </button>
        </div>
      </div>
    </main>
  )
}

function ModelOption({
  value,
  label,
  hint,
  checked,
  onSelect,
}: {
  value: string
  label: string
  hint?: string
  checked: boolean
  onSelect: (value: string) => void
}) {
  return (
    <label className={`model-option ${checked ? 'selected' : ''}`}>
      <input type="radio" name="model" value={value} checked={checked} onChange={() => onSelect(value)} />
      <span className="model-text">
        <span className={value ? 'model-name mono' : 'model-name'}>{label}</span>
        {hint && <span className="model-hint">{hint}</span>}
      </span>
      <span className="model-check" aria-hidden="true">{checked && <CheckIcon />}</span>
    </label>
  )
}
