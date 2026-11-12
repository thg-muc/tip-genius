// Tests for public/js/dashboard-data.js: ranking and listing rules, the
// backfill switch, the filter state in the URL, and a smoke run over the
// sample JSON. Run with `npm test` (Node's built-in test runner).

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { beforeEach, describe, test } from 'node:test'

// Browser globals the module reads (models.js sets window.TIP_GENIUS_MODELS)
globalThis.window = globalThis
globalThis.location = { search: '', pathname: '/dashboard' }
globalThis.history = {
  replaceState: (_state, _title, url) => {
    const query = url.startsWith('?') ? url : ''
    globalThis.location = { search: query, pathname: '/dashboard' }
  },
}
await import('../../public/js/models.js')
const D = await import('../../public/js/dashboard-data.js')

const SAMPLE = JSON.parse(
  readFileSync(
    new URL('../../public/samples/dashboard_stats.sample.json', import.meta.url),
    'utf8'
  )
)

// --- A small synthetic stats object in the real column layout ---------------

const LB = SAMPLE.meta.columns.leaderboard
const SLICE = '2026/27|all'

function lbRow(values) {
  const defaults = {
    rank: null,
    tipped: values.offered - (values.missed ?? 0),
    missed: 0,
    avg: values.avg0,
    se: 0.05,
    ci_low: values.avg0 - 0.1,
    ci_high: values.avg0 + 0.1,
    matchdays: 10,
    ppmd: 10,
    exact: 1,
    diff: 1,
    tendency: 1,
    wrong: 1,
    exact_share: 0.25,
    diff_share: 0.25,
    tendency_share: 0.25,
    wrong_share: 0.25,
    missed_share: 0,
    backfill: 0,
  }
  const row = { ...defaults, ...values }
  return LB.map((column) => row[column])
}

function makeStats({ leaderboard, live = [] }) {
  return {
    version: 1,
    meta: { ...SAMPLE.meta, seasons: ['2026/27'] },
    models: [
      { id: 'Deepseek-Chat', kind: 'llm', active: true },
      { id: 'Google-Gemma-Medium', kind: 'llm', active: true },
      { id: 'Meta-Llama-Mid', kind: 'llm', active: true },
      { id: 'Mistral-Ministral', kind: 'llm', active: true },
      { id: 'OpenRouter-Grok', kind: 'llm', active: false },
      { id: 'Odds-Favourite', kind: 'baseline', active: true },
      { id: 'Consensus', kind: 'baseline', active: true },
    ],
    slices: {
      [SLICE]: {
        fixtures: 200,
        matchdays: 10,
        leaderboard,
        live,
        timeline: {},
        fair: [],
        reliability: [],
      },
    },
    recent: {},
  }
}

const STATE = {
  season: '2026/27',
  league: 'all',
  showAll: false,
  backfill: true,
  fair: null,
}

// Deepseek and Gemma level on Avg and points, Llama behind, Ministral with
// too few matches (100 < 80% of 200), Grok inactive, two baselines
const STATS = makeStats({
  leaderboard: [
    lbRow({ model: 0, points: 300, offered: 200, avg0: 1.5 }),
    lbRow({ model: 1, points: 300, offered: 200, avg0: 1.5 }),
    lbRow({ model: 2, points: 280, offered: 200, avg0: 1.4 }),
    lbRow({ model: 3, points: 170, offered: 100, avg0: 1.7 }),
    lbRow({ model: 4, points: 330, offered: 200, avg0: 1.65 }),
    lbRow({ model: 5, points: 290, offered: 200, avg0: 1.45 }),
    lbRow({ model: 6, points: 310, offered: 200, avg0: 1.55 }),
  ],
})

const byId = (view, id) => view.allRows.find((row) => row.info.id === id)

describe('buildView', () => {
  test('sorts by Avg and gives tied models the same rank', () => {
    const view = D.buildView(STATS, STATE)
    const ranked = view.llms.map((row) => [row.info.id, row.rank])
    assert.deepEqual(ranked, [
      ['Deepseek-Chat', 1],
      ['Google-Gemma-Medium', 1],
      ['Meta-Llama-Mid', 3],
    ])
  })

  test('baselines are listed but never ranked', () => {
    const view = D.buildView(STATS, STATE)
    for (const id of ['Odds-Favourite', 'Consensus']) {
      const row = byId(view, id)
      assert.equal(row.listed, true)
      assert.equal(row.rank, null)
    }
  })

  test('hides inactive models and models under 80% of the matches', () => {
    const view = D.buildView(STATS, STATE)
    assert.equal(byId(view, 'OpenRouter-Grok').reason, 'inactive')
    assert.equal(byId(view, 'Mistral-Ministral').reason, 'few matches')
    assert.deepEqual(
      view.hidden.map((row) => row.info.id),
      ['Mistral-Ministral', 'OpenRouter-Grok']
    )
    assert.ok(!view.rows.some((row) => !row.listed))
  })

  test('Show unranked adds the hidden models without ranking them', () => {
    const view = D.buildView(STATS, { ...STATE, showAll: true })
    assert.equal(view.rows.length, 7)
    assert.equal(byId(view, 'Mistral-Ministral').rank, null)
  })

  test('± Fav is the gap to the favourite baseline', () => {
    const view = D.buildView(STATS, STATE)
    assert.equal(view.fav.info.id, 'Odds-Favourite')
    assert.ok(Math.abs(byId(view, 'Meta-Llama-Mid').deltaFav - -0.05) < 1e-9)
    assert.equal(byId(view, 'Odds-Favourite').deltaFav, 0)
  })

  test('without backfill a model uses its live-only row', () => {
    const stats = makeStats({
      leaderboard: [
        lbRow({ model: 0, points: 300, offered: 200, avg0: 1.5 }),
        lbRow({ model: 3, points: 320, offered: 200, avg0: 1.6, backfill: 60 }),
        lbRow({ model: 5, points: 290, offered: 200, avg0: 1.45 }),
      ],
      live: [lbRow({ model: 3, points: 200, offered: 140, avg0: 1.43 })],
    })
    const withBackfill = D.buildView(stats, STATE)
    assert.equal(withBackfill.hasBackfill, true)
    assert.equal(byId(withBackfill, 'Mistral-Ministral').backfillTips, 60)

    const live = D.buildView(stats, { ...STATE, backfill: false })
    const ministral = byId(live, 'Mistral-Ministral')
    assert.equal(ministral.liveOnly, true)
    assert.equal(ministral.offered, 140)
    assert.equal(ministral.backfillTips, 0)
    // 140 < 80% of 200: listed only with Show unranked
    assert.equal(ministral.reason, 'few matches')
  })

  test('returns null for a slice the JSON does not have', () => {
    assert.equal(D.buildView(STATS, { ...STATE, league: 'La Liga - Spain' }), null)
  })
})

