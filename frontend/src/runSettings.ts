/**
 * Pure helpers for per-round RunSettings, reasoning effort capability lookup,
 * explicit-null reset payloads, and cross-agent isolation.
 *
 * Imports nothing and touches no browser globals so `node --test` can load it directly.
 */

export type ModelReasoningInfo = {
  levels: string[]
  default?: string | null
}

export type BackendCapability = {
  name: string
  version: string
  models: string[]
  command: string
  model_count: number
  grouped_models?: Record<string, string[]> | null
  models_source?: string
  models_note?: string | null
  models_call_verified?: boolean
  reasoning_supported?: boolean
  reasoning_levels?: string[]
  reasoning_default?: string | null
  reasoning_source?: string
  reasoning_note?: string | null
  model_reasoning?: Record<string, ModelReasoningInfo>
}

export type PerBackendSaved = {
  model: string | null
  reasoning?: string | null
  variant: string | null
}

export type SavedConfig = {
  backend: string
  model: string | null
  reasoning?: string | null
  variant: string | null
  per_backend?: Record<string, PerBackendSaved>
}

export type RunSettings = {
  agent: string
  model?: string | null
  reasoning?: string | null
  speed?: string | null
  language: string
  focus?: string
  note?: string
  sources?: Record<string, string>
}

export type EffectiveSettingsResponse = {
  settings: RunSettings
  backend: string
  model: string | null
  reasoning: string | null
  sources: Record<string, string>
}

export type CheckAgentResponse = {
  ok: boolean
  backend: string
  executable: string
  command: string[]
  exit_code: number | null
  message: string
  model: string | null
  reasoning: string | null
}

export type ResolvedReasoningOptions = {
  supported: boolean
  levels: string[]
  defaultLevel: string | null
}

export type RunDraft = {
  agent: string
  model: string
  reasoning: string
  language: 'en' | 'zh'
  note: string
  explicit: {
    agent: boolean
    model: boolean
    reasoning: boolean
    language: boolean
  }
  /** Tracks fields directly edited by the user in this form session (distinct from latestRound carryover). */
  userEdited: {
    agent: boolean
    model: boolean
    reasoning: boolean
    language: boolean
  }
}

export type RoundLike = {
  backend: string
  model?: string | null
  reasoning?: string | null
  language?: string | null
  settings?: RunSettings | null
}

export type TaskLike = {
  backend: string
  model?: string | null
  reasoning?: string | null
  language?: string | null
}

export function normalizeReviewLang(value?: string | null): 'en' | 'zh' {
  if (!value) return 'en'
  const lower = value.trim().toLowerCase()
  return lower === 'zh' || lower.startsWith('zh-') ? 'zh' : 'en'
}

/**
 * Resolve available reasoning effort levels for the selected agent + model.
 * Uses per-model cache metadata when present; otherwise falls back to
 * agent-level levels (e.g. Claude Code `--effort`).
 */
export function resolveReasoningOptions(
  backend: BackendCapability | undefined,
  selectedModel: string,
): ResolvedReasoningOptions {
  if (!backend) {
    return { supported: false, levels: [], defaultLevel: null }
  }
  const trimmedModel = selectedModel.trim()
  const perModel = trimmedModel ? backend.model_reasoning?.[trimmedModel] : undefined
  if (perModel && Array.isArray(perModel.levels) && perModel.levels.length > 0) {
    return {
      supported: true,
      levels: [...perModel.levels],
      defaultLevel: perModel.default ?? null,
    }
  }
  const levels = Array.isArray(backend.reasoning_levels) ? backend.reasoning_levels : []
  if (backend.reasoning_supported && levels.length > 0) {
    return {
      supported: true,
      levels: [...levels],
      defaultLevel: backend.reasoning_default ?? null,
    }
  }
  return { supported: false, levels: [], defaultLevel: null }
}

/**
 * Read the saved model & reasoning for a given agent from `/api/config/current`,
 * never leaking another agent's saved model or reasoning.
 */
