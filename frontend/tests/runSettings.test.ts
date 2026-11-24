// Run with: npm test (from frontend/; Node >= 22.18 strips types natively)
import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  buildSaveConfigPayload,
  buildStartRoundPayload,
  createRunDraft,
  formatRoundSettingsLabel,
  mergeEffectiveIntoDraft,
  normalizeReviewLang,
  resolveReasoningOptions,
  savedForAgent,
  switchDraftAgent,
  type BackendCapability,
  type SavedConfig,
} from '../src/runSettings.ts'

const CODEX: BackendCapability = {
  name: 'codex',
  version: 'codex-cli 0.169.0',
  models: ['gpt-6.2-sol', 'gpt-6-luna'],
  command: 'codex',
  model_count: 2,
  reasoning_supported: true,
  reasoning_levels: ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'],
  reasoning_default: null,
  reasoning_source: 'cache',
  model_reasoning: {
    'gpt-6.2-sol': { levels: ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'], default: 'low' },
    'gpt-6-luna': { levels: ['low', 'medium', 'high'], default: 'medium' },
  },
}

const CLAUDE: BackendCapability = {
  name: 'claude-code',
  version: '2.1.310 (Claude Code)',
  models: [],
  command: 'claude',
  model_count: 0,
  reasoning_supported: true,
  reasoning_levels: ['low', 'medium', 'high', 'xhigh', 'max'],
  reasoning_default: null,
  reasoning_source: 'cli_help',
}

const COPILOT: BackendCapability = {
  name: 'copilot',
  version: 'gh 2.60.0',
  models: [],
  command: 'gh',
  model_count: 0,
  reasoning_supported: false,
  reasoning_levels: [],
  reasoning_source: 'unsupported',
}

const BACKENDS = [CODEX, CLAUDE, COPILOT]

const SAVED: SavedConfig = {
  backend: 'codex',
  model: 'gpt-6.2-sol',
  reasoning: 'xhigh',
  variant: 'xhigh',
  per_backend: {
    codex: { model: 'gpt-6.2-sol', reasoning: 'xhigh', variant: 'xhigh' },
    'claude-code': { model: 'claude-opus-5.5', reasoning: 'max', variant: 'max' },
  },
}

test('resolveReasoningOptions prefers per-model cache levels and default', () => {
  const sol = resolveReasoningOptions(CODEX, 'gpt-6.2-sol')
  assert.deepEqual(sol, {
    supported: true,
    levels: ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'],
    defaultLevel: 'low',
  })

  const luna = resolveReasoningOptions(CODEX, 'gpt-6-luna')
  assert.deepEqual(luna, {
    supported: true,
    levels: ['low', 'medium', 'high'],
    defaultLevel: 'medium',
  })

  // Falls back to agent-level levels when model is CLI default or unknown
  const codexDefault = resolveReasoningOptions(CODEX, '')
  assert.equal(codexDefault.supported, true)
  assert.equal(codexDefault.levels.includes('ultra'), true)
  assert.equal(codexDefault.defaultLevel, null)

  const claude = resolveReasoningOptions(CLAUDE, '')
  assert.deepEqual(claude, {
    supported: true,
    levels: ['low', 'medium', 'high', 'xhigh', 'max'],
    defaultLevel: null,
  })

  // Unsupported or missing cache hides reasoning options
  assert.deepEqual(resolveReasoningOptions(COPILOT, ''), {
    supported: false,
    levels: [],
    defaultLevel: null,
  })
  assert.deepEqual(resolveReasoningOptions(undefined, 'gpt-6.2-sol'), {
    supported: false,
    levels: [],
    defaultLevel: null,
  })
})

test('savedForAgent isolates saved model and reasoning per backend', () => {
  assert.deepEqual(savedForAgent(SAVED, 'codex'), { model: 'gpt-6.2-sol', reasoning: 'xhigh' })
  assert.deepEqual(savedForAgent(SAVED, 'claude-code'), { model: 'claude-opus-5.5', reasoning: 'max' })
  assert.deepEqual(savedForAgent(SAVED, 'copilot'), { model: '', reasoning: '' })
})

test('buildSaveConfigPayload sends explicit null when supported and omits reasoning when unsupported', () => {
  assert.deepEqual(buildSaveConfigPayload('codex', 'gpt-6.2-sol', 'xhigh', true), {
    backend: 'codex',
    model: 'gpt-6.2-sol',
    reasoning: 'xhigh',
  })
  const reset = buildSaveConfigPayload('codex', '', '', true)
  assert.deepEqual(reset, { backend: 'codex', model: null, reasoning: null })
  assert.ok(Object.prototype.hasOwnProperty.call(reset, 'reasoning'))
  assert.ok(Object.prototype.hasOwnProperty.call(reset, 'model'))

  // Unsupported backend (e.g. OpenCode with hidden legacy variant) must OMIT reasoning
  const opencodeSave = buildSaveConfigPayload('opencode', 'openai/gpt-6', '', false)
  assert.deepEqual(opencodeSave, { backend: 'opencode', model: 'openai/gpt-6' })
  assert.equal(Object.prototype.hasOwnProperty.call(opencodeSave, 'reasoning'), false)
})

test('createRunDraft preserves switched agent and explicit CLI/Model default from latestRound', () => {
  const draft = createRunDraft(
    { backend: 'codex', model: 'gpt-6.2-sol', reasoning: 'xhigh', language: 'en' },
    {
      backend: 'claude-code',
      model: null,
      reasoning: null,
      language: 'en',
      settings: {
        agent: 'claude-code',
        model: null,
        reasoning: null,
        language: 'en',
        sources: { agent: 'round', model: 'round', reasoning: 'round', language: 'round' },
      },
    },
    SAVED,
  )
  assert.equal(draft.agent, 'claude-code')
  assert.equal(draft.model, '')
  assert.equal(draft.reasoning, '')
  assert.deepEqual(buildStartRoundPayload(draft), {
    language: 'en',
    backend: 'claude-code',
    model: null,
    reasoning: null,
  })
})

test('switchDraftAgent clears previous agent model/reasoning and explicit flags', () => {
  const initial = createRunDraft(
    { backend: 'codex', model: 'gpt-6.2-sol', reasoning: 'xhigh', language: 'en' },
    undefined,
    SAVED,
  )
  assert.equal(initial.agent, 'codex')
  assert.equal(initial.model, 'gpt-6.2-sol')
  assert.equal(initial.reasoning, 'xhigh')

  // Unmodified draft omits backend/model/reasoning so backend inherits cleanly
  assert.deepEqual(buildStartRoundPayload(initial), { language: 'en' })

  // User edits model & reasoning on codex, then switches to claude-code
  const edited = {
    ...initial,
    model: 'gpt-6-luna',
    reasoning: 'high',
    explicit: { ...initial.explicit, model: true, reasoning: true },
  }
  const switched = switchDraftAgent(edited, 'claude-code', BACKENDS, SAVED)
  assert.equal(switched.agent, 'claude-code')
  assert.equal(switched.model, 'claude-opus-5.5')
  assert.equal(switched.reasoning, 'max')
  assert.deepEqual(switched.explicit, {
    agent: true,
    model: false,
    reasoning: false,
    language: false,
  })
  // Switching sends only backend + language so target agent resolves its own defaults
  assert.deepEqual(buildStartRoundPayload(switched), {
    language: 'en',
    backend: 'claude-code',
  })

  // Switching to Copilot clears reasoning completely
  const switchedCopilot = switchDraftAgent(edited, 'copilot', BACKENDS, SAVED)
  assert.equal(switchedCopilot.model, '')
  assert.equal(switchedCopilot.reasoning, '')
})

test('buildStartRoundPayload sends explicit null when user selects CLI/Model default', () => {
  const draft = createRunDraft({ backend: 'codex', model: 'gpt-6.2-sol', reasoning: 'xhigh' }, undefined, SAVED)
  const resetDraft = {
    ...draft,
    model: '',
    reasoning: '',
    note: 'Focus on error handling',
    language: 'zh' as const,
    explicit: { agent: false, model: true, reasoning: true, language: true },
  }
  assert.deepEqual(buildStartRoundPayload(resetDraft), {
    language: 'zh',
    model: null,
    reasoning: null,
    review_note: 'Focus on error handling',
  })
})

test('createRunDraft uses repo effective settings over global config for auto-created tasks', () => {
  const effectiveForRepo = {
    settings: {
      agent: 'codex',
      model: 'gpt-repo-only',
      reasoning: 'medium',
      language: 'en',
      sources: { agent: 'global', model: 'repo', reasoning: 'repo', language: 'default' },
    },
    backend: 'codex',
    model: 'gpt-repo-only',
    reasoning: 'medium',
    sources: { agent: 'global', model: 'repo', reasoning: 'repo', language: 'default' },
  }
  const draft = createRunDraft(
    { backend: 'codex', model: null, reasoning: null, language: 'en' },
    undefined,
    SAVED,
    effectiveForRepo,
  )
  assert.equal(draft.agent, 'codex')
  assert.equal(draft.model, 'gpt-repo-only')
  assert.equal(draft.reasoning, 'medium')
  // Still untouched so backend resolves from repo/env cleanly
  assert.deepEqual(buildStartRoundPayload(draft), { language: 'en' })

  // If user edited reasoning before /effective returned, merge updates untouched model while keeping edited reasoning
  const editedBeforeEffective = {
    ...createRunDraft({ backend: 'codex', model: null, reasoning: null, language: 'en' }, undefined, SAVED),
    reasoning: 'high',
    note: 'Keep my note',
    explicit: { agent: false, model: false, reasoning: true, language: false },
    userEdited: { agent: false, model: false, reasoning: true, language: false },
  }
  const merged = mergeEffectiveIntoDraft(
    editedBeforeEffective,
    { backend: 'codex', model: null, reasoning: null, language: 'en' },
    undefined,
    SAVED,
    effectiveForRepo,
  )
  assert.equal(merged.model, 'gpt-repo-only')
  assert.equal(merged.reasoning, 'high')
  assert.equal(merged.note, 'Keep my note')
  assert.deepEqual(buildStartRoundPayload(merged), {
    language: 'en',
    reasoning: 'high',
    review_note: 'Keep my note',
  })

  // Subsequent round with switched agent + CLI/Model default + late savedConfig must emit explicit nulls
  const switchedTask = { backend: 'codex', model: null, reasoning: null, language: 'en' }
  const priorSwitchedRound = { backend: 'claude-code', model: null, reasoning: null, language: 'en' }
  const beforeSaved = createRunDraft(switchedTask, priorSwitchedRound, null)
  const afterSaved = mergeEffectiveIntoDraft(beforeSaved, switchedTask, priorSwitchedRound, SAVED, null)
  assert.deepEqual(buildStartRoundPayload(afterSaved), {
    language: 'en',
    backend: 'claude-code',
    model: null,
    reasoning: null,
  })

  // Commutativity: saved-first vs effective-first converge to identical draft & payload
  const openTask = { backend: 'opencode', model: null, reasoning: null, language: 'en' }
  const openRound = {
    backend: 'opencode',
    model: null,
    reasoning: null,
    language: 'en',
    settings: { agent: 'opencode', model: null, reasoning: null, language: 'en', sources: { model: 'default', reasoning: 'default' } },
  }
  const openSaved: SavedConfig = { backend: 'opencode', model: null, reasoning: 'high', variant: 'high' }
  const openEffective = {
    settings: openRound.settings,
    backend: 'opencode',
    model: null,
    reasoning: null,
    sources: openRound.settings.sources,
  }
  const openInit = createRunDraft(openTask, openRound, null)
  const savedFirst = mergeEffectiveIntoDraft(
    mergeEffectiveIntoDraft(openInit, openTask, openRound, openSaved, null),
    openTask,
    openRound,
    openSaved,
    openEffective,
  )
  const effectiveFirst = mergeEffectiveIntoDraft(
    mergeEffectiveIntoDraft(openInit, openTask, openRound, null, openEffective),
    openTask,
    openRound,
    openSaved,
    openEffective,
  )
  assert.deepEqual(buildStartRoundPayload(savedFirst), buildStartRoundPayload(effectiveFirst))
})

test('formatRoundSettingsLabel formats agent, model, and raw reasoning level', () => {
  const labels = { cliDefault: 'CLI default', modelDefault: 'Model default' }
  assert.equal(
    formatRoundSettingsLabel(
      { backend: 'codex', model: 'gpt-6.2-sol', reasoning: 'xhigh' },
      BACKENDS,
      labels,
    ),
    'codex · gpt-6.2-sol · xhigh',
  )
  assert.equal(
    formatRoundSettingsLabel(
      { backend: 'claude-code', model: null, reasoning: null },
      BACKENDS,
      labels,
    ),
    'claude-code · CLI default · Model default',
  )
  // Copilot hides Model default reasoning because it does not support reasoning
  assert.equal(
    formatRoundSettingsLabel(
      { backend: 'copilot', model: null, reasoning: null },
      BACKENDS,
      labels,
    ),
    'copilot · CLI default',
  )
  assert.equal(normalizeReviewLang('zh-CN'), 'zh')
  assert.equal(normalizeReviewLang('en-US'), 'en')
  assert.equal(normalizeReviewLang(null), 'en')
})
