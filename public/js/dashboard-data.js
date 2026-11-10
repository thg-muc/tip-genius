// Shared data layer for the statistics dashboard variants.
//
// Loads the dashboard JSON (written by the cron step, see lib/stats.py), applies
// the agreed filters and display rules and returns plain view objects, so the
// pages only differ in presentation. Rows in the JSON refer to models by their
// index in `models`; everything returned here carries the resolved model info.

// Display names and logos, matching LLM_PROVIDERS in main.js where a model is
// still offered on the main page
const MODEL_DISPLAY = {
  'Deepseek-Chat': { name: 'DeepSeek V4.1 Flash', short: 'DeepSeek', logo: 'DeepSeek' },
  'Google-Gemma-Medium': { name: 'Gemma 4', short: 'Gemma', logo: 'Google' },
  'Meta-Llama-Mid': { name: 'Llama 4 Maverick', short: 'Llama', logo: 'Meta' },
  'Microsoft-Phi-Medium': { name: 'Phi 4 Medium', short: 'Phi', logo: 'Microsoft' },
  'Mistral-Ministral': { name: 'Ministral 14B', short: 'Ministral', logo: 'Mistral' },
  'Nvidia-Nemotron-Lightning': {
    name: 'Nemotron 3.5 Lightning',
    short: 'Nemotron',
    logo: 'NVIDIA',
  },
  'OpenRouter-GPT-OSS-Large': {
    name: 'GPT-OSS 120b',
    short: 'GPT-OSS',
    logo: 'OpenAI',
  },
  'Google-Gemini-Flash': { name: 'Gemini Flash', short: 'Gemini', logo: 'Google' },
  'Meta-Llama-70b': { name: 'Llama 70B', short: 'Llama 70B', logo: 'Meta' },
  'Mistral-Large': { name: 'Mistral Large', short: 'Mistral L', logo: 'Mistral' },
  'Mistral-Medium': { name: 'Mistral Medium', short: 'Mistral M', logo: 'Mistral' },
  'OpenAI-GPT-Mini': { name: 'GPT Mini', short: 'GPT Mini', logo: 'OpenAI' },
  'OpenRouter-Gemini-Flash-Lite': {
    name: 'Gemini Flash Lite',
    short: 'Gemini Lite',
    logo: 'Google',
  },
  'OpenRouter-GPT-Mini': {
    name: 'GPT Mini (OpenRouter)',
    short: 'GPT Mini OR',
    logo: 'OpenAI',
  },
  'OpenRouter-Grok': { name: 'Grok', short: 'Grok', logo: 'xAI' },
  'Odds-Favourite': { name: 'Favourite 2:1', short: 'Favourite 2:1', icon: '⭐' },
  'Always-2:1': { name: 'Always 2:1', short: 'Always 2:1', icon: '🏠' },
  Consensus: { name: 'Model consensus', short: 'Consensus', icon: '🤖' },
}

const BASELINE_HINTS = {
  'Odds-Favourite':
    'Baseline without AI: the bookmakers’ favourite wins 2:1, i.e. 2:1 when the home team is the favourite and 1:2 when the away team is (never a draw)',
  'Always-2:1': 'Baseline without AI: tips 2:1 for the home team in every match',
  Consensus:
    'Baseline built from the models: per match the median of all model tips (home and away goals separately, .5 rounded up)',
}

export const FAVOURITE = 'Odds-Favourite'

// Plain-language explanations shown on every page; `key` lets a page pick
// the terms it actually uses
export const GLOSSARY = [
  {
    key: 'favourite',
    term: 'Favourite 2:1',
    text: BASELINE_HINTS['Odds-Favourite'] + '.',
  },
  { key: 'consensus', term: 'Model consensus', text: BASELINE_HINTS.Consensus + '.' },
  { key: 'always', term: 'Always 2:1', text: BASELINE_HINTS['Always-2:1'] + '.' },
  {
    key: 'avg',
    term: 'Avg',
    text: 'Points per match the model was offered; a missing tip counts 0. Models are ranked by it.',
  },
  {
    key: 'delta',
    term: '± Fav',
    text: 'Avg minus the Avg of Favourite 2:1. Positive means better than simply backing the favourite.',
  },
  {
    key: 'interval',
    term: '95% interval',
    text: 'The range the model’s true Avg most likely lies in. Where two intervals overlap a lot, the difference can be chance.',
  },
  {
    key: 'hits',
    term: '4 / 3 / 2',
    text: 'Number of exact scores (4 points), correct goal differences (3) and correct outcomes (2).',
  },
  {
    key: 'coverage',
    term: 'Cov.',
    text: 'Coverage: share of the expected tips the model delivered before kickoff; ⚠︎ below 90%.',
  },
  {
    key: 'form',
    term: 'Form',
    text: 'Avg over the last 5 matchdays (last 5 weeks when all leagues are shown): who is in good shape right now.',
  },
  {
    key: 'backfill',
    term: 'BF',
    text: 'Includes backfill tips: generated afterwards for models that joined late, for the matches before their first live run.',
  },
]