export function savedForAgent(saved: SavedConfig | null, agent: string): { model: string; reasoning: string } {
  if (!saved || !agent) return { model: '', reasoning: '' }
  const entry = saved.per_backend?.[agent]
  if (entry) {
    return {
      model: entry.model || '',
      reasoning: entry.reasoning ?? entry.variant ?? '',
    }
  }
  if (saved.backend === agent) {
    return {
      model: saved.model || '',
      reasoning: saved.reasoning ?? saved.variant ?? '',
    }
  }
  return { model: '', reasoning: '' }
}

/**
 * Build payload for `POST /api/config/save`.
 * When reasoning is supported for this backend, always sends `reasoning`
 * explicitly (`null` for Model default) so backend clears any previously saved
 * reasoning. When reasoning is NOT supported (e.g. OpenCode legacy `variant`),
 * omits `reasoning` so existing hidden config is preserved.
 */
export function buildSaveConfigPayload(
  backend: string,
  model: string,
  reasoning: string,
  reasoningSupported = true,
): { backend: string; model: string | null; reasoning?: string | null } {
  const payload: { backend: string; model: string | null; reasoning?: string | null } = {
    backend: backend.trim(),
    model: model.trim() || null,
  }
  if (reasoningSupported) {
    payload.reasoning = reasoning.trim() || null
  }
  return payload
}

/**
 * Initialize a per-round draft from the latest round (if any), falling back to
 * the task's defaults and then the saved config for that agent.
 *
 * When the latest round switched agents or explicitly overrode model/reasoning
 * (including forcing CLI/Model default), mark those fields explicit so clicking
 * "Run next round" keeps the displayed settings instead of snapping back to the
 * original task's agent or defaults.
 */
export function createRunDraft(
  task: TaskLike | null,
  latestRound: RoundLike | undefined,
  saved: SavedConfig | null,
  effective?: EffectiveSettingsResponse | null,
): RunDraft {
  const agent = latestRound?.settings?.agent || latestRound?.backend || task?.backend || saved?.backend || ''
  const fromSaved = savedForAgent(saved, agent)
  const effectiveMatchesAgent = Boolean(effective && effective.backend === agent)
  const baselineModel = effectiveMatchesAgent ? (effective?.model ?? '') : fromSaved.model
  const baselineReasoning = effectiveMatchesAgent ? (effective?.reasoning ?? '') : fromSaved.reasoning
  let model = ''
  let reasoning = ''
  if (latestRound) {
    model = latestRound.settings?.model ?? latestRound.model ?? ''
    reasoning = latestRound.settings?.reasoning ?? latestRound.reasoning ?? ''
  } else if (task) {
    model = task.model ?? baselineModel
    reasoning = task.reasoning ?? baselineReasoning
  } else {
    model = baselineModel
    reasoning = baselineReasoning
  }
  const language = normalizeReviewLang(
    latestRound?.settings?.language ?? latestRound?.language ?? task?.language ?? 'en',
  )

  const switchedFromTask = Boolean(latestRound && task && agent && agent !== task.backend)
  const roundOverrodeModel = Boolean(
    latestRound &&
      (latestRound.settings?.sources?.model === 'round' ||
        (model === '' && Boolean(baselineModel)) ||
        (!switchedFromTask && task?.model && model !== task.model)),
  )
  const roundOverrodeReasoning = Boolean(
    latestRound &&
      (latestRound.settings?.sources?.reasoning === 'round' ||
        (reasoning === '' && Boolean(baselineReasoning)) ||
        (!switchedFromTask && task?.reasoning && reasoning !== task.reasoning)),
  )

  return {
    agent,
    model,
    reasoning,
    language,
    note: '',
    explicit: {
      agent: switchedFromTask,
      model: roundOverrodeModel,
      reasoning: roundOverrodeReasoning,
      language: false,
    },
    userEdited: {
      agent: false,
      model: false,
      reasoning: false,
      language: false,
    },
  }
}

