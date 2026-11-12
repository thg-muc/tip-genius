// api/predictions.js

import { handleCommon, internalError, rejectMissingEnv } from './_shared.js'

// Configuration object
const CONFIG = {
  CACHE_DURATION: 300, // Cache in seconds
}

// Validate required environment variables
const REQUIRED_ENV_VARS = [
  'KV_REST_API_URL',
  'KV_REST_API_READ_ONLY_TOKEN',
  'KV_DEFAULT_KEY',
]

export default async function handler(req, res) {
  res.setHeader('Cache-Control', `public, s-maxage=${CONFIG.CACHE_DURATION}`)
  if (handleCommon(req, res)) return

  try {
    if (rejectMissingEnv(res, REQUIRED_ENV_VARS)) return

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
    return internalError(res, error)
  }
}