export const LEAGUES = [
  { value: 'all', code: 'all', label: 'All leagues', short: 'All' },
  {
    value: 'Bundesliga - Germany',
    code: 'bl',
    label: 'Bundesliga',
    short: 'BL',
    logo: '/images/leagues/Bundesliga-Germany.png',
  },
  {
    value: 'La Liga - Spain',
    code: 'll',
    label: 'La Liga',
    short: 'LL',
    logo: '/images/leagues/LaLiga-Spain.png',
  },
  {
    value: 'Premier League - England',
    code: 'pl',
    label: 'Premier League',
    short: 'PL',
    logo: '/images/leagues/PremierLeague-England.png',
  },
  {
    value: 'UEFA Champions League',
    code: 'cl',
    label: 'Champions League',
    short: 'CL',
    logo: '/images/leagues/UEFAChampionsLeague.png',
  },
]

// Minimum share of the slice's best-covered model a model needs to be listed
// by default, and the slice size below which the small-sample note shows
export const MIN_SHARE_OF_MAX = 0.8
export const SMALL_SLICE = 100
export const LOW_COVERAGE = 0.9

// Categorical slots (dataviz reference palette, validated against the card
// surfaces #ffffff and #1f2937). Colour follows the model, never its rank:
// active LLMs take the slots in the order of `models`
const SERIES = {
  light: [
    '#2a78d6',
    '#eb6834',
    '#1baf7a',
    '#eda100',
    '#e87ba4',
    '#008300',
    '#6250d6',
    '#e34948',
  ],
  dark: [
    '#3987e5',
    '#d95926',
    '#199e70',
    '#c98500',
    '#d55181',
    '#008300',
    '#9085e9',
    '#e66767',
  ],
}

// Hit types, an ordinal one-hue ramp (strongest = exact) plus neutral greys
const HIT_COLORS = {
  light: {
    exact: '#184f95',
    diff: '#2a78d6',
    tendency: '#86b6ef',
    wrong: '#d1d5db',
    missed: '#f3f4f6',
  },
  dark: {
    exact: '#b7d3f6',
    diff: '#5598e7',
    tendency: '#256abf',
    wrong: '#4b5563',
    missed: '#374151',
  },
}

export const HIT_TYPES = [
  { key: 'exact', label: 'Exact', points: 4, long: 'Exact score (4 points)' },
  {
    key: 'diff',
    label: 'Goal difference',
    points: 3,
    long: 'Correct goal difference (3 points)',
  },
  { key: 'tendency', label: 'Outcome', points: 2, long: 'Correct outcome (2 points)' },
  { key: 'wrong', label: 'Wrong', points: 0, long: 'Wrong tip (0 points)' },
  {
    key: 'missed',
    label: 'No tip',
    points: 0,
    long: 'No tip given (0 points)',
  },
]

const CHROME = {
  light: {
    text: '#1f2937',
    secondary: '#4b5563',
    muted: '#6b7280',
    grid: '#e5e7eb',
    axis: '#d1d5db',
    surface: '#ffffff',
    favourite: '#111827',
    baseline: '#6b7280',
    inactive: '#9ca3af',
  },
  dark: {
    text: '#e5e7eb',
    secondary: '#d1d5db',
    muted: '#9ca3af',
    grid: '#374151',
    axis: '#4b5563',
    surface: '#1f2937',
    favourite: '#f9fafb',
    baseline: '#9ca3af',
    inactive: '#6b7280',
  },
}