describe('filter state in the URL', () => {
  beforeEach(() => {
    globalThis.location = { search: '', pathname: '/dashboard' }
  })

  test('defaults: current season, all leagues, backfill on', () => {
    const state = D.readState(SAMPLE)
    assert.deepEqual(state, {
      season: D.currentSeason(SAMPLE),
      league: 'all',
      showAll: false,
      backfill: true,
      fair: null,
    })
  })

  test('round trip through writeState and readState', () => {
    const wanted = {
      season: '2025/26',
      league: 'Bundesliga - Germany',
      showAll: true,
      backfill: false,
      fair: 'active',
    }
    D.writeState(wanted)
    assert.match(location.search, /league=bl/)
    assert.deepEqual(D.readState(SAMPLE), wanted)
  })

  test('unknown values fall back to the defaults', () => {
    globalThis.location = {
      search: '?season=1999%2F00&league=xx&fair=bogus',
      pathname: '/dashboard',
    }
    const state = D.readState(SAMPLE)
    assert.equal(state.season, D.currentSeason(SAMPLE))
    assert.equal(state.league, 'all')
    assert.equal(state.fair, null)
  })
})

describe('data source', () => {
  test('the live site always reads /api/stats, ?src= only works locally', () => {
    globalThis.location = { hostname: 'tip-genius.vercel.app', search: '?src=https://evil.example/x.json' }
    assert.equal(D.dataUrl(), '/api/stats')
    globalThis.location = { hostname: 'localhost', search: '?src=/samples/other.json' }
    assert.equal(D.dataUrl(), '/samples/other.json')
    globalThis.location = { hostname: '127.0.0.1', search: '' }
    assert.equal(D.dataUrl(), '/samples/dashboard_stats.sample.json')
  })

  test('scores of the last matchdays are numbers, never markup', () => {
    const stats = structuredClone(SAMPLE)
    const league = Object.keys(stats.recent.leagues)[0]
    const fixture = stats.recent.leagues[league][0].fixtures[0]
    fixture[3] = '<img src=x onerror=alert(1)>'
    fixture[5] = fixture[5].map((tip) => (tip ? ['<b>1</b>', 2, '4', 0] : tip))
    const recent = D.recentMatchdays(stats, { ...STATE, league, season: stats.recent.season })
    const parsed = recent.matchdays[0].fixtures[0]
    assert.ok(Number.isNaN(parsed.homeGoals))
    const tip = parsed.tips.find(Boolean)
    assert.ok(Number.isNaN(tip.home))
    assert.equal(tip.points, 4)
  })
})

describe('helpers', () => {
  test('form covers weeks across all leagues, matchdays within one', () => {
    assert.equal(D.formUnit({ league: 'all' }), 'weeks')
    assert.equal(D.formUnit({ league: 'Bundesliga - Germany' }), 'matchdays')
  })

  test('escapeHtml escapes markup characters', () => {
    assert.equal(D.escapeHtml('<b>"A" & \'B\'</b>'), '&lt;b&gt;&quot;A&quot; &amp; &#39;B&#39;&lt;/b&gt;')
  })

  test('every model in the sample has a display name', () => {
    for (let index = 0; index < SAMPLE.models.length; index++) {
      const info = D.modelInfo(SAMPLE, index)
      assert.ok(info.name && info.short, `no name for ${info.id}`)
    }
  })
})

describe('sample JSON (smoke)', () => {
  test('every slice builds a view in every mode', () => {
    for (const key of Object.keys(SAMPLE.slices)) {
      const [season, league] = key.split('|')
      for (const backfill of [true, false]) {
        for (const fair of [null, 'active', 'established']) {
          const view = D.buildView(SAMPLE, { ...STATE, season, league, backfill, fair })
          assert.ok(view, key)
          assert.ok(view.rows.length > 0, key)
          assert.ok(view.fav, `no favourite in ${key}`)
        }
      }
    }
  })
})
