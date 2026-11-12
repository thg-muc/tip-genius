// Service worker for the Tip Genius PWA: offline copies, never stale pages.
//
// - Pages, scripts, styles, the manifest and the API: network first. Online
//   visitors always get the deployed files (no mix of an old page with new
//   CSS after a deploy); the stored copy is used when the network fails, or
//   after NETWORK_TIMEOUT_MS on a very slow connection
// - Images (logos, icons): served from the cache at once, refreshed in the
//   background
// - Chart.js from jsDelivr: the URL names a fixed version and never changes,
//   so it is cached for good
// - Everything else (other origins, non-GET, version.json) is not touched
//
// Bump CACHE_VERSION when this file changes in a way that needs fresh caches;
// old caches are deleted on activation.

const CACHE_VERSION = 'v2'
const PAGES_CACHE = `tip-genius-pages-${CACHE_VERSION}`
const IMAGES_CACHE = `tip-genius-images-${CACHE_VERSION}`
const CDN_CACHE = `tip-genius-cdn-${CACHE_VERSION}`
const CACHES = [PAGES_CACHE, IMAGES_CACHE, CDN_CACHE]

// Network wait before a stored copy is used instead (only when there is one)
const NETWORK_TIMEOUT_MS = 4000

// Stored at install, so both pages also open offline after the first visit.
// One missing file does not stop the install (each is added on its own)
const PRECACHE = [
  '/',
  '/dashboard',
  '/css/tailwind.css',
  '/js/models.js',
  '/js/main.js',
  '/js/dashboard-data.js',
  '/js/register-sw.js',
  '/manifest.json',
  '/images/icon-128x128.png',
  '/images/icon-256x256.png',
  '/images/icon-512x512.png',
]

const CDN_ORIGIN = 'https://cdn.jsdelivr.net'

self.addEventListener('install', (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(PAGES_CACHE)
      const results = await Promise.allSettled(
        PRECACHE.map(async (path) => {
          const response = await fetch(path, { cache: 'no-cache' })
          if (!isStorable(response)) throw new Error(`${path}: HTTP ${response.status}`)
          await cache.put(path, response)
        })
      )
      const failed = results.filter((r) => r.status === 'rejected')
      if (failed.length) {
        console.warn(
          'Service worker: not precached:',
          failed.map((r) => r.reason.message)
        )
      }
      // Activate right away: the strategies below never serve stale code to
      // online visitors, so there is nothing to wait for
      await self.skipWaiting()
    })()
  )
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys()
      await Promise.all(
        names
          .filter((name) => !CACHES.includes(name))
          .map((name) => caches.delete(name))
      )
      // The browser starts a page's request while the worker boots
      // (navigation preload); networkFirst uses that response
      if (self.registration?.navigationPreload) {
        await self.registration.navigationPreload.enable()
      }
      await self.clients.claim()
    })()
  )
})

self.addEventListener('fetch', (event) => {
  const { request } = event
  if (request.method !== 'GET') return
  const url = new URL(request.url)

  if (url.origin === CDN_ORIGIN && url.pathname.startsWith('/npm/chart.js@')) {
    event.respondWith(cacheFirst(request))
    return
  }
  if (url.origin !== self.location.origin) return
  if (url.pathname === '/version.json' || url.pathname === '/sw.js') return

  if (url.pathname.startsWith('/images/')) {
    event.respondWith(staleWhileRevalidate(event, request))
    return
  }
  if (request.mode === 'navigate') {
    // One stored copy per page: the query string only sets filters
    event.respondWith(networkFirst(event, request, url.pathname, '/'))
    return
  }
  event.respondWith(networkFirst(event, request, request))
})

// Only complete, direct answers are stored (a redirected response must not be
// served for a navigation, and opaque or error responses are useless offline)
function isStorable(response) {
  return response.ok && !response.redirected && response.type !== 'opaque'
}

async function networkFirst(event, request, key, fallbackKey) {
  // The request starts before anything else; a page navigation takes the
  // response the browser already requested while the worker started
  const cachePromise = caches.open(PAGES_CACHE)
  const network = (async () => {
    const preloaded =
      request.mode === 'navigate' ? await event.preloadResponse : undefined
    const response = preloaded || (await fetch(request))
    if (isStorable(response)) {
      const cache = await cachePromise
      event.waitUntil(cache.put(key, response.clone()))
    }
    return response
  })()
  const cache = await cachePromise
  const cached = await cache.match(key)

  if (!cached) {
    try {
      return await network
    } catch (error) {
      const fallback = fallbackKey && (await cache.match(fallbackKey))
      if (fallback) return fallback
      throw error
    }
  }

  // A stored copy exists: use it when the network fails or is very slow; the
  // network answer still updates the cache in the background
  let timer
  const slow = new Promise((resolve) => {
    timer = setTimeout(() => resolve(cached), NETWORK_TIMEOUT_MS)
  })
  event.waitUntil(network.catch(() => undefined))
  try {
    return await Promise.race([network.catch(() => cached), slow])
  } finally {
    clearTimeout(timer)
  }
}

async function staleWhileRevalidate(event, request) {
  const cache = await caches.open(IMAGES_CACHE)
  const cached = await cache.match(request)
  const network = fetch(request).then((response) => {
    if (isStorable(response)) {
      event.waitUntil(cache.put(request, response.clone()))
    }
    return response
  })
  if (cached) {
    event.waitUntil(network.catch(() => undefined))
    return cached
  }
  return network
}

async function cacheFirst(request) {
  const cache = await caches.open(CDN_CACHE)
  const cached = await cache.match(request)
  if (cached) return cached
  const response = await fetch(request)
  if (isStorable(response)) await cache.put(request, response.clone())
  return response
}
