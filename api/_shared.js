// api/_shared.js
//
// Request handling shared by the API routes (files starting with "_" are not
// deployed as routes by Vercel).

// Origins that may call the API from another site (same-origin requests from
// the deployed pages need no CORS header)
export const ALLOWED_ORIGINS = [
  'http://localhost:3000',
  'http://localhost:5000',
  'http://localhost:8000',
  'https://tip-genius.vercel.app',
]

/**
 * Set the common headers and answer what is not a GET.
 *
 * Returns true when the request has been answered (OPTIONS preflight, or a
 * method other than GET), so the route should stop.
 */
export function handleCommon(req, res) {
  res.setHeader('X-Content-Type-Options', 'nosniff')

  // CORS: exact match, so look-alike origins (e.g. a longer host name that
  // merely contains an allowed one) get no header. Vary on every response, so
  // a cached copy is never reused for a request from another origin
  res.setHeader('Vary', 'Origin')
  const origin = req.headers.origin
  if (origin && ALLOWED_ORIGINS.includes(origin)) {
    res.setHeader('Access-Control-Allow-Origin', origin)
    res.setHeader('Access-Control-Allow-Methods', 'GET')
  }

  if (req.method === 'OPTIONS') {
    res.status(200).end()
    return true
  }
  if (req.method !== 'GET') {
    res.status(405).json({ error: 'Method not allowed' })
    return true
  }
  return false
}

/**
 * Answer 500 when required environment variables are missing.
 *
 * Returns true when it answered, so the route should stop.
 */
export function rejectMissingEnv(res, names) {
  const missing = names.filter((name) => !process.env[name])
  if (missing.length === 0) return false
  console.error('Missing required environment variables:', missing)
  res.status(500).json({
    error: 'Server configuration error',
    details:
      process.env.NODE_ENV === 'development'
        ? `Missing environment variables: ${missing.join(', ')}`
        : 'Server configuration error',
  })
  return true
}

// Generic 500 for unexpected errors (details only in development)
export function internalError(res, error) {
  console.error('API Error:', error)
  return res.status(500).json({
    error: 'Internal server error',
    details:
      process.env.NODE_ENV === 'development' ? error.message : 'Unexpected error',
  })
}