// --- Loading -----------------------------------------------------------------

// Local development reads the sample file; production reads the API endpoint.
// `?src=<url>` overrides both
export function dataUrl() {
  const override = new URLSearchParams(location.search).get('src')
  if (override) return override
  const local = ['localhost', '127.0.0.1', '[::1]', ''].includes(location.hostname)
  return local ? '/samples/dashboard_stats.sample.json' : '/api/stats'
}

export async function loadStats(url = dataUrl()) {
  const response = await fetch(url, { headers: { Accept: 'application/json' } })
  if (!response.ok) throw new Error(`HTTP ${response.status} for ${url}`)
  const stats = await response.json()
  if (!stats || !stats.models || !stats.slices)
    throw new Error('Unexpected stats format')
  return stats
}

// --- Filter state (mirrored in the query string so views can be linked) ------

export function seasonOptions(stats) {
  const seasons = [...stats.meta.seasons].sort().reverse()
  return [
    ...seasons.map((s) => ({ value: s, label: s })),
    { value: 'all', label: 'All' },
  ]
}

export function currentSeason(stats) {
  return [...stats.meta.seasons].sort().at(-1)
}

export function readState(stats, extra = {}) {
  const params = new URLSearchParams(location.search)
  const seasons = seasonOptions(stats).map((o) => o.value)
  const league = LEAGUES.find((l) => l.code === params.get('league'))
  const fair = params.get('fair')
  const state = {
    season: seasons.includes(params.get('season'))
      ? params.get('season')
      : currentSeason(stats),
    league: league ? league.value : 'all',
    showAll: params.get('all') === '1',
    backfill: params.get('backfill') !== '0',
    fair: ['active', 'established'].includes(fair) ? fair : null,
  }
  for (const [key, fallback] of Object.entries(extra)) {
    state[key] = params.get(key) ?? fallback
  }
  return state
}

export function writeState(state, extraKeys = []) {
  const params = new URLSearchParams(location.search)
  const set = (key, value, fallback) =>
    value === fallback || value == null ? params.delete(key) : params.set(key, value)
  set('season', state.season, undefined)
  set('league', LEAGUES.find((l) => l.value === state.league)?.code, 'all')
  set('all', state.showAll ? '1' : null, null)
  set('backfill', state.backfill ? null : '0', null)
  set('fair', state.fair, null)
  for (const key of extraKeys) set(key, state[key], undefined)
  const query = params.toString()
  history.replaceState(null, '', query ? `?${query}` : location.pathname)
}

// --- Models ------------------------------------------------------------------

export function modelInfo(stats, index) {
  const model = stats.models[index]
  const display = MODEL_DISPLAY[model.id] || { name: model.label, short: model.label }
  const llms = stats.models.filter((m) => m.kind === 'llm' && m.active)
  const slot = model.kind === 'llm' && model.active ? llms.indexOf(model) : -1
  return {
    index,
    id: model.id,
    name: display.name,
    short: display.short,
    logo: display.logo ? `/images/llm-logos/${display.logo}.png` : null,
    kind: model.kind,
    isBaseline: model.kind === 'baseline',
    isFavourite: model.id === FAVOURITE,
    active: model.active,
    lateJoiner: model.late_joiner,
    firstLive: model.first_live,
    lastTip: model.last_tip,
    backfill: model.backfill,
    hint: BASELINE_HINTS[model.id] || null,
    icon: display.icon || null,
    slot,
  }
}

export function isDark() {
  return window.matchMedia('(prefers-color-scheme: dark)').matches
}

export function onSchemeChange(callback) {
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', callback)
}

export function chrome() {
  return CHROME[isDark() ? 'dark' : 'light']
}

export function hitColors() {
  return HIT_COLORS[isDark() ? 'dark' : 'light']
}

// Line/marker style per model: categorical hue for active LLMs, ink with a
// dash pattern for the baselines, grey for inactive models
export function modelStyle(info) {
  const mode = isDark() ? 'dark' : 'light'
  const c = CHROME[mode]
  if (info.isFavourite) return { color: c.favourite, dash: [], width: 3 }
  if (info.id === 'Consensus') return { color: c.baseline, dash: [2, 3], width: 2 }
  if (info.isBaseline) return { color: c.baseline, dash: [6, 4], width: 2 }
  if (info.slot < 0) return { color: c.inactive, dash: [], width: 1.5 }
  return { color: SERIES[mode][info.slot % SERIES[mode].length], dash: [], width: 2 }
}

