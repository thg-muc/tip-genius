// Registers the service worker (public/sw.js) from both pages, so visitors who
// open the stats page first also get the offline copies.
//
// No reload or "update available" prompt: the worker loads pages and code
// network-first, so an online visitor always sees the deployed version, and a
// new worker takes over by itself (skipWaiting + clients.claim).
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch((error) => {
      console.warn('Service worker registration failed:', error)
    })
  })
}
