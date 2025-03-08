// Cache version management
const VERSION = '2.0.0'
const CACHE_NAME = `tip-genius-static-${VERSION}`
const DYNAMIC_CACHE = `tip-genius-dynamic-${VERSION}`

// Assets to cache
const STATIC_ASSETS = [
  // Core app files
  '/',
  '/index.html',
  '/manifest.json',
  '/js/main.js',
  '/sw.js',

  // Image directories (will be matched by pattern)
  '/images/icon-', // Will match all icon sizes
  '/images/leagues/',
  '/images/llm-logos/',

  // External dependencies
  'https://cdn.tailwindcss.com'
]

// Cache patterns for image directories
const IMAGE_PATTERNS = [
  new RegExp('^/images/icon-.*\\.png$'),
  new RegExp('^/images/leagues/.*\\.png$'),
  new RegExp('^/images/llm-logos/.*\\.png$'),
  new RegExp('^/images/teams/.*\\.png$')
]

// Function to clean up old caches
const deleteOldCaches = async () => {
  const cacheKeepList = [CACHE_NAME, DYNAMIC_CACHE]
  const keyList = await caches.keys()
  const cachesToDelete = keyList.filter(key => !cacheKeepList.includes(key))
  return Promise.all(cachesToDelete.map(key => caches.delete(key)))
}

// Helper function to handle API responses
async function handleApiResponse (request, response) {
  const cache = await caches.open(DYNAMIC_CACHE)
  await cache.put(request, response.clone())
  return response
}

// Helper function to handle offline fallback
async function handleOfflineFallback (request) {
  // If it's a page navigation, return index.html
  if (request.mode === 'navigate') {
    const cache = await caches.open(CACHE_NAME)
    return cache.match('/index.html')
  }
  // For other requests, throw error
  throw new Error('Network and cache unavailable')
}

// Install event - cache static assets
self.addEventListener('install', event => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then(cache => {
        console.log('Caching static assets')
        return cache.addAll(STATIC_ASSETS)
      })
      .catch(error => {
        console.error('Error caching static assets:', error)
      })
  )
  // Force the waiting service worker to become the active service worker
  self.skipWaiting()
})

// Activate event - cleanup old caches and claim clients
self.addEventListener('activate', event => {
  event.waitUntil(
    Promise.all([
      deleteOldCaches(),
      // Force new service worker to take control immediately
      self.clients.claim()
    ])
  )
})

// Fetch event - handle requests
self.addEventListener('fetch', event => {
  const { request } = event
  const url = new URL(request.url)

  // Special handling for team logos - use cache-first strategy
  if (url.pathname.startsWith('/images/teams/')) {
    event.respondWith(
      (async () => {
        // Check cache first
        const cachedResponse = await caches.match(request)
        if (cachedResponse) {
          return cachedResponse
        }

        // If not in cache, fetch from network and cache for long term
        try {
          const networkResponse = await fetch(request)
          if (networkResponse.status === 200) {
            const cache = await caches.open(CACHE_NAME)
            cache.put(request, networkResponse.clone())
          }
          return networkResponse
        } catch (error) {
          console.error('Error fetching team logo:', error)
          return new Response('', { status: 404 })
        }
      })()
    )
    return
  }

  // Handle static assets with improved cache-first strategy
  event.respondWith(
    (async () => {
      try {
        // Check if request matches any image pattern
        const isImageRequest = IMAGE_PATTERNS.some(pattern =>
          pattern.test(url.pathname)
        )

        // Try to get from cache first
        const cachedResponse = await caches.match(request)

        // Start fetch in background for cache update
        const networkResponsePromise = fetch(request).then(async response => {
          // Cache the new version if it's a successful response
          if (
            response.status === 200 &&
            (STATIC_ASSETS.includes(url.pathname) || isImageRequest)
          ) {
            const cache = await caches.open(CACHE_NAME)
            await cache.put(request, response.clone())
          }
          return response
        })

        // If we have a cached response, use it but still update cache in background
        if (cachedResponse) {
          networkResponsePromise.catch(console.error)
          return cachedResponse
        }

        // If no cached response, wait for the network response
        const networkResponse = await networkResponsePromise
        return networkResponse
      } catch (error) {
        // If network fails and we have a cached version, use it
        const cachedResponse = await caches.match(request)
        if (cachedResponse) return cachedResponse

        // If nothing works, try offline fallback
        return handleOfflineFallback(request)
      }
    })()
  )
})