// --- Slices and leaderboard ----------------------------------------------------

function columns(stats, name) {
  return stats.meta.columns[name]
}

function zip(cols, row) {
  return Object.fromEntries(cols.map((c, i) => [c, row[i]]))
}

export function sliceKey(state) {
  return `${state.season}|${state.league}`
}

export function getSlice(stats, state) {
  return stats.slices[sliceKey(state)] || null
}

function coverageFor(raw, reliability) {
  if (reliability && reliability.expected) {
    return {
      share: reliability.delivered / reliability.expected,
      delivered: reliability.delivered,
      expected: reliability.expected,
      runShare: reliability.run_share,
      runs: reliability.runs,
      missedRuns: reliability.missed_runs,
      source: 'live',
    }
  }
  return {
    share: raw.offered ? raw.tipped / raw.offered : 0,
    delivered: raw.tipped,
    expected: raw.offered,
    runShare: null,
    runs: null,
    missedRuns: null,
    source: 'table',
  }
}

/**
 * Build the view for one filter state.
 *
 * Returns every model row of the slice (sorted by avg0, the ranking key) with
 * `listed` = shown by default (active and >= 80% of the best-covered model's
 * fixtures); `rows` is what the page should draw given `state.showAll`.
 */
export function buildView(stats, state) {
  const slice = getSlice(stats, state)
  if (!slice) return null
  const lbCols = columns(stats, 'leaderboard')
  const relCols = columns(stats, 'reliability')
  const reliability = new Map(
    (slice.reliability || []).map((r) => [r[0], zip(relCols, r)])
  )
  const formMap = new Map(
    (slice.timeline?.form || []).map(([m, avg0, points, offered]) => [
      m,
      { avg0, points, offered },
    ])
  )
  const liveRows = new Map((slice.live || []).map((r) => [r[0], zip(lbCols, r)]))
  const hasBackfill = slice.leaderboard.some((r) => zip(lbCols, r).backfill > 0)

  let mode = 'standard'
  let fairGroup = null
  let raws
  if (state.fair && slice.fair?.length) {
    fairGroup = slice.fair.find((g) => g.group === state.fair) || slice.fair[0]
    mode = 'fair'
    const fairCols = columns(stats, 'fair')
    raws = fairGroup.rows.map((r) => {
      const f = zip(fairCols, r)
      return {
        ...f,
        offered: fairGroup.fixtures,
        tipped: fairGroup.fixtures - f.missed,
        ci_low: f.avg0 - 1.96 * f.se,
        ci_high: f.avg0 + 1.96 * f.se,
        avg: null,
        backfill: 0,
      }
    })
  } else {
    raws = slice.leaderboard.map((r) => {
      const row = zip(lbCols, r)
      if (!state.backfill && row.backfill > 0 && liveRows.has(row.model)) {
        return { ...liveRows.get(row.model), backfill: 0, liveOnly: true }
      }
      return row
    })
  }

  const rows = raws.map((raw) => {
    const info = modelInfo(stats, raw.model)
    return {
      info,
      points: raw.points,
      offered: raw.offered,
      tipped: raw.tipped,
      missed: raw.missed,
      avg: raw.avg,
      avg0: raw.avg0,
      se: raw.se,
      ciLow: raw.ci_low,
      ciHigh: raw.ci_high,
      matchdays: raw.matchdays ?? null,
      ppmd: raw.ppmd,
      hits:
        mode === 'standard'
          ? {
              exact: raw.exact,
              diff: raw.diff,
              tendency: raw.tendency,
              wrong: raw.wrong,
              missed: raw.missed,
            }
          : null,
      backfillTips: raw.backfill || 0,
      liveOnly: !!raw.liveOnly,
      coverage: info.isBaseline ? null : coverageFor(raw, reliability.get(raw.model)),
      form: formMap.get(raw.model) || null,
    }
  })
  rows.sort((a, b) => b.avg0 - a.avg0 || b.points - a.points)

  const llmOffered = rows.filter((r) => !r.info.isBaseline).map((r) => r.offered)
  const maxOffered = Math.max(0, ...llmOffered)
  // Competition ranking: models level on Avg and points share a rank
  let rank = 0
  let position = 0
  let previous = null
  for (const row of rows) {
    if (row.info.isBaseline) {
      row.listed = true
      row.reason = null
    } else if (!row.info.active) {
      row.listed = false
      row.reason = 'inactive'
    } else if (row.offered < MIN_SHARE_OF_MAX * maxOffered) {
      row.listed = false
      row.reason = 'few matches'
    } else {
      row.listed = true
      row.reason = null
    }
    if (row.listed && !row.info.isBaseline) {
      position++
      const tied =
        previous && previous.avg0 === row.avg0 && previous.points === row.points
      if (!tied) rank = position
      row.rank = rank
      previous = row
    } else {
      row.rank = null
    }
    row.smallSample = !row.listed && !row.info.isBaseline
    row.lowCoverage = !!row.coverage && row.coverage.share < LOW_COVERAGE
  }

  const fav = rows.find((r) => r.info.isFavourite) || null
  for (const row of rows) row.deltaFav = fav ? row.avg0 - fav.avg0 : null
  const listedLlms = rows.filter((r) => r.listed && !r.info.isBaseline)
  const hidden = rows.filter((r) => !r.listed)

  return {
    state,
    slice,
    mode,
    fairGroup,
    fairGroups: (slice.fair || []).map((g) => g.group),
    fixtures: slice.fixtures,
    matchdays: slice.matchdays,
    smallSlice: slice.fixtures < SMALL_SLICE,
    hasBackfill,
    maxOffered,
    allRows: rows,
    rows: state.showAll ? rows : rows.filter((r) => r.listed),
    llms: listedLlms,
    hidden,
    fav,
    best: listedLlms[0] || null,
    baselines: rows.filter((r) => r.info.isBaseline),
  }
}

