import { useEffect, useRef } from 'react'
import type { Messages, ReviewLanguage } from '../i18n'
import {
  resolveReasoningOptions,
  type BackendCapability,
  type RunDraft,
} from '../runSettings'
import { CheckIcon } from './Icons'

type Props = {
  open: boolean
  nextRoundNumber: number
  backends: BackendCapability[]
  draft: RunDraft
  note: string
  focusOverride: string
  language: ReviewLanguage
  t: Messages
  onClose: () => void
  onSelectAgent: (agent: string) => void
  onSelectModel: (model: string) => void
  onSelectReasoning: (reasoning: string) => void
  onChangeLanguage: (lang: ReviewLanguage) => void
  onChangeNote: (note: string) => void
  onChangeFocus: (focus: string) => void
  onRunNow: () => void
}

export function RunRoundPopover({
  open,
  nextRoundNumber,
  backends,
  draft,
  note,
  focusOverride,
  language,
  t,
  onClose,
  onSelectAgent,
  onSelectModel,
  onSelectReasoning,
  onChangeLanguage,
  onChangeNote,
  onChangeFocus,
  onRunNow,
}: Props) {
  const ref = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        onClose()
      }
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onKey)
    }
  }, [open, onClose])

  if (!open) return null

  const activeCap = backends.find((b) => b.name === draft.agent)
  const reasoningOpts = resolveReasoningOptions(activeCap, draft.model)

  return (
    <div ref={ref} className="wb-popover" role="dialog" aria-label={t.workbench.runRoundWith(nextRoundNumber)}>
      <div className="wb-popover-section">
        <div className="wb-cap">{t.workbench.runRoundWith(nextRoundNumber)}</div>
        <div className="wb-popover-agents">
          {backends.map((b) => {
            const active = b.name === draft.agent
            return (
              <button
                key={b.name}
                type="button"
                className={`wb-popover-agent-row ${active ? 'active' : ''}`}
                onClick={() => onSelectAgent(b.name)}
              >
                <span className="wb-popover-check">{active ? <CheckIcon size={13} /> : null}</span>
                <span className="wb-b">{b.name}</span>
                <span className="wb-t2 wb-truncate">
                  {active
                    ? draft.model || t.settings.agentDefault
                    : b.models[0] || t.settings.agentDefault}
                </span>
              </button>
            )
          })}
        </div>
      </div>

      {activeCap && (
        <div className="wb-popover-section">
          <label className="wb-cap" htmlFor="wb-pop-model">{t.detail.roundModel}</label>
          <select
            id="wb-pop-model"
            className="wb-select wb-select-sm"
            value={draft.model}
            onChange={(e) => onSelectModel(e.target.value)}
          >
            <option value="">{t.settings.agentDefault}</option>
            {activeCap.models.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
            {draft.model && !activeCap.models.includes(draft.model) && (
              <option value={draft.model}>{draft.model}</option>
            )}
          </select>
        </div>
      )}

      {reasoningOpts.supported && reasoningOpts.levels.length > 0 && (
        <div className="wb-popover-section">
          <div className="wb-popover-row-head">
            <span className="wb-cap">{t.detail.roundReasoning}</span>
            <span className="wb-xs wb-t3">
              {t.workbench.levelsCount(reasoningOpts.levels.length, reasoningOpts.defaultLevel)}
            </span>
          </div>
          <div className="wb-reasoning-pills">
            <button
              type="button"
              className={`wb-rpill ${draft.reasoning === '' ? 'active' : ''}`}
              onClick={() => onSelectReasoning('')}
            >
              {t.workbench.defaultBadge}
            </button>
            {reasoningOpts.levels.map((lvl) => (
              <button
                key={lvl}
                type="button"
                className={`wb-rpill ${draft.reasoning === lvl ? 'active' : ''}`}
                onClick={() => onSelectReasoning(lvl)}
              >
                {lvl}
              </button>
            ))}
          </div>
          <p className="wb-xs wb-t3 wb-mt-xs">{t.workbench.higherIsSlower}</p>
        </div>
      )}

      <div className="wb-popover-section">
        <label className="wb-cap" htmlFor="wb-pop-lang">{t.detail.roundLanguage}</label>
        <select
          id="wb-pop-lang"
          className="wb-select wb-select-sm"
          value={language}
          onChange={(e) => onChangeLanguage(e.target.value as ReviewLanguage)}
        >
          <option value="en">{t.detail.reviewLangEn}</option>
          <option value="zh">{t.detail.reviewLangZh}</option>
        </select>
      </div>

      <div className="wb-popover-section">
        <label className="wb-cap" htmlFor="wb-pop-note">{t.detail.roundNote}</label>
        <input
          id="wb-pop-note"
          type="text"
          className="wb-input wb-input-sm"
          value={note}
          onChange={(e) => onChangeNote(e.target.value)}
          placeholder={t.detail.roundNotePlaceholder}
        />
      </div>

      <div className="wb-popover-section">
        <label className="wb-cap" htmlFor="wb-pop-focus">{t.workbench.focusOverride}</label>
        <input
          id="wb-pop-focus"
          type="text"
          className="wb-input wb-input-sm"
          value={focusOverride}
          onChange={(e) => onChangeFocus(e.target.value)}
          placeholder={t.workbench.focusOverridePlaceholder}
        />
      </div>

      <div className="wb-popover-footer">
        <button
          type="button"
          className="wb-btn wb-btn-primary wb-btn-full"
          onClick={() => {
            onClose()
            onRunNow()
          }}
        >
          {t.workbench.runRoundN(nextRoundNumber)}
        </button>
      </div>
    </div>
  )
}
