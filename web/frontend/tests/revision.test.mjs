import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { webcrypto } from 'node:crypto'
import test from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/revision.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
})
const { createRevision } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)

test('works when getRandomValues exists but randomUUID does not (LAN HTTP)', () => {
  const lanCrypto = { getRandomValues: webcrypto.getRandomValues.bind(webcrypto) }
  const values = Array.from({ length: 1000 }, () => createRevision(lanCrypto))
  assert.equal(new Set(values).size, 1000)
  for (const value of values) assert.match(value, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
})

test('sets UUID version and variant bits', () => {
  assert.equal(createRevision({ getRandomValues: bytes => bytes.fill(0) }),
    '00000000-0000-4000-8000-000000000000')
})

test('does not silently use insecure randomness if Web Crypto is unavailable', () => {
  assert.throws(() => createRevision({}), /Web Crypto/)
})
