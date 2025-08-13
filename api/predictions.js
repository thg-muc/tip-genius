// api/predictions.js

// Configuration object
const CONFIG = {
  ALLOWED_ORIGINS: [
    'http://localhost:3000',
    'http://localhost:5000',
    'http://localhost:8000',
    'https://tip-genius.vercel.app',
  ],
  CACHE_DURATION: 300, // Cache in seconds
}

// Validate required environment variables
const REQUIRED_ENV_VARS = [
  'KV_REST_API_URL',
  'KV_REST_API_READ_ONLY_TOKEN',
  'KV_DEFAULT_KEY',
]

export default async function handler(req, res) {
  // Set basic headers
  res.setHeader('X-Content-Type-Options', 'nosniff')
  res.setHeader('Cache-Control', `public, s-maxage=${CONFIG.CACHE_DURATION}`)

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

    // Fetch predictions from KV
    const response = await fetch(
      `${process.env.KV_REST_API_URL}/get/${
        req.query.key || process.env.KV_DEFAULT_KEY
      }`,
      {
        headers: {
          Authorization: `Bearer ${process.env.KV_REST_API_READ_ONLY_TOKEN}`,
        },
      }
    )

    // Handle unsuccessful responses
    if (!response.ok) {
      return res.status(response.status).json({
        error:
          response.status === 404
            ? 'No predictions found'
            : 'Failed to fetch predictions',
        details: await response.text(),
      })
    }

    // Return the predictions
    const data = await response.json()
    return res.status(200).json(data)
  } catch (error) {
    console.error('API Error:', error)
    return res.status(500).json({
      error: 'Internal server error',
      details:
        process.env.NODE_ENV === 'development' ? error.message : 'Unexpected error',
    })
  }
}
