// Run with: npm test   (from frontend/; Node >= 22.18 strips types natively)
//
// Checks the interface dictionaries and the language preference: English by
// default, the same keys in every language, and a switch that survives a
// reload. Storage is passed in, the same way App reads and writes it.
import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  DEFAULT_LANGUAGE,
  DEFAULT_REVIEW_LANGUAGE,
  LANGUAGES,
  LANGUAGE_STORAGE_KEY,
  errorMessage,
  isLanguage,
  labelFor,
  messages,
  otherLanguage,
  readStoredLanguage,
  storeLanguage,
  verbatim,
  type Language,
} from '../src/i18n.ts'

/** In-memory stand-in for window.localStorage. */
const memoryStorage = (initial: Record<string, string> = {}) => {
  const items = new Map<string, string>(Object.entries(initial))
  return {
    items,
    getItem: (key: string) => items.get(key) ?? null,
    setItem: (key: string, value: string) => {
      items.set(key, String(value))
    },
  }
}

/** Every leaf of a dictionary, keyed by its dotted path. */
const leaves = (node: object, prefix = ''): Map<string, unknown> => {
  const out = new Map<string, unknown>()
  for (const [key, value] of Object.entries(node)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (value !== null && typeof value === 'object') {
      for (const [childPath, child] of leaves(value, path)) out.set(childPath, child)
    } else {
      out.set(path, value)
    }
  }
  return out
}

type Template = (...args: unknown[]) => unknown

/** A leaf as the UI shows it. Templates get distinct sample arguments. */
const render = (leaf: unknown): { text: unknown; args: number[] } => {
  if (typeof leaf !== 'function') return { text: leaf, args: [] }
  const args = Array.from({ length: leaf.length }, (_, i) => 41 + 16 * i)
  return { text: (leaf as Template)(...args), args }
}

const codes = LANGUAGES.map((option) => option.code)

test('English is the default interface language', () => {
  assert.equal(DEFAULT_LANGUAGE, 'en')
  const storage = memoryStorage()
  assert.equal(readStoredLanguage(storage), 'en')
  // Reading never writes, so only an explicit choice is ever stored.
  assert.equal(storage.items.size, 0)
  assert.equal(readStoredLanguage(null), 'en')
  // No window here, as during a server-side or test import.
  assert.equal(readStoredLanguage(), 'en')
  assert.doesNotThrow(() => storeLanguage('zh-CN'))
})

test('the switch lists each language once, named in that language', () => {
  assert.deepEqual(
    LANGUAGES.map(({ code, label }) => [code, label]),
    [
      ['en', 'EN'],
      ['zh-CN', '中文'],
    ],
  )
  assert.deepEqual(Object.keys(messages).sort(), [...codes].sort())
})

test('every language defines exactly the keys English defines', () => {
  const reference = leaves(messages.en)
  // The walk reaches nested groups.
  assert.ok(reference.has('settings.modelSource.unreadable'))
  assert.ok(reference.has('verdict.inconclusive'))

  for (const code of codes) {
    const other = leaves(messages[code])
    const missing = [...reference.keys()].filter((path) => !other.has(path))
    const extra = [...other.keys()].filter((path) => !reference.has(path))
    assert.deepEqual({ code, missing, extra }, { code, missing: [], extra: [] })

    for (const [path, value] of reference) {
      const counterpart = other.get(path)
      assert.equal(typeof counterpart, typeof value, `${code} ${path}: kind`)
      if (typeof value === 'function') {
        assert.equal((counterpart as Template).length, value.length, `${code} ${path}: argument count`)
      }
    }
  }
})