// --- Headline (key finding: do the models beat backing the favourite?) -------

export function headline(view) {
  const { best, fav, llms } = view
  if (!best || !fav) return null
  const beating = llms.filter((r) => r.avg0 > fav.avg0)
  const delta = best.avg0 - fav.avg0
  const overlap = best.ciLow < fav.ciHigh && fav.ciLow < best.ciHigh
  const pointsGap = best.points - fav.points
  return {
    best,
    fav,
    delta,
    pointsGap,
    beating: beating.length,
    total: llms.length,
    overlap,
    verdict:
      delta > 0
        ? overlap
          ? 'ahead-noise'
          : 'ahead'
        : overlap
          ? 'behind-noise'
          : 'behind',
  }
}

// --- Timeline -------------------------------------------------------------------

const MONTHS = [
  'Jan',
  'Feb',
  'Mar',
  'Apr',
  'May',
  'Jun',
  'Jul',
  'Aug',
  'Sep',
  'Oct',
  'Nov',
  'Dec',
]
const MONTHS_LONG = [
  'January',
  'February',
  'March',
  'April',
  'May',
  'June',
  'July',
  'August',
  'September',
  'October',
  'November',
  'December',
]

export function stepLabel(step, value, long = false) {
  if (step === 'matchday') {
    const [y, m, d] = value.split('-')
    return long
      ? `Matchday from ${MONTHS[Number(m) - 1]} ${Number(d)}, ${y}`
      : `${MONTHS[Number(m) - 1]} ${Number(d)}`
  }
  if (step === 'week') {
    const [y, w] = value.split('-W')
    return long ? `Week ${Number(w)}, ${y}` : `W${Number(w)}`
  }
  const [y, m] = value.split('-')
  return long
    ? `${MONTHS_LONG[Number(m) - 1]} ${y}`
    : `${MONTHS[Number(m) - 1]} ${y.slice(2)}`
}

/**
 * Cumulative points per step for the view's models, aligned to the slice's
 * steps (null outside a model's active range), plus the running gap to the
 * favourite over the same steps (the favourite's points from the model's first
 * step on, so a late joiner starts at zero).
 */
