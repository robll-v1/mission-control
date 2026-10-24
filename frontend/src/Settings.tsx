import { useEffect, useState } from 'react'
import { ArrowLeftIcon, CloseIcon, Insignia, SearchIcon } from './components/Icons'

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

type CurrentConfig = {
  config: { backend: string; model: string | null; variant: string | null }
}

/* Settings — backend & model configuration, console-styled.
   Same endpoints and behaviour as before: detect → current → save. */
export function Settings({ onBack }: { onBack: () => void }) {
  const [backends, setBackends] = useState<Backend[]>([])
  const [selectedBackend, setSelectedBackend] = useState('')
  const [selectedModel, setSelectedModel] = useState('')
  const [modelSearch, setModelSearch] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const [saveSuccess, setSaveSuccess] = useState(false)

  useEffect(() => {
    Promise.all([fetch('/api/config/detect'), fetch('/api/config/current')])
      .then(async ([detectRes, currentRes]) => {
        if (!detectRes.ok || !currentRes.ok) throw new Error('Failed to load configuration')
        const [data, current]: [DetectResponse, CurrentConfig] = await Promise.all([
          detectRes.json(),
          currentRes.json(),
        ])
        setBackends(data.backends)
        const savedBackend = current.config.backend
        setSelectedBackend(savedBackend || data.backends[0]?.name || '')
        setSelectedModel(current.config.model || '')
        setLoading(false)
      })
      .catch((err) => {
        setError(err.message)
        setLoading(false)
      })
  }, [])

  const handleSave = async () => {
    setSaving(true)
    setSaveSuccess(false)
    try {
      const res = await fetch('/api/config/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          backend: selectedBackend,
          model: selectedModel,
        }),
      })

      if (!res.ok) {
        throw new Error('Failed to save configuration')
      }

      setSaveSuccess(true)
      setTimeout(() => setSaveSuccess(false), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setSaving(false)
    }
  }

  const currentBackend = backends.find((b) => b.name === selectedBackend)
  const customModel = selectedModel && !currentBackend?.models.includes(selectedModel)

  const filteredModels =
    currentBackend?.models.filter((model) =>
      model.toLowerCase().includes(modelSearch.toLowerCase()),
    ) || []
  if (selectedModel && currentBackend?.models.includes(selectedModel) && !filteredModels.includes(selectedModel)) {
    filteredModels.unshift(selectedModel)
  }

  const renderModelSelect = () => {
    if (!currentBackend) return null
    if (currentBackend.models.length === 0)
      return <p className="st-note">当前已保存模型：{selectedModel || '(默认)'}</p>

    const hasGroups =
      currentBackend.grouped_models && Object.keys(currentBackend.grouped_models).length > 0

    if (hasGroups && !modelSearch) {
      return (
        <select
          value={selectedModel}
          onChange={(e) => setSelectedModel(e.target.value)}
          className="model-select"
        >
          <option value="">后端默认模型</option>
          {customModel && <option value={selectedModel}>{selectedModel}（当前配置）</option>}
          {Object.entries(currentBackend.grouped_models!).map(([group, models]) => (
            <optgroup key={group} label={`${group} (${models.length})`}>
              {models.map((model) => (
                <option key={model} value={model}>
                  {model}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
      )
    }

    return (
      <select
        value={selectedModel}
        onChange={(e) => setSelectedModel(e.target.value)}
        className="model-select"
      >
        <option value="">后端默认模型</option>
        {customModel && <option value={selectedModel}>{selectedModel}（当前配置）</option>}
        {filteredModels.map((model) => (
          <option key={model} value={model}>
            {model}
          </option>
        ))}
      </select>
    )
  }

  return (
    <div className="console">
      <div className="atmosphere" aria-hidden="true" />
      <div className="grain" aria-hidden="true" />

      <header className="cmdbar">
        <button className="icon-btn" onClick={onBack} aria-label="返回控制台">
          <ArrowLeftIcon size={17} />
        </button>
        <div className="brand">
          <Insignia size={26} />
          <span className="brand-text">
            <span className="brand-name">MISSION CONTROL</span>
            <span className="brand-sub">系统配置 / system config</span>
          </span>
        </div>
        <div className="cmdbar-right">
          <span className="micro">config / 后端与模型</span>
        </div>
      </header>

      <main className="stage">
        <div className="st-wrap">
          <div className="st-head">
            <h1 className="st-title">系统配置</h1>
            <span className="micro" style={{ marginLeft: 'auto' }}>
              backend &amp; model
            </span>
          </div>

          {loading && <div className="panel st-section"><div className="st-loading">正在检测 AI 后端…</div></div>}

          {!loading && error && backends.length === 0 && (
            <section className="panel st-section st-error">
              <span className="brackets" aria-hidden="true" />
              <div className="st-section-head">
                <span className="micro" style={{ color: 'var(--danger)' }}>
                  fault / 未检测到 AI 后端
                </span>
              </div>
              <div className="st-section-body">
                <p>请安装以下任意一个后端：</p>
                <ul className="st-list">
                  <li>
                    OpenCode: <code>npm install -g @opencode/cli</code>
                  </li>
                  <li>
                    Claude Code:{' '}
                    <a href="https://docs.anthropic.com/claude-code" target="_blank" rel="noreferrer">
                      安装指南
                    </a>
                  </li>
                  <li>
                    GitHub Copilot: <code>gh extension install github/gh-copilot</code>
                  </li>
                  <li>
                    Codex CLI:{' '}
                    <a href="https://github.com/openai/codex-cli" target="_blank" rel="noreferrer">
                      安装指南
                    </a>
                  </li>
                </ul>
              </div>
            </section>
          )}

          {!loading && backends.length > 0 && (
            <>
              {error && <div className="banner banner-error" role="status">{error}</div>}
              {saveSuccess && (
                <div className="banner banner-info" role="status">
                  配置已保存
                </div>
              )}

              <section className="panel st-section">
                <span className="brackets" aria-hidden="true" />
                <div className="st-section-head">
                  <span className="micro">
                    已检测到的后端<i>/</i>backends
                  </span>
                  <span className="micro">{backends.length}</span>
                </div>
                <div className="st-section-body">
                  <div className="backend-list">
                    {backends.map((backend) => (
                      <label
                        key={backend.name}
                        className={`backend-card ${selectedBackend === backend.name ? 'selected' : ''}`}
                      >
                        <input
                          type="radio"
                          name="backend"
                          value={backend.name}
                          checked={selectedBackend === backend.name}
                          onChange={(e) => {
                            setSelectedBackend(e.target.value)
                            setSelectedModel('')
                          }}
                        />
                        <span className="backend-info">
                          <span className="backend-name">
                            {backend.name}
                            {selectedBackend === backend.name && (
                              <span className="micro" style={{ color: 'var(--accent)' }}>
                                selected
                              </span>
                            )}
                          </span>
                          <span className="backend-version">版本 {backend.version}</span>
                          <span className="backend-command">
                            命令 <code>{backend.command}</code>
                          </span>
                          {backend.model_count > backend.models.length && (
                            <span className="micro">
                              显示 {backend.models.length} / {backend.model_count} 个模型（已过滤）
                            </span>
                          )}
                        </span>
                      </label>
                    ))}
                  </div>
                </div>
              </section>

              {selectedBackend && !currentBackend && (
                <div className="banner banner-error" role="status">
                  已保存的后端“{selectedBackend}”当前不可用。请先安装并检测到该后端，再保存其他配置。
                </div>
              )}

              {currentBackend && (
                <section className="panel st-section">
                  <span className="brackets" aria-hidden="true" />
                  <div className="st-section-head">
                    <span className="micro">
                      选择模型<i>/</i>model
                    </span>
                    {currentBackend.grouped_models && !modelSearch && (
                      <span className="micro">已按类型分组</span>
                    )}
                  </div>
                  <div className="st-section-body">
                    {currentBackend.models.length > 10 && (
                      <div className="rail-search">
                        <span className="search-icon">
                          <SearchIcon />
                        </span>
                        <input
                          type="text"
                          placeholder="搜索模型…"
                          value={modelSearch}
                          onChange={(e) => setModelSearch(e.target.value)}
                          className="search-input"
                          aria-label="搜索模型"
                        />
                        {modelSearch && (
                          <button
                            className="search-clear"
                            onClick={() => setModelSearch('')}
                            aria-label="清空搜索"
                          >
                            <CloseIcon />
                          </button>
                        )}
                      </div>
                    )}

                    {renderModelSelect()}

                    {modelSearch && filteredModels.length === 0 && (
                      <div className="empty-note">没有找到匹配 “{modelSearch}” 的模型</div>
                    )}
                    {currentBackend && customModel && currentBackend.models.length > 0 && (
                      <p className="st-note">当前保存模型未出现在检测列表中，将保留为：{selectedModel}</p>
                    )}
                  </div>
                </section>
              )}

              <section className="panel st-section">
                <span className="brackets" aria-hidden="true" />
                <div className="st-section-head">
                  <span className="micro">
                    配置预览<i>/</i>preview
                  </span>
                </div>
                <div className="st-section-body">
                  <div className="config-preview">
                    <div className="config-line">
                      <span className="config-key">backend</span>
                      <span className="config-value">{selectedBackend}</span>
                    </div>
                    <div className="config-line">
                      <span className="config-key">model</span>
                      <span className="config-value">{selectedModel || '(默认)'}</span>
                    </div>
                  </div>
                  <div className="st-actions">
                    <button className="btn btn-ghost" onClick={onBack}>
                      取消
                    </button>
                    <button
                      className="btn btn-primary"
                      onClick={handleSave}
                      disabled={saving || !selectedBackend || !currentBackend}
                    >
                      {saving ? '保存中…' : '保存配置'}
                    </button>
                  </div>
                </div>
              </section>
            </>
          )}
        </div>
      </main>
    </div>
  )
}
