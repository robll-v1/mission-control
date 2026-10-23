import { useEffect, useState } from 'react'

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
        const [data, current]: [DetectResponse, CurrentConfig] = await Promise.all([detectRes.json(), currentRes.json()])
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

  // Filter models based on search
  const filteredModels = currentBackend?.models.filter((model) =>
    model.toLowerCase().includes(modelSearch.toLowerCase())
  ) || []
  if (selectedModel && currentBackend?.models.includes(selectedModel) && !filteredModels.includes(selectedModel)) {
    filteredModels.unshift(selectedModel)
  }

  // Render models - grouped or flat
  const renderModelSelect = () => {
    if (!currentBackend) return null
    if (currentBackend.models.length === 0) return <p className="model-info">当前已保存模型：{selectedModel || '(默认)'}</p>

    const hasGroups = currentBackend.grouped_models && Object.keys(currentBackend.grouped_models).length > 0

    if (hasGroups && !modelSearch) {
      // Render grouped select
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

    // Render flat select (filtered)
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

  if (loading) {
    return (
      <div className="settings-container">
        <div className="settings-header">
          <button className="back-button" onClick={onBack}>
            ← 返回
          </button>
          <h1>配置</h1>
        </div>
        <div className="loading">正在检测 AI 后端...</div>
      </div>
    )
  }

  if (error && backends.length === 0) {
    return (
      <div className="settings-container">
        <div className="settings-header">
          <button className="back-button" onClick={onBack}>
            ← 返回
          </button>
          <h1>配置</h1>
        </div>
        <div className="error-box">
          <p className="error-title">未检测到 AI 后端</p>
          <p>请安装以下任意一个：</p>
          <ul>
            <li>OpenCode: <code>npm install -g @opencode/cli</code></li>
            <li>Claude Code: <a href="https://docs.anthropic.com/claude-code" target="_blank" rel="noopener noreferrer">安装指南</a></li>
            <li>GitHub Copilot: <code>gh extension install github/gh-copilot</code></li>
            <li>Codex CLI: <a href="https://github.com/openai/codex-cli" target="_blank" rel="noopener noreferrer">安装指南</a></li>
          </ul>
        </div>
      </div>
    )
  }

  return (
    <div className="settings-container">
      <div className="settings-header">
        <button className="back-button" onClick={onBack}>
          ← 返回
        </button>
        <h1>配置</h1>
      </div>

      <div className="settings-content">
        {error && <div className="error-message">{error}</div>}
        {saveSuccess && <div className="success-message">配置已保存</div>}

        <div className="settings-section">
          <h2>已检测到的 AI 后端 ({backends.length})</h2>
          <div className="backend-list">
            {backends.map((backend) => (
              <label key={backend.name} className={`backend-card ${selectedBackend === backend.name ? 'selected' : ''}`}>
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
                <div className="backend-info">
                  <div className="backend-name">{backend.name}</div>
                  <div className="backend-version">版本: {backend.version}</div>
                  <div className="backend-command">命令: <code>{backend.command}</code></div>
                  {backend.model_count > backend.models.length && (
                    <div className="backend-filter-info">
                      显示 {backend.models.length} / {backend.model_count} 个模型（已过滤）
                    </div>
                  )}
                </div>
              </label>
            ))}
          </div>
        </div>

        {selectedBackend && !currentBackend && (
          <div className="error-box">已保存的后端“{selectedBackend}”当前不可用。请先安装并检测到该后端，再保存其他配置。</div>
        )}

        {currentBackend && (
          <div className="settings-section">
            <h2>选择模型</h2>

            {currentBackend.models.length > 10 && (
              <div className="model-search">
                <input
                  type="text"
                  placeholder="搜索模型..."
                  value={modelSearch}
                  onChange={(e) => setModelSearch(e.target.value)}
                  className="search-input"
                />
                {modelSearch && (
                  <button
                    className="clear-search"
                    onClick={() => setModelSearch('')}
                  >
                    ✕
                  </button>
                )}
              </div>
            )}

            {renderModelSelect()}

            {currentBackend.grouped_models && !modelSearch && (
              <div className="model-info">
                模型已按类型分组显示
              </div>
            )}

            {modelSearch && filteredModels.length === 0 && (
              <div className="no-results">
                没有找到匹配 "{modelSearch}" 的模型
              </div>
            )}
          </div>
        )}

        {currentBackend && customModel && currentBackend.models.length > 0 && (
          <div className="settings-section"><p className="model-info">当前保存模型未出现在检测列表中，将保留为：{selectedModel}</p></div>
        )}

        <div className="settings-section">
          <h2>配置预览</h2>
          <div className="config-preview">
            <div className="config-line">
              <span className="config-key">后端:</span>
              <span className="config-value">{selectedBackend}</span>
            </div>
            <div className="config-line">
              <span className="config-key">模型:</span>
              <span className="config-value">{selectedModel || '(默认)'}</span>
            </div>
          </div>
        </div>

        <div className="settings-actions">
          <button className="btn btn-ghost" onClick={onBack}>
            取消
          </button>
          <button
            className="btn btn-primary"
            onClick={handleSave}
            disabled={saving || !selectedBackend || !currentBackend}
          >
            {saving ? '保存中...' : '保存配置'}
          </button>
        </div>
      </div>
    </div>
  )
}
