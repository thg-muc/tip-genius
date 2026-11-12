// Tests for public/sw.js: the file runs in a sandbox with an in-memory
// CacheStorage and a fake network. Run with `npm test`.

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { describe, mock, test } from 'node:test'
import vm from 'node:vm'

const ORIGIN = 'https://tip-genius.vercel.app'
const SOURCE = readFileSync(new URL('../../public/sw.js', import.meta.url), 'utf8')

// --- In-memory CacheStorage ----------------------------------------------------

function keyOf(input) {
  return typeof input === 'string' ? new URL(input, ORIGIN).href : input.url
}

class FakeCache {
  constructor() {
    this.entries = new Map()
  }
  async match(input) {
    const response = this.entries.get(keyOf(input))
    return response ? response.clone() : undefined
  }
  async put(input, response) {
    this.entries.set(keyOf(input), response)
  }
}

class FakeCacheStorage {
  constructor() {
    this.caches = new Map()
  }
  async open(name) {
    if (!this.caches.has(name)) this.caches.set(name, new FakeCache())
    return this.caches.get(name)
  }
  async keys() {
    return [...this.caches.keys()]
  }
  async delete(name) {
    return this.caches.delete(name)
  }
}

// --- Worker sandbox --------------------------------------------------------------

function loadWorker({ network }) {
  const listeners = {}
  const calls = { skipWaiting: 0, claim: 0, fetch: [] }
  const caches = new FakeCacheStorage()
  const self = {
    location: { origin: ORIGIN },
    addEventListener: (type, fn) => (listeners[type] = fn),
    skipWaiting: async () => calls.skipWaiting++,
    clients: { claim: async () => calls.claim++ },
  }
  const context = vm.createContext({
    self,
    caches,
    fetch: async (input, init) => {
      const url = keyOf(input)
      calls.fetch.push(url)
      return network(url, init)
    },
    URL,
    Response,
    Promise,
    Error,
    console: { warn: () => {}, log: () => {} },
    // Delegate so node:test mock timers apply inside the sandbox
    setTimeout: (...args) => setTimeout(...args),
    clearTimeout: (...args) => clearTimeout(...args),
  })
  vm.runInContext(SOURCE, context)
  return { listeners, calls, caches, self }
}

function makeEvent(request) {
  const event = {
    request,
    response: undefined,
    waits: [],
    respondWith(promise) {
      this.response = promise
    },
    waitUntil(promise) {
      this.waits.push(promise)
    },
    async settled() {
      // Background work (cache updates) may add more waitUntil promises
      for (let i = 0; i < this.waits.length; i++) await this.waits[i]
    },
  }
  return event
}

const request = (path, extra = {}) => ({
  url: new URL(path, ORIGIN).href,
  method: 'GET',
  mode: 'cors',
  ...extra,
})
const page = (path) => request(path, { mode: 'navigate' })
const ok = (body) => new Response(body, { status: 200 })
const offline = () => Promise.reject(new TypeError('Failed to fetch'))

async function dispatchFetch(worker, req) {
  const event = makeEvent(req)
  worker.listeners.fetch(event)
  if (!event.response) return { handled: false, event }
  const response = await event.response
  await event.settled()
  return { handled: true, event, response, body: await response.text() }
}

async function cacheEntries(worker) {
  const out = {}
  for (const [name, cache] of worker.caches.caches) out[name] = [...cache.entries.keys()]
  return out
}

// --- Tests -------------------------------------------------------------------------

