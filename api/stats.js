// api/stats.js
//
// Serves the statistics dashboard JSON (public/dashboard.html). The workflow
// recomputes it on every non-debug run (Tue/Fri) and stores it as one string
// in KV under `stats_kv_key` (src/tip_genius/cfg/vercel_config.yaml); this
// route only reads it with the read-only token.

import { handleCommon, internalError, rejectMissingEnv } from './_shared.js'

const CONFIG = {
  // KV key of the dashboard JSON (stats_kv_key in vercel_config.yaml)
  KV_KEY: 'dashboard_stats',
  // The JSON only changes twice a week: the CDN keeps it for an hour and may
  // serve the old copy for a day while it refetches in the background; browsers
  // keep it for 5 minutes, so switching between Tips and Stats does not refetch
  CACHE_CONTROL: 'public, max-age=300, s-maxage=3600, stale-while-revalidate=86400',
}

const REQUIRED_ENV_VARS = ['KV_REST_API_URL', 'KV_REST_API_READ_ONLY_TOKEN']

export default async function handler(req, res) {
  // Errors are never cached (a success overrides this below)
  res.setHeader('Cache-Control', 'no-store')
  if (handleCommon(req, res)) return

  // The route takes no parameters. The CDN caches per URL, so a query string
  // would turn every request into a KV read; refuse it before touching KV
  if (Object.keys(req.query || {}).length > 0) {
    return res.status(400).json({ error: 'This endpoint takes no parameters' })
  }

  try {
    if (rejectMissingEnv(res, REQUIRED_ENV_VARS)) return

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

    res.setHeader('Cache-Control', CONFIG.CACHE_CONTROL)
    return res.status(200).json(stats)
  } catch (error) {
    return internalError(res, error)
  }
}
