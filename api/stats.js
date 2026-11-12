// api/stats.js
//
// Serves the statistics dashboard JSON (public/dashboard.html). The workflow
// recomputes it on every non-debug run (Tue/Fri) and stores it as one string
// in KV under `stats_kv_key` (src/tip_genius/cfg/vercel_config.yaml); this
// route only reads it with the read-only token.

// Configuration object
const CONFIG = {
  ALLOWED_ORIGINS: [
    'http://localhost:3000',
    'http://localhost:5000',
    'http://localhost:8000',
    'https://tip-genius.vercel.app',
  ],
  // KV key of the dashboard JSON (stats_kv_key in vercel_config.yaml)
  KV_KEY: 'dashboard_stats',
  // The JSON only changes twice a week: the CDN keeps it for an hour and may
  // serve the old copy for a day while it refetches in the background; browsers
  // keep it for 5 minutes, so switching between Tips and Stats does not refetch
  CACHE_CONTROL: 'public, max-age=300, s-maxage=3600, stale-while-revalidate=86400',
}

// Validate required environment variables
const REQUIRED_ENV_VARS = ['KV_REST_API_URL', 'KV_REST_API_READ_ONLY_TOKEN']

export default async function handler(req, res) {
  // Set basic headers; errors are never cached (success overrides this below)
  res.setHeader('X-Content-Type-Options', 'nosniff')
  res.setHeader('Cache-Control', 'no-store')

  // CORS handling
  const origin = req.headers.origin
  if (origin && CONFIG.ALLOWED_ORIGINS.some((allowed) => origin.includes(allowed))) {
    res.setHeader('Access-Control-Allow-Origin', origin)
    res.setHeader('Access-Control-Allow-Methods', 'GET')
  }

  // Quick returns for non-GET requests
  if (req.method === 'OPTIONS') return res.status(200).end()
  if (req.method !== 'GET') return res.status(405).json({ error: 'Method not allowed' })

  try {
    // Check for required environment variables
    const missingVars = REQUIRED_ENV_VARS.filter((varName) => !process.env[varName])
    if (missingVars.length > 0) {
      console.error('Missing required environment variables:', missingVars)
      return res.status(500).json({
        error: 'Server configuration error',
        details:
          process.env.NODE_ENV === 'development'
            ? `Missing environment variables: ${missingVars.join(', ')}`
            : 'Server configuration error',
      })
    }

    // Fetch the stats from KV (a fixed key: this route takes no parameters)
    const response = await fetch(
      `${process.env.KV_REST_API_URL}/get/${CONFIG.KV_KEY}`,
      {
        headers: {
          Authorization: `Bearer ${process.env.KV_REST_API_READ_ONLY_TOKEN}`,
        },
      }
    )

    if (!response.ok) {
      console.error('KV request failed:', response.status, await response.text())
      return res.status(502).json({ error: 'Failed to fetch stats' })
    }

    // KV returns {"result": "<the JSON as a string>"}, or null for a missing key
    const { result } = await response.json()
    if (typeof result !== 'string') {
      return res.status(404).json({ error: 'No stats found' })
    }

    // Parse before sending: a broken value must not be cached by the CDN
    const stats = JSON.parse(result)
    if (!stats || typeof stats !== 'object' || !stats.models || !stats.slices) {
      console.error('Unexpected stats format in KV')
      return res.status(502).json({ error: 'Unexpected stats format' })
    }

    // Only successful responses are cacheable
    res.setHeader('Cache-Control', CONFIG.CACHE_CONTROL)
    return res.status(200).json(stats)
  } catch (error) {
    console.error('API Error:', error)
    return res.status(500).json({
      error: 'Internal server error',
      details:
        process.env.NODE_ENV === 'development' ? error.message : 'Unexpected error',
    })
  }
}