test('every entry renders as non-empty text with its arguments filled in', () => {
  for (const code of codes) {
    for (const [path, leaf] of leaves(messages[code])) {
      const { text, args } = render(leaf)
      const where = `${code} ${path}: ${JSON.stringify(text)}`
      assert.equal(typeof text, 'string', where)
      const shown = text as string
      assert.notEqual(shown.trim(), '', where)
      assert.doesNotMatch(shown, /undefined|NaN|\[object|\$\{/, where)
      for (const arg of args) assert.ok(shown.includes(String(arg)), `${where} is missing ${arg}`)
    }
  }
})

test('no entry contains an emoji', () => {
  for (const code of codes) {
    for (const [path, leaf] of leaves(messages[code])) {
      const text = String(render(leaf).text)
      assert.doesNotMatch(text, /\p{Extended_Pictographic}/u, `${code} ${path}: ${text}`)
    }
  }
})

test('the English dictionary contains no Chinese text', () => {
  for (const [path, leaf] of leaves(messages.en)) {
    const text = String(render(leaf).text)
    assert.doesNotMatch(text, /\p{Script=Han}/u, `en ${path}: ${text}`)
  }
})

test('key terms follow the glossary in redesign-draft.md section 5', () => {
  const en = messages.en
  const zh = messages['zh-CN']
  const terms: [string, string, string][] = [
    [en.verdict.clear, zh.verdict.clear, 'No issues found|未发现明显问题'],
    [en.verdict.concerns, zh.verdict.concerns, 'Needs attention|需要关注'],
    [en.verdict.failed, zh.verdict.failed, 'Review failed|审查失败'],
    [en.verdict.inconclusive, zh.verdict.inconclusive, 'Inconclusive|结论不明确'],
    [en.severity.critical, zh.severity.critical, 'Critical|严重'],
    [en.severity.high, zh.severity.high, 'High|高'],
    [en.severity.medium, zh.severity.medium, 'Medium|中'],
    [en.severity.low, zh.severity.low, 'Low|低'],
    [en.newReview.localChanges, zh.newReview.localChanges, 'Local changes|本地变更'],
    [en.newReview.pullRequest, zh.newReview.pullRequest, 'Pull request|GitHub PR'],
    [en.settings.title, zh.settings.title, 'Model selection|模型选择'],
    [en.topbar.modelSelection, zh.topbar.modelSelection, 'Model selection|模型选择'],
    [en.detail.cancelRound, zh.detail.cancelRound, 'Cancel round|取消本轮'],
    [en.stage.preflight, zh.stage.preflight, 'Checking agent|检查 Agent'],
    // A cancelled round is labelled as cancelled, not as an error.
    [en.status.aborted, zh.status.aborted, 'Cancelled|已取消'],
    [en.rounds.title(2), zh.rounds.title(2), 'Round 2|第 2 轮'],
    [en.counts.findings(2), zh.counts.findings(2), '2 findings|2 条问题'],
  ]
  for (const [enText, zhText, expected] of terms) {
    assert.equal(`${enText}|${zhText}`, expected)
  }
  for (const code of codes) {
    for (const [path, leaf] of leaves(messages[code])) {
      assert.ok(!String(render(leaf).text).includes('智能模型选择器'), `${code} ${path}`)
    }
  }
})

test('English counts use the singular for exactly one', () => {
  const { counts } = messages.en
  assert.equal(counts.findings(0), '0 findings')
  assert.equal(counts.findings(1), '1 finding')
  assert.equal(counts.findings(12), '12 findings')
  assert.equal(messages.en.settings.modelsFound(1), '1 model found')
  assert.equal(messages['zh-CN'].counts.findings(1), '1 条问题')
})

test('local changes without a base branch say it is auto-detected', () => {
  assert.equal(messages.en.source.localChanges('main'), 'Local changes vs main')
  assert.equal(messages.en.source.localChanges(null), 'Local changes · base auto-detected')
  assert.equal(messages['zh-CN'].source.localChanges(null), '本地变更 · 自动基准')
})

test('switching language is stored and survives a reload', () => {
  const storage = memoryStorage()
  // The same steps as App: read on load, store on each click of the switch.
  let language: Language = readStoredLanguage(storage)
  assert.equal(language, 'en')

  const offer = otherLanguage(language)
  assert.equal(offer.code, 'zh-CN')
  language = offer.code
  storeLanguage(language, storage)
  assert.equal(LANGUAGE_STORAGE_KEY, 'amc_ui_language')
  assert.equal(storage.getItem('amc_ui_language'), 'zh-CN')
  assert.equal(readStoredLanguage(storage), 'zh-CN', 'after a reload')
  assert.equal(messages[readStoredLanguage(storage)].settings.title, '模型选择')

  language = otherLanguage(language).code
  storeLanguage(language, storage)
  assert.equal(language, 'en')
  assert.equal(readStoredLanguage(storage), 'en', 'after switching back')
  assert.deepEqual([...storage.items.keys()], ['amc_ui_language'])
})

test('an unrecognised stored value falls back to English', () => {
  for (const value of ['zh', 'zh-cn', 'EN', ' en', 'fr', '', 'null', '{"lang":"zh-CN"}']) {
    const storage = memoryStorage({ [LANGUAGE_STORAGE_KEY]: value })
    assert.equal(readStoredLanguage(storage), 'en', JSON.stringify(value))
  }
  assert.equal(isLanguage('zh-CN'), true)
  assert.equal(isLanguage('zh'), false)
  assert.equal(isLanguage(undefined), false)
})

test('blocked storage leaves the language usable', () => {
  const blocked = {
    getItem: () => {
      throw new Error('SecurityError')
    },
    setItem: () => {
      throw new Error('QuotaExceededError')
    },
  }
  assert.equal(readStoredLanguage(blocked), 'en')
  assert.doesNotThrow(() => storeLanguage('zh-CN', blocked))
})

test('the review output language defaults to English, in the backend vocabulary', () => {
  // A separate setting from the interface language. The backend's
  // start_task accepts 'en' | 'zh'.
  assert.equal(DEFAULT_REVIEW_LANGUAGE, 'en')
  assert.ok(['en', 'zh'].includes(DEFAULT_REVIEW_LANGUAGE))
})

test('a server message is shown as sent; the fallback follows the language', () => {
  const fallback = errorMessage('not an Error', (t) => t.errors.startRound)
  assert.equal(fallback(messages.en), "Couldn't start the round")
  assert.equal(fallback(messages['zh-CN']), '启动审查失败')

  const fromServer = errorMessage(new Error('task is not actively running'), (t) => t.errors.cancelRound)
  for (const code of codes) assert.equal(fromServer(messages[code]), 'task is not actively running')

  const empty = errorMessage(new Error(''), (t) => t.errors.exportSummary)
  assert.equal(empty(messages.en), "Couldn't export the summary")
  assert.equal(verbatim('/tmp/summary.md')(messages['zh-CN']), '/tmp/summary.md')
})

test('a value the dictionary does not know is shown as is', () => {
  const { stage } = messages.en
  assert.equal(labelFor(stage, 'review_done'), 'Review complete')
  assert.equal(labelFor(messages['zh-CN'].stage, 'review_done'), '审查完成')
  for (const key of ['some_new_stage', 'constructor', '__proto__', 'toString', 'hasOwnProperty']) {
    assert.equal(labelFor(stage, key), key)
  }
})
