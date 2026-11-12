// Tests for api/stats.js with a mocked KV (fetch) and a minimal response
// object. Run with `npm test` (Node's built-in test runner).

import assert from 'node:assert/strict'
import { afterEach, beforeEach, describe, test } from 'node:test'

const { default: handler } = await import('../../api/stats.js')

const STATS = { version: 1, meta: {}, models: [], slices: {}, recent: {} }
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

function request({ method = 'GET', origin } = {}) {
  return { method, headers: origin ? { origin } : {}, query: {} }
}

// fetch stub that answers like the Upstash REST API
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

describe('api/stats', () => {
  beforeEach(() => {
    process.env.KV_REST_API_URL = 'https://kv.example'
    process.env.KV_REST_API_READ_ONLY_TOKEN = 'read-only-test-token'
  })
  afterEach(() => {
    globalThis.fetch = realFetch
    delete process.env.KV_REST_API_URL
    delete process.env.KV_REST_API_READ_ONLY_TOKEN
  })

  test('returns the parsed JSON with the cache header', async () => {
    const calls = kvReturns(200, { result: JSON.stringify(STATS) })
    const res = mockResponse()
    await handler(request(), res)
    assert.equal(res.statusCode, 200)
    assert.deepEqual(res.body, STATS)
    assert.match(res.headers['cache-control'], /s-maxage=3600/)
    assert.match(res.headers['cache-control'], /max-age=300/)
    assert.equal(calls[0].url, 'https://kv.example/get/dashboard_stats')
    assert.equal(
      calls[0].options.headers.Authorization,
      'Bearer read-only-test-token'
    )
  })

  test('refuses query parameters without reading KV (no cache busting)', async () => {
    const calls = kvReturns(200, { result: JSON.stringify(STATS) })
    const req = request()
    req.query = { key: 'Match_Predictions_secret' }
    const res = mockResponse()
    await handler(req, res)
    assert.equal(res.statusCode, 400)
    assert.equal(res.headers['cache-control'], 'no-store')
    assert.equal(calls.length, 0)
  })

  test('404 without caching when the key is missing', async () => {
    kvReturns(200, { result: null })
    const res = mockResponse()
    await handler(request(), res)
    assert.equal(res.statusCode, 404)
    assert.equal(res.headers['cache-control'], 'no-store')
  })

  test('502 when KV fails', async () => {
    kvReturns(401, { error: 'Unauthorized' })
    const res = mockResponse()
    await handler(request(), res)
    assert.equal(res.statusCode, 502)
    assert.equal(res.headers['cache-control'], 'no-store')
  })

  test('502 for a value that is not the stats JSON', async () => {
    kvReturns(200, { result: JSON.stringify({ hello: 'world' }) })
    const res = mockResponse()
    await handler(request(), res)
    assert.equal(res.statusCode, 502)
    assert.equal(res.headers['cache-control'], 'no-store')
  })

  test('500 for a value that is not JSON at all', async () => {
    kvReturns(200, { result: '{broken' })
    const res = mockResponse()
    await handler(request(), res)
    assert.equal(res.statusCode, 500)
    assert.equal(res.headers['cache-control'], 'no-store')
  })

  test('500 when the KV variables are missing', async () => {
    delete process.env.KV_REST_API_READ_ONLY_TOKEN
    const calls = kvReturns(200, { result: JSON.stringify(STATS) })
    const res = mockResponse()
    await handler(request(), res)
    assert.equal(res.statusCode, 500)
    assert.equal(calls.length, 0)
  })

  test('only GET is allowed; OPTIONS answers the preflight', async () => {
    kvReturns(200, { result: JSON.stringify(STATS) })
    const post = mockResponse()
    await handler(request({ method: 'POST' }), post)
    assert.equal(post.statusCode, 405)
    const options = mockResponse()
    await handler(request({ method: 'OPTIONS' }), options)
    assert.equal(options.statusCode, 200)
  })

  test('CORS header only for allowed origins', async () => {
    kvReturns(200, { result: JSON.stringify(STATS) })
    const allowed = mockResponse()
    await handler(request({ origin: 'https://tip-genius.vercel.app' }), allowed)
    assert.equal(
      allowed.headers['access-control-allow-origin'],
      'https://tip-genius.vercel.app'
    )
    assert.equal(allowed.headers.vary, 'Origin')
    for (const origin of ['https://example.com', 'https://tip-genius.vercel.app.evil.com']) {
      const other = mockResponse()
      await handler(request({ origin }), other)
      assert.equal(other.headers['access-control-allow-origin'], undefined, origin)
    }
  })
})