/**
 * Merge newly arrived saved/repo-effective config into a draft field-by-field.
 *
 * Purely re-derives every non-user-edited field and its `explicit` flag from
 * `createRunDraft(task, latestRound, saved, effective)` so the result is
 * strictly commutative (independent of whether `saved` or `effective` arrives
 * first), while never overwriting any field the user edited.
 */
export function mergeEffectiveIntoDraft(
  prev: RunDraft,
  task: TaskLike,
  latestRound: RoundLike | undefined,
  saved: SavedConfig | null,
  effective?: EffectiveSettingsResponse | null,
): RunDraft {
  if (prev.userEdited.agent) return prev
  const next = createRunDraft(task, latestRound, saved, effective)
  return {
    ...prev,
    agent: next.agent,
    model: prev.userEdited.model ? prev.model : next.model,
    reasoning: prev.userEdited.reasoning ? prev.reasoning : next.reasoning,
    language: prev.userEdited.language ? prev.language : next.language,
    explicit: {
      agent: next.explicit.agent,
      model: prev.userEdited.model ? prev.explicit.model : next.explicit.model,
      reasoning: prev.userEdited.reasoning ? prev.explicit.reasoning : next.explicit.reasoning,
      language: prev.userEdited.language ? prev.explicit.language : next.explicit.language,
    },
  }
}

/**
 * Switch the agent on a draft: resets model and reasoning to the target
 * agent's saved defaults (or CLI/Model default) and clears explicit model/reasoning
 * flags so the previous agent's values never leak across agents.
 */
export function switchDraftAgent(
  draft: RunDraft,
  nextAgent: string,
  backends: BackendCapability[],
  saved: SavedConfig | null,
): RunDraft {
  const defaults = savedForAgent(saved, nextAgent)
  const cap = backends.find((b) => b.name === nextAgent)
  const opts = resolveReasoningOptions(cap, defaults.model)
  const nextReasoning = opts.supported ? defaults.reasoning : ''
  return {
    ...draft,
    agent: nextAgent,
    model: defaults.model,
    reasoning: nextReasoning,
    explicit: {
      ...draft.explicit,
      agent: true,
      model: false,
      reasoning: false,
    },
    userEdited: {
      ...draft.userEdited,
      agent: true,
      model: false,
      reasoning: false,
    },
  }
}

/**
 * Build payload for `POST /api/tasks/{id}/start`.
 * Only includes `backend`, `model`, `reasoning` when explicitly changed by the
 * user (sending `null` when user explicitly picked CLI default / Model default).
 */
export function buildStartRoundPayload(draft: RunDraft): Record<string, string | null> {
  const payload: Record<string, string | null> = {
    language: draft.language,
  }
  if (draft.explicit.agent && draft.agent.trim()) {
    payload.backend = draft.agent.trim()
  }
  if (draft.explicit.model) {
    payload.model = draft.model.trim() || null
  }
  if (draft.explicit.reasoning) {
    payload.reasoning = draft.reasoning.trim() || null
  }
  const note = draft.note.trim()
  if (note) {
    payload.review_note = note
  }
  return payload
}

/**
 * Format a compact `agent · model · reasoning` summary for round cards and headers.
 * Raw reasoning levels (`low`, `high`, `xhigh`, `max`, ...) are never translated.
 */
export function formatRoundSettingsLabel(
  round: RoundLike,
  backends: BackendCapability[],
  labels: { cliDefault: string; modelDefault: string },
): string {
  const agent = round.settings?.agent || round.backend
  const model = round.settings?.model !== undefined ? round.settings.model : round.model
  const reasoning = round.settings?.reasoning !== undefined ? round.settings.reasoning : round.reasoning
  const parts: string[] = [agent]
  parts.push(model ? model : labels.cliDefault)

  const cap = backends.find((b) => b.name === agent)
  const knownUnsupported = cap !== undefined && cap.reasoning_supported === false
  if (reasoning) {
    parts.push(reasoning)
  } else if (!knownUnsupported) {
    parts.push(labels.modelDefault)
  }
  return parts.join(' · ')
}
