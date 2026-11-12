// Tests for api/predictions.js (on the shared handling in api/_shared.js)
// with a mocked KV. Run with `npm test`.

import assert from 'node:assert/strict'
import { afterEach, beforeEach, describe, test } from 'node:test'

const { default: handler } = await import('../../api/predictions.js')
const realFetch = globalThis.fetch

function mockResponse() {
  return {
    statusCode: null,
    headers: {},
    body: undefined,
    setHeader(name, value) {
      this.headers[name.toLowerCase()] = value
    },
    status(code) {
      this.statusCode = code
      return this
    },
    json(body) {
      this.body = body
      return this
    },
    end() {
      return this
    },
  }
}

function kvReturns(status, body) {
  const calls = []
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options })
    return {
      ok: status >= 200 && status < 300,
      status,
      json: async () => body,
      text: async () => JSON.stringify(body),
    }
  }
  return calls
}

describe('api/predictions', () => {
  beforeEach(() => {
    process.env.KV_REST_API_URL = 'https://kv.example'
    process.env.KV_REST_API_READ_ONLY_TOKEN = 'read-only-test-token'
    process.env.KV_DEFAULT_KEY = 'Default_Key'
  })
  afterEach(() => {
    globalThis.fetch = realFetch
    delete process.env.KV_REST_API_URL
    delete process.env.KV_REST_API_READ_ONLY_TOKEN
    delete process.env.KV_DEFAULT_KEY
  })

  test('reads the requested key, else the default key', async () => {
    const calls = kvReturns(200, { result: '[]' })
    const res = mockResponse()
    await handler({ method: 'GET', headers: {}, query: { key: 'Some_Key' } }, res)
    await handler({ method: 'GET', headers: {}, query: {} }, mockResponse())
    assert.equal(res.statusCode, 200)
    assert.deepEqual(res.body, { result: '[]' })
    assert.match(res.headers['cache-control'], /s-maxage=300/)
    assert.equal(calls[0].url, 'https://kv.example/get/Some_Key')
    assert.equal(calls[1].url, 'https://kv.example/get/Default_Key')
  })

  test('passes a KV error status through', async () => {
    kvReturns(404, null)
    const res = mockResponse()
    await handler({ method: 'GET', headers: {}, query: {} }, res)
    assert.equal(res.statusCode, 404)
    assert.equal(res.body.error, 'No predictions found')
  })

  test('405 for other methods, 500 without configuration', async () => {
    const calls = kvReturns(200, { result: '[]' })
    const post = mockResponse()
    await handler({ method: 'POST', headers: {}, query: {} }, post)
    assert.equal(post.statusCode, 405)
    delete process.env.KV_DEFAULT_KEY
    const res = mockResponse()
    await handler({ method: 'GET', headers: {}, query: {} }, res)
    assert.equal(res.statusCode, 500)
    assert.equal(calls.length, 0)
  })

  test('CORS header only for an exactly allowed origin', async () => {
    kvReturns(200, { result: '[]' })
    const local = mockResponse()
    await handler({ method: 'GET', headers: { origin: 'http://localhost:3000' }, query: {} }, local)
    assert.equal(local.headers['access-control-allow-origin'], 'http://localhost:3000')
    const lookalike = mockResponse()
    await handler(
      { method: 'GET', headers: { origin: 'http://localhost:30000' }, query: {} },
      lookalike
    )
    assert.equal(lookalike.headers['access-control-allow-origin'], undefined)
  })
})
