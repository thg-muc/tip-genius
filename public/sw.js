// Cache version management
const VERSION = '2.0.0';
const CACHE_NAME = `tip-genius-static-${VERSION}`;
const DYNAMIC_CACHE = `tip-genius-dynamic-${VERSION}`;

// Assets to cache on install
const STATIC_ASSETS = [
    '/',
    '/index.html',
    '/manifest.json',
    '/images/icon-128x128.png',
    '/images/icon-256x256.png',
    '/images/icon-512x512.png',
    'https://cdn.tailwindcss.com'
];

// Function to clean up old caches
const deleteOldCaches = async () => {
    const cacheKeepList = [CACHE_NAME, DYNAMIC_CACHE];
    const keyList = await caches.keys();
    const cachesToDelete = keyList.filter(key => !cacheKeepList.includes(key));
    return Promise.all(cachesToDelete.map(key => caches.delete(key)));
};

// Install event - cache static assets
self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME)
            .then(cache => {
                console.log('Caching static assets');
                return cache.addAll(STATIC_ASSETS);
            })
            .catch(error => {
                console.error('Error caching static assets:', error);
            })
    );
    // Force the waiting service worker to become the active service worker
    self.skipWaiting();
});

// Activate event - cleanup old caches and claim clients
self.addEventListener('activate', event => {
    event.waitUntil(
        Promise.all([
            deleteOldCaches(),
            // Force new service worker to take control immediately
            self.clients.claim()
        ])
    );
});

// Helper function to handle API responses
async function handleApiResponse(request, response) {
    const cache = await caches.open(DYNAMIC_CACHE);
    await cache.put(request, response.clone());
    return response;
}

// Fetch event - handle requests
self.addEventListener('fetch', event => {
    const { request } = event;
    const url = new URL(request.url);

    // Handle API requests differently (network-first strategy)
    if (request.url.includes('/api/predictions')) {
        event.respondWith(
            fetch(request)
                .then(response => handleApiResponse(request, response))
                .catch(async () => {
                    const cachedResponse = await caches.match(request);
                    if (cachedResponse) {
                        // Send message to client about using cached data
                        const clients = await self.clients.matchAll();
                        clients.forEach(client => {
                            client.postMessage({
                                type: 'USING_CACHED_DATA',
                                timestamp: new Date().toISOString()
                            });
                        });
                        return cachedResponse;
                    }
                    throw new Error('No cached data available');
                })
        );
        return;
    }

    // Handle static assets with stale-while-revalidate strategy
    event.respondWith(
        (async () => {
            // Try to get from cache first
            const cachedResponse = await caches.match(request);
            
            // Start fetch in background
            const networkResponsePromise = fetch(request).then(
                async response => {
                    // Cache the new version if it's a successful response
                    if (response.status === 200) {
                        const cache = await caches.open(CACHE_NAME);
                        cache.put(request, response.clone());
                    }
                    return response;
                }
            );

            try {
                // If we have a cached response, use it but still update cache in background
                if (cachedResponse) {
                    networkResponsePromise.catch(console.error);
                    return cachedResponse;
                }
                
                // If no cached response, wait for the network response
                const networkResponse = await networkResponsePromise;
                return networkResponse;
                
            } catch (error) {
                // If network fails and we have a cached version, use it
                if (cachedResponse) return cachedResponse;
                
                // Otherwise, propagate the error
                throw error;
            }
        })()
    );
});