export function timeline(stats, view) {
  const tl = view.slice.timeline
  const n = tl.steps.length
  const byModel = new Map(tl.points.map(([m, start, values]) => [m, { start, values }]))
  const cumulative = (model) => {
    const line = byModel.get(model)
    if (!line) return null
    const out = new Array(n).fill(null)
    line.values.forEach((v, i) => (out[line.start + i] = v))
    return out
  }
  const favIndex = view.fav?.info.index
  const favLine = favIndex != null ? cumulative(favIndex) : null
  const favAt = (i) => {
    if (!favLine || i < 0) return 0
    for (let j = i; j >= 0; j--) if (favLine[j] != null) return favLine[j]
    return 0
  }
  const series = view.rows
    .map((row) => {
      const values = cumulative(row.info.index)
      if (!values) return null
      const start = byModel.get(row.info.index).start
      const diff = values.map((v, i) =>
        v == null ? null : v - (favAt(i) - favAt(start - 1))
      )
      return { row, values, diff }
    })
    .filter(Boolean)
  return {
    step: tl.step,
    steps: tl.steps,
    labels: tl.steps.map((s) => stepLabel(tl.step, s)),
    longLabels: tl.steps.map((s) => stepLabel(tl.step, s, true)),
    series,
    stepName: { matchday: 'matchday', week: 'week', month: 'month' }[tl.step],
    stepPlural: { matchday: 'matchdays', week: 'weeks', month: 'months' }[tl.step],
  }
}

// --- Last matchdays ----------------------------------------------------------

/**
 * Last matchdays with every model's tip. The JSON only carries the current
 * season; with all leagues selected, the latest matchday of each league.
 */
export function recentMatchdays(stats, state) {
  const recent = stats.recent
  if (!recent) return null
  const models = recent.models.map((i) => modelInfo(stats, i))
  const leagues = state.league === 'all' ? Object.keys(recent.leagues) : [state.league]
  const matchdays = []
  for (const league of leagues) {
    const days = recent.leagues[league] || []
    for (const day of state.league === 'all' ? days.slice(0, 1) : days) {
      matchdays.push({
        league: LEAGUES.find((l) => l.value === league) || {
          value: league,
          label: league,
        },
        date: day.matchday,
        fixtures: day.fixtures.map(([kickoff, home, away, hg, ag, tips]) => ({
          kickoff,
          home,
          away,
          homeGoals: hg,
          awayGoals: ag,
          tips: tips.map((t, i) =>
            t
              ? {
                  info: models[i],
                  home: t[0],
                  away: t[1],
                  points: t[2],
                  backfill: t[3] === 1,
                }
              : null
          ),
        })),
      })
    }
  }
  matchdays.sort((a, b) => b.date.localeCompare(a.date))
  return {
    season: recent.season,
    models,
    matchdays,
    otherSeason: state.season !== recent.season,
  }
}

// --- Formatting (English, as on the main page) -------------------------------------------------------

const numberFormats = new Map()
export function fmt(value, digits = 2) {
  if (value == null || Number.isNaN(value)) return '–'
  if (!numberFormats.has(digits)) {
    numberFormats.set(
      digits,
      new Intl.NumberFormat('en-US', {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
      })
    )
  }
  return numberFormats.get(digits).format(value)
}

export function fmtSigned(value, digits = 2) {
  if (value == null) return '–'
  const text = fmt(Math.abs(value), digits)
  if (Number(text) === 0) return `±${text}`
  return `${value > 0 ? '+' : '−'}${text}`
}

export function pct(value, digits = 0) {
  if (value == null) return '–'
  return `${fmt(value * 100, digits)}%`
}

export function fmtDate(iso, withYear = true) {
  const [y, m, d] = iso.slice(0, 10).split('-')
  const day = `${MONTHS[Number(m) - 1]} ${Number(d)}`
  return withYear ? `${day}, ${y}` : day
}

