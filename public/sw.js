// Cache names
const CACHE_NAME = 'tip-genius-v1';
const DYNAMIC_CACHE = 'tip-genius-dynamic-v1';

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
});

// Activate event - cleanup old caches
self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys()
            .then(cacheNames => {
                return Promise.all(
                    cacheNames
                        .filter(name => name !== CACHE_NAME && name !== DYNAMIC_CACHE)
                        .map(name => caches.delete(name))
                );
            })
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

    // Handle API requests differently
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

    // Handle static assets
    event.respondWith(
        caches.match(request)
            .then(response => {
                if (response) {
                    return response;
                }
                return fetch(request)
                    .then(response => {
                        // Cache successful responses for static assets
                        if (response.status === 200) {
                            const responseToCache = response.clone();
                            caches.open(CACHE_NAME)
                                .then(cache => {
                                    cache.put(request, responseToCache);
                                });
                        }
                        return response;
                    });
            })
    );
});