describe('service worker', () => {
  test('install stores the pages and assets; a missing file does not stop it', async () => {
    const worker = loadWorker({
      network: async (url) =>
        url.endsWith('/dashboard') ? new Response('', { status: 404 }) : ok(url),
    })
    const event = makeEvent()
    worker.listeners.install(event)
    await event.settled()
    const stored = (await cacheEntries(worker))['tip-genius-pages-v2']
    assert.ok(stored.includes(`${ORIGIN}/`))
    assert.ok(stored.includes(`${ORIGIN}/css/tailwind.css`))
    assert.ok(!stored.includes(`${ORIGIN}/dashboard`))
    assert.equal(worker.calls.skipWaiting, 1)
  })

  test('activate deletes caches of older versions and takes control', async () => {
    const worker = loadWorker({ network: async (url) => ok(url) })
    await worker.caches.open('tip-genius-static-2501010000')
    await worker.caches.open('tip-genius-pages-v2')
    const event = makeEvent()
    worker.listeners.activate(event)
    await event.settled()
    assert.deepEqual(await worker.caches.keys(), ['tip-genius-pages-v2'])
    assert.equal(worker.calls.claim, 1)
  })

  test('online: pages come from the network and are stored without the query', async () => {
    const worker = loadWorker({ network: async () => ok('fresh page') })
    const cache = await worker.caches.open('tip-genius-pages-v2')
    await cache.put('/dashboard', ok('old page'))
    const result = await dispatchFetch(worker, page('/dashboard?league=bl'))
    assert.equal(result.body, 'fresh page')
    assert.equal(await (await cache.match('/dashboard')).text(), 'fresh page')
  })

  test('a page uses the navigation preload response instead of a second request', async () => {
    const worker = loadWorker({ network: async () => ok('second request') })
    const event = makeEvent(page('/'))
    event.preloadResponse = Promise.resolve(ok('preloaded page'))
    worker.listeners.fetch(event)
    assert.equal(await (await event.response).text(), 'preloaded page')
    await event.settled()
    assert.equal(worker.calls.fetch.length, 0)
  })

  test('activate turns on navigation preload where the browser has it', async () => {
    const worker = loadWorker({ network: async (url) => ok(url) })
    let enabled = false
    worker.self.registration = { navigationPreload: { enable: async () => (enabled = true) } }
    const event = makeEvent()
    worker.listeners.activate(event)
    await event.settled()
    assert.equal(enabled, true)
  })

  test('offline: the stored copy of the page is served', async () => {
    const worker = loadWorker({ network: offline })
    const cache = await worker.caches.open('tip-genius-pages-v2')
    await cache.put('/dashboard', ok('stored stats page'))
    const result = await dispatchFetch(worker, page('/dashboard?view=hits'))
    assert.equal(result.body, 'stored stats page')
  })

  test('offline, page never stored: the tips page is served instead', async () => {
    const worker = loadWorker({ network: offline })
    const cache = await worker.caches.open('tip-genius-pages-v2')
    await cache.put('/', ok('stored tips page'))
    const result = await dispatchFetch(worker, page('/dashboard.html'))
    assert.equal(result.body, 'stored tips page')
  })

  test('offline, nothing stored: the request fails as without a worker', async () => {
    const worker = loadWorker({ network: offline })
    const event = makeEvent(request('/js/main.js'))
    worker.listeners.fetch(event)
    await assert.rejects(event.response)
  })

  test('very slow network: the stored copy after 4 s, the cache still updates', async () => {
    mock.timers.enable({ apis: ['setTimeout'] })
    try {
      let answer
      const worker = loadWorker({
        network: () => new Promise((resolve) => (answer = resolve)),
      })
      const cache = await worker.caches.open('tip-genius-pages-v2')
      await cache.put('/js/main.js', ok('stored script'))
      const event = makeEvent(request('/js/main.js'))
      worker.listeners.fetch(event)
      await new Promise((resolve) => setImmediate(resolve))
      mock.timers.tick(4000)
      assert.equal(await (await event.response).text(), 'stored script')
      answer(ok('new script'))
      await event.settled()
      assert.equal(await (await cache.match('/js/main.js')).text(), 'new script')
    } finally {
      mock.timers.reset()
    }
  })

  test('API answers are network first and stored per query', async () => {
    const worker = loadWorker({ network: async (url) => ok(`answer for ${url}`) })
    const path = '/api/predictions?key=Match_Predictions_Deepseek-Chat'
    const online = await dispatchFetch(worker, request(path))
    assert.equal(online.body, `answer for ${ORIGIN}${path}`)
    const stored = (await cacheEntries(worker))['tip-genius-pages-v2']
    assert.ok(stored.includes(`${ORIGIN}${path}`))
  })

  test('images: the stored copy at once, refreshed in the background', async () => {
    const worker = loadWorker({ network: async () => ok('new logo') })
    const cache = await worker.caches.open('tip-genius-images-v2')
    const logo = request('/images/leagues/LaLiga-Spain.png')
    await cache.put(logo, ok('old logo'))
    const result = await dispatchFetch(worker, logo)
    assert.equal(result.body, 'old logo')
    assert.equal(await (await cache.match(logo)).text(), 'new logo')
  })

  test('Chart.js from jsDelivr is fetched once and then served from the cache', async () => {
    const worker = loadWorker({ network: async () => ok('chart.js code') })
    const chart = {
      url: 'https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.js',
      method: 'GET',
      mode: 'cors',
    }
    await dispatchFetch(worker, chart)
    const second = await dispatchFetch(worker, chart)
    assert.equal(second.body, 'chart.js code')
    assert.equal(worker.calls.fetch.length, 1)
  })

  test('other origins, non-GET requests and version.json are left alone', async () => {
    const worker = loadWorker({ network: async () => ok('') })
    for (const req of [
      { url: 'https://va.vercel-scripts.com/v1/script.js', method: 'GET', mode: 'cors' },
      request('/api/stats', { method: 'POST' }),
      request('/version.json?v=123'),
    ]) {
      const { handled } = await dispatchFetch(worker, req)
      assert.equal(handled, false, req.url)
    }
  })

  test('error answers are passed on but never stored', async () => {
    const worker = loadWorker({ network: async () => new Response('down', { status: 503 }) })
    const result = await dispatchFetch(worker, request('/api/stats'))
    assert.equal(result.response.status, 503)
    const stored = (await cacheEntries(worker))['tip-genius-pages-v2'] || []
    assert.equal(stored.length, 0)
  })
})