export function fmtKickoff(iso) {
  // Kickoffs are UTC ("2026-10-30T19:30Z"); show them in Berlin time
  const date = new Date(iso.replace('Z', ':00Z'))
  return date.toLocaleString('en-US', {
    timeZone: 'Europe/Berlin',
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

export function fmtGenerated(stats) {
  const date = new Date(stats.meta.generated_at)
  return date.toLocaleString('en-US', {
    timeZone: 'Europe/Berlin',
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

// Footer status line; phones skip the results date to stay on two lines
export function footerMetaHtml(stats) {
  const counts = `${stats.meta.matches} matches, ${stats.meta.tips} tips`
  return `Updated: ${escapeHtml(fmtGenerated(stats))}<span class="hidden sm:inline"> · Results through ${escapeHtml(
    fmtDate(stats.meta.data_through)
  )}</span> · ${escapeHtml(counts)}`
}

// Glossaries start open where there is room and closed on phones
export function startsExpanded() {
  return window.matchMedia('(min-width: 640px)').matches
}

// --- Model labels (one look everywhere) -----------------------------------
//
// A model is always shown as its logo (or a baseline's symbol) plus its name:
// the short name on phones, the full name from the sm breakpoint up. Colour
// only appears where it identifies a line in a chart, next to this label.

export function modelIconHtml(info, size = 'w-5 h-5') {
  if (info.logo) {
    return `<img src="${info.logo}" alt="" class="${size} object-contain shrink-0" loading="lazy">`
  }
  return `<span class="${size} shrink-0 inline-flex items-center justify-center text-sm leading-none" aria-hidden="true">${info.icon || ''}</span>`
}

export function modelNameHtml(info) {
  return `<span class="sm:hidden">${escapeHtml(info.short)}</span><span class="hidden sm:inline">${escapeHtml(info.name)}</span>`
}

export function modelLabelHtml(info, { size = 'w-5 h-5', suffix = '' } = {}) {
  return `<span class="inline-flex items-center gap-2 min-w-0 ${info.isBaseline ? 'italic' : ''}">${modelIconHtml(
    info,
    size
  )}<span class="truncate">${modelNameHtml(info)}${suffix}</span></span>`
}

// Logo images for canvas charts, loaded once; onLoad redraws a waiting chart
const iconImages = new Map()
export function modelIconImage(info, onLoad) {
  if (!info.logo) return null
  let image = iconImages.get(info.logo)
  if (!image) {
    image = new Image()
    image.src = info.logo
    iconImages.set(info.logo, image)
  }
  if (!image.complete && onLoad) image.addEventListener('load', onLoad, { once: true })
  return image
}

export function escapeHtml(text) {
  return String(text).replace(
    /[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]
  )
}

// --- Notes shared by all variants ------------------------------------------

export function viewNotes(view) {
  const notes = []
  if (view.smallSlice) {
    notes.push({
      kind: 'sample',
      text: `Small sample: only ${view.fixtures} matches. Differences within the error bars are chance.`,
    })
  }
  if (view.mode === 'fair') {
    notes.push({
      kind: 'info',
      text: `Fair comparison: only the ${view.fairGroup.fixtures} matches offered to every ${
        view.fairGroup.group === 'established' ? 'established' : 'active'
      } model.${view.hasBackfill ? ' Backfill tips always count here.' : ''}`,
    })
  } else if (!view.state.backfill && view.hasBackfill) {
    notes.push({
      kind: 'info',
      text: 'Without backfill: tips generated afterwards do not count, so the late joiners have fewer matches.',
    })
  }
  return notes
}

// With no note, an invisible placeholder of the same size keeps the space, so
// the content below does not jump when a filter adds or removes a note
export function notesWithPlaceholder(view) {
  const notes = viewNotes(view)
  if (notes.length) return notes
  return [
    {
      kind: 'sample',
      text: 'Small sample: only 100 matches. Differences within the error bars are chance.',
      placeholder: true,
    },
  ]
}

// --- Chart.js defaults ---------------------------------------------------------

export function applyChartDefaults(Chart) {
  const c = chrome()
  Chart.defaults.font.family =
    'ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif'
  Chart.defaults.font.size = 12
  Chart.defaults.color = c.muted
  Chart.defaults.borderColor = c.grid
  Chart.defaults.maintainAspectRatio = false
  // No animations: a chart is painted completely when it is created instead
  // of over animation frames, which a throttled or background tab may never
  // deliver (the chart then stays blank after a tab switch)
  Chart.defaults.animation = false
  Object.assign(Chart.defaults.plugins.tooltip, {
    backgroundColor: isDark() ? '#111827' : '#ffffff',
    titleColor: c.text,
    bodyColor: c.secondary,
    borderColor: c.axis,
    borderWidth: 1,
    padding: 10,
    cornerRadius: 8,
    boxPadding: 4,
    usePointStyle: true,
  })
  Chart.defaults.plugins.legend.display = false
}
