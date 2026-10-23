import { useEffect, useMemo, useState } from 'react'

type Backend = {
  name: string
  version: string
  models: string[]
  command: string
  model_count: number
  grouped_models?: Record<string, string[]>
}

type DetectResponse = {
  backends: Backend[]
}

type SavedConfig = { backend: string; model: string | null; variant: string | null }

type CurrentConfig = {
  config: SavedConfig
}

type Status = { tone: 'info' | 'error'; text: string } | null

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
  return DISPLAY_NAMES[name] ?? name
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

export function Settings({ onBack }: { onBack: () => void }) {
  const [backends, setBackends] = useState<Backend[]>([])
  const [saved, setSaved] = useState<SavedConfig | null>(null)
  const [selectedBackend, setSelectedBackend] = useState('')
  const [selectedModel, setSelectedModel] = useState('')
  const [modelSearch, setModelSearch] = useState('')
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [saving, setSaving] = useState(false)
  const [status, setStatus] = useState<Status>(null)

  useEffect(() => {
    Promise.all([fetch('/api/config/detect'), fetch('/api/config/current')])
      .then(async ([detectRes, currentRes]) => {
        if (!detectRes.ok || !currentRes.ok) throw new Error('无法读取配置，请确认后端服务正在运行')
        const [data, current]: [DetectResponse, CurrentConfig] = await Promise.all([detectRes.json(), currentRes.json()])
        setBackends(data.backends)
        setSaved(current.config)
        setSelectedBackend(current.config.backend || data.backends[0]?.name || '')
        setSelectedModel(current.config.model || '')
      })
      .catch((err) => setLoadError(err instanceof Error ? err.message : String(err)))
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
      if (!res.ok) throw new Error('保存失败，请稍后重试')
      setSaved({ backend: selectedBackend, model: selectedModel || null, variant: saved?.variant ?? null })
      setStatus({
        tone: 'info',
        text: `已保存。之后发起的审查会使用 ${displayName(selectedBackend)} · ${selectedModel || '后端默认模型'}。`,
      })
    } catch (err) {
      setStatus({ tone: 'error', text: err instanceof Error ? err.message : String(err) })
    } finally {
      setSaving(false)
    }
  }

  const header = (
    <div className="detail-head">
      <div className="detail-eyebrow">
        <button className="link-back" onClick={onBack}>
          <BackIcon />
          返回审查
        </button>
        <span aria-hidden="true">·</span>
        <span>设置</span>
      </div>
      <h1 className="detail-title">模型选择</h1>
      <p className="settings-lede">
        Mission Control 通过你本机已安装的 Agent CLI 执行审查。在这里选择默认使用的后端和模型，网页中发起的审查都会使用这份配置；仓库内的
        <code>.amc.yaml</code> 仍然优先。
      </p>
    </div>
  )

  if (loading) {
    return (
      <main className="main settings-main">
        <div className="main-inner settings-inner">
          {header}
          <div className="section-label">
            <span>后端</span>
            <span className="count">正在检测本机的 Agent CLI…</span>
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
          <div className="banner is-error" role="alert">{loadError}</div>
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
            <h2>没有检测到可用的 Agent CLI</h2>
            <p>安装下面任意一个，并确认它在 <code>PATH</code> 中，然后重新打开此页面。</p>
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
            <span>{status.text}</span>
            <button onClick={() => setStatus(null)} aria-label="关闭提示">✕</button>
          </div>
        )}

        <section aria-labelledby="backend-heading">
          <div className="section-label">
            <span id="backend-heading">后端</span>
            <span className="count">检测到 {backends.length} 个</span>
          </div>
          <div className="backend-grid" role="radiogroup" aria-labelledby="backend-heading">
            {backends.map((backend) => {
              const active = selectedBackend === backend.name
              return (
                <label
                  key={backend.name}
                  className={`backend-option ${active ? 'selected' : ''}`}
                  title={`命令：${backend.command}`}
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
                    {saved?.backend === backend.name && <span className="pill">当前</span>}
                    <span className="backend-check" aria-hidden="true">{active && <CheckIcon />}</span>
                  </span>
                  <span className="backend-meta">
                    {backend.model_count ? `${backend.model_count} 个可用模型` : '使用后端默认模型'}
                  </span>
                  <span className="backend-version mono">{backend.version || '版本未知'}</span>
                </label>
              )
            })}
          </div>
        </section>

        {selectedBackend && !currentBackend && (
          <div className="banner is-error" role="alert">
            已保存的后端「{displayName(selectedBackend)}」当前没有检测到。请先安装它，或在上方改选其他后端。
          </div>
        )}

        {currentBackend && (
          <section aria-labelledby="model-heading">
            <div className="section-label">
              <span id="model-heading">模型</span>
              {currentBackend.model_count > currentBackend.models.length && (
                <span className="count">
                  显示 {currentBackend.models.length} / {currentBackend.model_count}，已隐藏测试版和弃用模型
                </span>
              )}
            </div>

            <div className="settings-card model-card">
              {currentBackend.models.length > SEARCH_THRESHOLD && (
                <div className="model-search">
                  <input
                    type="search"
                    placeholder="搜索模型"
                    value={modelSearch}
                    onChange={(e) => setModelSearch(e.target.value)}
                    aria-label="搜索模型"
                  />
                </div>
              )}

              <div className="model-list" role="radiogroup" aria-labelledby="model-heading">
                {!query && (
                  <ModelOption
                    value=""
                    label="后端默认模型"
                    hint={currentBackend.models.length ? '不指定，由后端自己决定' : '这个后端没有提供可读取的模型列表'}
                    checked={selectedModel === ''}
                    onSelect={setSelectedModel}
                  />
                )}
                {customModel && (
                  <ModelOption
                    value={selectedModel}
                    label={selectedModel}
                    hint="当前配置，不在检测列表中"
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
                  <div className="model-empty">没有匹配「{modelSearch.trim()}」的模型</div>
                )}
              </div>
            </div>
          </section>
        )}

        <div className="settings-savebar">
          <div className="savebar-summary">
            <span className="meta-key">{dirty ? '将保存为' : '当前配置'}</span>
            <span className="savebar-value">
              {displayName(selectedBackend)}
              <span aria-hidden="true"> · </span>
              <span className="mono">{selectedModel || '后端默认模型'}</span>
            </span>
          </div>
          <button className="btn btn-ghost" onClick={onBack}>
            返回
          </button>
          <button
            className="btn btn-primary"
            onClick={handleSave}
            disabled={saving || !currentBackend || !dirty}
          >
            {saving ? '保存中…' : '保存配置'}
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
