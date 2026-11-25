import assert from 'node:assert/strict'
import { test } from 'node:test'

import { resolveUiMode } from '../src/uiMode.ts'

test('resolveUiMode defaults to next and supports explicit next/legacy overrides', () => {
  assert.equal(resolveUiMode(''), 'next')
  assert.equal(resolveUiMode('?ui=next'), 'next')
  assert.equal(resolveUiMode('?foo=1&ui=next'), 'next')
  assert.equal(resolveUiMode('?ui=legacy'), 'legacy')
  assert.equal(resolveUiMode('?ui=classic'), 'legacy')
  assert.equal(resolveUiMode('?ui=unknown'), 'next')
})
