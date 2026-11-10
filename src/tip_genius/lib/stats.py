"""Model-comparison statistics from the prediction table and the results archive.

Joins the prediction table (``lib.prediction_table``, one row per fixture and
provider) to the results CSVs (``lib.results_archive``) on ``match_key`` and
scores every tip with Kicktipp four-points scoring. Used by the workflow (a
compact JSON in Vercel KV for the dashboard, ``build_stats``) and by
``utils/eval_providers.py`` (terminal tables).

Rules (also written into the JSON's ``meta``):

- Offered: a finished fixture counts as offered to a provider if it kicked off
  after the provider's first prediction and no later than the last kickoff it
  predicted. A missing tip on an offered fixture scores 0 (``Avg0``, the
  ranking key, is points per offered fixture; ``Avg`` points per tip).
- Baselines are offered every fixture: ``Odds-Favourite`` ("Favourite 2:1")
  tips a 2:1 win for the favourite, 2:1 for a home and 1:2 for an away
  favourite (higher margin-free win probability, never a draw; no odds = no
  tip), ``Always-2:1`` tips 2:1, ``Consensus`` tips the
  median of every model's tip on the fixture, per side, .5 rounded up (at
  least two tips).
- Matchdays: per league, a new matchday starts when a team plays again or
  after a break of more than three days.
- Backfill predictions (generated later for late-joining providers) count by
  default and are flagged.
"""
# * Author(s): Thomas Glanzer
# * Creation : Nov 2026
# * License: MIT license

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import TYPE_CHECKING, Any

from lib.prediction_table import (
    BACKFILL,
    LIVE,
    RUNS_SEPARATOR,
    parse_key_time,
    parse_run_ts,
)
from lib.results_archive import BERLIN, kicktipp_points

if TYPE_CHECKING:
    from collections.abc import Iterable

# %% --------------------------------------------
# * Constants

ODDS_FAVOURITE = "Odds-Favourite"
ALWAYS_HOME = "Always-2:1"
CONSENSUS = "Consensus"
BASELINES = (ODDS_FAVOURITE, ALWAYS_HOME, CONSENSUS)
BASELINE_LABELS = {
    ODDS_FAVOURITE: "Favourite 2:1 (2:1 / 1:2)",
    ALWAYS_HOME: "Always 2:1",
    CONSENSUS: "Model consensus (median)",
}
ALL = "all"
FROM_SEASON = "2025/26"
MATCHDAY_BREAK = timedelta(days=3)
FORM_STEPS = 5
RECENT_MATCHDAYS = 3
MIN_CONSENSUS_TIPS = 2
# Active models whose first live tip came this long after the newest season's
# first run are "late joiners" (left out of the second fair comparison)
LATE_JOIN_GRACE = timedelta(days=14)
Z_95 = 1.96
STATS_VERSION = 1

LEADERBOARD_COLUMNS = [
    "model",
    "rank",
    "points",
    "offered",
    "tipped",
    "missed",
    "avg",
    "avg0",
    "se",
    "ci_low",
    "ci_high",
    "matchdays",
    "ppmd",
    "exact",
    "diff",
    "tendency",
    "wrong",
    "exact_share",
    "diff_share",
    "tendency_share",
    "wrong_share",
    "missed_share",
    "backfill",
]
FAIR_COLUMNS = ["model", "rank", "points", "missed", "avg0", "se", "ppmd"]
RELIABILITY_COLUMNS = [
    "model",
    "runs",
    "missed_runs",
    "run_offered",
    "run_tipped",
    "run_share",
    "expected",
    "delivered",
    "share",
]

RULES = {
    "scoring": (
        "Kicktipp four points on the score after 90 minutes: 4 exact result, "
        "3 correct goal difference, 2 correct tendency (a draw with the wrong "
        "score is 2), 0 otherwise"
    ),
    "pick": (
        "Per fixture and model the latest prediction made before kickoff; a "
        "rescheduled fixture counts once, with the kickoff of its newest record"
    ),
    "offered": (
        "A finished fixture is offered to a model from its first prediction to "
        "the last kickoff it predicted; a missing tip scores 0. avg0 = points "
        "per offered fixture (ranking key), avg = points per tip, se = standard "
        "error of avg0, ci = avg0 +- 1.96 se, ppmd = points per matchday"
    ),
    "hit_types": (
        "Exclusive: exact = 4, diff = 3, tendency = 2, wrong = 0 points with a "
        "tip, missed = no tip; shares are of offered fixtures"
    ),
    "baselines": (
        "Odds-Favourite (Favourite 2:1): 2:1 home / 1:2 away favourite by "
        "margin-free win probability of the latest odds before kickoff (never "
        "a draw; no odds "
        "= no tip). Always-2:1. Consensus: median of every model's tip per "
        "side, .5 rounded up, at least two tips. Baselines are offered every "
        "fixture"
    ),
    "matchdays": (
        "Per league a new matchday starts when a team plays again or after a "
        "break of more than 3 days"
    ),
    "timeline": (
        "Cumulative points per step: matchdays (one season, one league), ISO "
        "weeks (one season, all leagues) or months (all seasons), Europe/Berlin; "
        "points[model] = [index of its first step, cumulative points up to its "
        "last step with an offered fixture]"
    ),
    "form": (
        f"[avg0, points, offered] over the last {FORM_STEPS} matchdays (one "
        "league) or ISO weeks (all leagues)"
    ),
    "fair": (
        "Leaderboard on the fixtures offered to every active model (missing "
        "tips score 0); 'established' leaves out late joiners"
    ),
    "reliability": (
        "Live predictions only: run_tipped of run_offered fixtures listed in "
        "the runs while the model was active (missed_runs = runs without any "
        "tip); delivered of expected finished fixtures with a tip before kickoff"
    ),
    "backfill": (
        "Backfill tips (generated later for a late-joining model) count by "
        "default; 'backfill' counts them, 'live' rows score without them"
    ),
}


# %% --------------------------------------------
# * Data structures


@dataclass(frozen=True)
class TableTip:
    """One row of the prediction table, parsed."""

    provider: str
    origin: str
    match_key: str
    league: str
    season: str
    kickoff: datetime
    home: str
    away: str
    goals: tuple[int, int]
    predicted_at: datetime
    live_runs: frozenset[datetime] = frozenset()
    odds: tuple[float, float, float] | None = None
    # Kickoff stated in the picked record (differs after a reschedule)
    tip_kickoff: datetime | None = None

    @property
    def own_kickoff(self) -> datetime:
        """Kickoff the model predicted for (its window ends at the latest)."""
        return self.tip_kickoff or self.kickoff


@dataclass(frozen=True)
class Fixture:
    """A finished (scored) fixture."""

    match_key: str
    league: str
    season: str
    kickoff: datetime
    home: str
    away: str
    result: tuple[int, int]
    matchday: str
    odds: tuple[float, float, float] | None = None


@dataclass(frozen=True)
class Entry:
    """One fixture offered to one model; points None if it gave no tip."""

    model: str
    fixture: Fixture
    tip: tuple[int, int] | None
    points: int | None
    origin: str = ""


@dataclass
class Score:
    """Aggregated points of one model; missing tips score 0."""

    offered: int = 0
    tipped: int = 0
    points: int = 0
    squares: int = 0
    exact: int = 0
    diff: int = 0
    tendency: int = 0
    wrong: int = 0
    two_one: int = 0
    backfill: int = 0
    matchdays: set[str] = field(default_factory=set)

    def add(self, entry: Entry) -> None:
        """Count one offered fixture."""
        self.offered += 1
        self.matchdays.add(entry.fixture.matchday)
        self.two_one += entry.tip == (2, 1)
        if entry.points is None:
            return
        self.tipped += 1
        self.backfill += entry.origin == BACKFILL
        self.points += entry.points
        self.squares += entry.points * entry.points
        self.exact += entry.points == 4  # noqa: PLR2004
        self.diff += entry.points == 3  # noqa: PLR2004
        self.tendency += entry.points == 2  # noqa: PLR2004
        self.wrong += entry.points == 0

    @property
    def missed(self) -> int:
        """Offered fixtures without a tip."""
        return self.offered - self.tipped

    @property
    def avg(self) -> float:
        """Points per tip."""
        return self.points / self.tipped if self.tipped else 0.0

    @property
    def avg0(self) -> float:
        """Points per offered fixture (missing tips score 0)."""
        return self.points / self.offered if self.offered else 0.0

    @property
    def ppmd(self) -> float:
        """Points per matchday."""
        return self.points / len(self.matchdays) if self.matchdays else 0.0

    @property
    def std_error(self) -> float:
        """Standard error of the average points per offered fixture."""
        if self.offered < 2:  # noqa: PLR2004
            return 0.0
        mean = self.points / self.offered
        variance = (self.squares - self.offered * mean * mean) / (self.offered - 1)
        return (max(variance, 0.0) / self.offered) ** 0.5


@dataclass
class Coverage:
    """Prediction coverage counts of one model (live predictions only)."""

    runs: int = 0
    missed_runs: int = 0
    offered: int = 0
    predicted: int = 0
    past_fixtures: int = 0
    past_predicted: int = 0


# %% --------------------------------------------
# * Parsing


def parse_table(rows: Iterable[dict[str, str]]) -> list[TableTip]:
    """Parse prediction table rows (``lib.prediction_table.TABLE_COLUMNS``).

    Rows that cannot be parsed are skipped.
    """
    tips = []
    for row in rows:
        try:
            odds = None
            if row.get("odds_home") and row.get("odds_draw") and row.get("odds_away"):
                odds = (
                    float(row["odds_home"]),
                    float(row["odds_draw"]),
                    float(row["odds_away"]),
                )
            runs = frozenset(
                parse_run_ts(r)
                for r in row.get("live_runs", "").split(RUNS_SEPARATOR)
                if r
            )
            tips.append(
                TableTip(
                    row["provider"],
                    row["origin"],
                    row["match_key"],
                    row["league"],
                    row["season"],
                    parse_key_time(row["kickoff_utc"]),
                    row["home_team"],
                    row["away_team"],
                    (int(row["tip_home"]), int(row["tip_away"])),
                    parse_run_ts(row["predicted_at"]),
                    runs,
                    odds,
                    parse_key_time(row["tip_kickoff_utc"])
                    if row.get("tip_kickoff_utc")
                    else None,
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return tips


def parse_results(rows: Iterable[dict[str, str]]) -> dict[str, tuple[int, int]]:
    """Map ``match_key`` to the 90-minute score of the results CSV rows."""
    results = {}
    for row in rows:
        try:
            results[row["match_key"]] = (int(row["home_goals"]), int(row["away_goals"]))
        except (KeyError, TypeError, ValueError):
            continue
    return results


# %% --------------------------------------------
# * Baselines


def odds_favourite(odds: tuple[float, float, float] | None) -> tuple[int, int] | None:
    """Tip a 2:1 win for the favourite (2:1 home, 1:2 away); None without odds.

    The favourite has the higher margin-free win probability; normalising
    1/odds by their sum keeps the order of 1/odds, so the lower price wins
    (home on a tie). Never tips a draw.
    """
    if odds is None:
        return None
    inverse = [1 / value for value in odds]
    total = sum(inverse)
    home, away = inverse[0] / total, inverse[2] / total
    return (2, 1) if home >= away else (1, 2)


def consensus(tips: Iterable[tuple[int, int]]) -> tuple[int, int] | None:
    """Median tip per side, .5 rounded up; None with fewer than two tips."""
    tips = list(tips)
    if len(tips) < MIN_CONSENSUS_TIPS:
        return None
    return (
        math.floor(median(t[0] for t in tips) + 0.5),
        math.floor(median(t[1] for t in tips) + 0.5),
    )


# %% --------------------------------------------
# * Scoring


def cluster_matchdays(
    fixtures: Iterable[tuple[datetime, str, str]], league: str
) -> dict[tuple[datetime, str, str], str]:
    """Group (kickoff, home, away) fixtures of one league into matchdays.

    Every team plays once per matchday, so a new matchday starts as soon as a
    team that already played in the current one appears again, or after a
    break longer than ``MATCHDAY_BREAK``. Rescheduled games can split a real
    matchday in two; that affects all models alike.
    """
    labels: dict[tuple[datetime, str, str], str] = {}
    label = ""
    teams: set[str] = set()
    previous: datetime | None = None
    for fixture in sorted(set(fixtures)):
        kickoff, home, away = fixture
        new_round = home in teams or away in teams
        if previous is None or kickoff - previous > MATCHDAY_BREAK or new_round:
            label = f"{league} {kickoff:%Y-%m-%d}"
            teams = set()
        teams |= {home, away}
        labels[fixture] = label
        previous = kickoff
    return labels


def scored_fixtures(
    tips: list[TableTip], results: dict[str, tuple[int, int]]
) -> dict[str, Fixture]:
    """Return every tipped fixture with a result, keyed by ``match_key``."""
    first: dict[str, TableTip] = {}
    odds: dict[str, tuple[float, float, float]] = {}
    for tip in tips:
        if tip.match_key in results:
            first.setdefault(tip.match_key, tip)
            if tip.odds is not None:
                odds.setdefault(tip.match_key, tip.odds)
    by_league: dict[str, list[tuple[datetime, str, str]]] = defaultdict(list)
    for tip in first.values():
        by_league[tip.league].append((tip.kickoff, tip.home, tip.away))
    matchdays: dict[tuple[str, datetime, str, str], str] = {}
    for league, fixtures in by_league.items():
        for fixture, label in cluster_matchdays(fixtures, league).items():
            matchdays[league, *fixture] = label
    return {
        key: Fixture(
            key,
            tip.league,
            tip.season,
            tip.kickoff,
            tip.home,
            tip.away,
            results[key],
            matchdays[tip.league, tip.kickoff, tip.home, tip.away],
            odds.get(key),
        )
        for key, tip in first.items()
    }


def model_windows(tips: Iterable[TableTip]) -> dict[str, tuple[datetime, datetime]]:
    """Return each model's active window: first prediction to last kickoff.

    The last kickoff is the one stated in its own records, so a fixture
    rescheduled far ahead does not stretch a retired model's window.
    """
    window: dict[str, tuple[datetime, datetime]] = {}
    for tip in tips:
        first = min([tip.predicted_at, *tip.live_runs])
        start, end = window.get(tip.provider, (first, tip.own_kickoff))
        window[tip.provider] = (min(start, first), max(end, tip.own_kickoff))
    return window


def score_entries(
    tips: list[TableTip],
    results: dict[str, tuple[int, int]],
    *,
    include_backfill: bool = True,
    baselines: bool = True,
) -> list[Entry]:
    """Score every fixture each model (and baseline) was offered.

    Parameters
    ----------
    tips : list[TableTip]
        The prediction table rows.
    results : dict[str, tuple[int, int]]
        ``match_key`` -> 90-minute score.
    include_backfill : bool, optional
        Count backfill tips (and their window); False scores live tips only.
    baselines : bool, optional
        Add the baseline rows.

    Returns
    -------
    list[Entry]
        One entry per offered (model, fixture); points None = no tip.

    """
    if not include_backfill:
        tips = [t for t in tips if t.origin != BACKFILL]
    fixtures = scored_fixtures(tips, results)
    windows = model_windows(tips)
    by_fixture: dict[str, dict[str, TableTip]] = defaultdict(dict)
    for tip in tips:
        if tip.match_key in fixtures:
            by_fixture[tip.match_key][tip.provider] = tip
    entries = []
    for key, fixture in fixtures.items():
        fixture_tips = by_fixture[key]
        for model, (first, last) in windows.items():
            if not first < fixture.kickoff <= last:
                continue
            tip = fixture_tips.get(model)
            goals = tip.goals if tip else None
            points = kicktipp_points(goals, fixture.result) if goals else None
            entries.append(
                Entry(model, fixture, goals, points, tip.origin if tip else "")
            )
        if not baselines:
            continue
        for model, goals in (
            (ODDS_FAVOURITE, odds_favourite(fixture.odds)),
            (ALWAYS_HOME, (2, 1)),
            (CONSENSUS, consensus(t.goals for t in fixture_tips.values())),
        ):
            points = kicktipp_points(goals, fixture.result) if goals else None
            entries.append(Entry(model, fixture, goals, points))
    return entries


def aggregate(entries: Iterable[Entry]) -> dict[str, Score]:
    """Aggregate entries per model."""
    scores: dict[str, Score] = defaultdict(Score)
    for entry in entries:
        scores[entry.model].add(entry)
    return dict(scores)


def ranked(scores: dict[str, Score]) -> list[tuple[str, Score]]:
    """Order models by points per offered fixture, then by offered fixtures."""
    return sorted(
        ((m, s) for m, s in scores.items() if s.offered),
        key=lambda item: (-item[1].avg0, -item[1].offered, item[0]),
    )


def common_entries(entries: list[Entry], group: set[str]) -> list[Entry]:
    """Keep entries of fixtures offered to every model of ``group``.

    Returns the group's and the baselines' entries on those fixtures.
    """
    by_fixture: dict[str, dict[str, Entry]] = defaultdict(dict)
    for entry in entries:
        by_fixture[entry.fixture.match_key][entry.model] = entry
    return [
        entry
        for models in by_fixture.values()
        if group <= models.keys()
        for model, entry in models.items()
        if model in group or model in BASELINES
    ]


def reliability(
    tips: list[TableTip], now: datetime, *, windows_from: list[TableTip] | None = None
) -> dict[str, Coverage]:
    """Measure how many fixtures each model actually predicted (live only).

    Per run, a fixture any model tipped counts as offered to every model
    active then (between its first and last run, from ``windows_from``,
    default ``tips``); a model with no tip in such a run missed the whole run.
    Pre-kickoff coverage counts past fixtures (kickoff before ``now``) in the
    model's window that got a tip before kickoff, since a later run can fill
    a per-run gap.
    """
    tips = [t for t in tips if t.origin == LIVE]
    source = [t for t in (windows_from or tips) if t.origin == LIVE]
    run_window: dict[str, tuple[datetime, datetime]] = {}
    last_kickoff: dict[str, datetime] = {}
    for tip in source:
        if tip.live_runs:
            first, last = run_window.get(
                tip.provider, (min(tip.live_runs), max(tip.live_runs))
            )
            run_window[tip.provider] = (
                min(first, *tip.live_runs),
                max(last, *tip.live_runs),
            )
        last_kickoff[tip.provider] = max(
            last_kickoff.get(tip.provider, tip.own_kickoff), tip.own_kickoff
        )
    offered: dict[datetime, set[str]] = defaultdict(set)
    delivered: dict[tuple[str, datetime], set[str]] = defaultdict(set)
    for tip in tips:
        for run in tip.live_runs:
            offered[run].add(tip.match_key)
            delivered[tip.provider, run].add(tip.match_key)
    coverage: dict[str, Coverage] = defaultdict(Coverage)
    for model, (first, last) in run_window.items():
        for run, fixtures in offered.items():
            if not first <= run <= last:
                continue
            got = delivered.get((model, run), set())
            cov = coverage[model]
            cov.runs += 1
            cov.missed_runs += not got
            cov.offered += len(fixtures)
            cov.predicted += len(got & fixtures)
    past: dict[str, datetime] = {
        t.match_key: t.kickoff for t in tips if t.kickoff < now
    }
    tipped: dict[str, set[str]] = defaultdict(set)
    for tip in tips:
        tipped[tip.provider].add(tip.match_key)
    for model, (first, _) in run_window.items():
        for key, kickoff in past.items():
            if first < kickoff <= last_kickoff[model]:
                coverage[model].past_fixtures += 1
                coverage[model].past_predicted += key in tipped[model]
    return dict(coverage)


# %% --------------------------------------------
# * JSON


def _r(value: float, digits: int = 3) -> float:
    """Round for the JSON."""
    return round(value, digits)


def _share(count: int, total: int) -> float:
    return _r(count / total) if total else 0.0


def leaderboard_rows(scores: dict[str, Score], ids: dict[str, int]) -> list[list[Any]]:
    """Leaderboard rows (``LEADERBOARD_COLUMNS``, model as index), best first."""
    rows = []
    for rank, (model, s) in enumerate(ranked(scores), 1):
        se = s.std_error
        rows.append(
            [
                ids[model],
                rank,
                s.points,
                s.offered,
                s.tipped,
                s.missed,
                _r(s.avg),
                _r(s.avg0),
                _r(se),
                _r(s.avg0 - Z_95 * se),
                _r(s.avg0 + Z_95 * se),
                len(s.matchdays),
                _r(s.ppmd, 2),
                s.exact,
                s.diff,
                s.tendency,
                s.wrong,
                _share(s.exact, s.offered),
                _share(s.diff, s.offered),
                _share(s.tendency, s.offered),
                _share(s.wrong, s.offered),
                _share(s.missed, s.offered),
                s.backfill,
            ]
        )
    return rows


def fair_rows(scores: dict[str, Score], ids: dict[str, int]) -> list[list[Any]]:
    """Compact leaderboard rows (``FAIR_COLUMNS``) for the fair comparison."""
    return [
        [
            ids[model],
            rank,
            s.points,
            s.missed,
            _r(s.avg0),
            _r(s.std_error),
            _r(s.ppmd, 2),
        ]
        for rank, (model, s) in enumerate(ranked(scores), 1)
    ]


def iso_week(moment: datetime) -> str:
    """Return the ISO week of a kickoff in Berlin time, e.g. ``2026-W45``."""
    year, week, _ = moment.astimezone(BERLIN).isocalendar()
    return f"{year}-W{week:02d}"


def step_of(fixture: Fixture, step: str) -> tuple[str, str]:
    """Return a fixture's timeline step: sort key and label.

    ``matchday`` (label: its first day), ``week`` (ISO week) or ``month``,
    all in Berlin time.
    """
    local = fixture.kickoff.astimezone(BERLIN)
    if step == "week":
        return iso_week(fixture.kickoff), iso_week(fixture.kickoff)
    if step == "month":
        return local.strftime("%Y-%m"), local.strftime("%Y-%m")
    return fixture.matchday, ""


def per_step(
    entries: list[Entry], step: str
) -> tuple[list[str], dict[str, list[int]], dict[str, list[int]]]:
    """Sum points and offered fixtures per model and step (in time order)."""
    key_of: dict[str, str] = {}
    first: dict[str, datetime] = {}
    labels: dict[str, str] = {}
    for entry in entries:
        fixture = entry.fixture
        key, label = step_of(fixture, step)
        key_of[fixture.match_key] = key
        first[key] = min(first.get(key, fixture.kickoff), fixture.kickoff)
        labels[key] = label
    steps = sorted(first, key=lambda k: (first[k], k))
    position = {key: i for i, key in enumerate(steps)}
    points: dict[str, list[int]] = defaultdict(lambda: [0] * len(steps))
    offered: dict[str, list[int]] = defaultdict(lambda: [0] * len(steps))
    for entry in entries:
        i = position[key_of[entry.fixture.match_key]]
        points[entry.model][i] += entry.points or 0
        offered[entry.model][i] += 1
    names = [labels[k] or first[k].astimezone(BERLIN).date().isoformat() for k in steps]
    return names, dict(points), dict(offered)


def timeline(
    entries: list[Entry], *, step: str, form_step: str, ids: dict[str, int]
) -> dict[str, Any]:
    """Cumulative points per step and each model's recent form.

    ``points`` holds ``[model, start, values]``: the model's cumulative points
    from step ``start`` (its first step with an offered fixture) to its last
    such step, so lines of models that joined late or stopped stay short.
    ``form`` holds ``[model, avg0, points, offered]`` over the last
    ``FORM_STEPS`` steps of ``form_step`` (matchdays or weeks, finer than
    months).
    """
    steps, points, offered = per_step(entries, step)
    lines: list[list[Any]] = []
    for model in sorted(points, key=ids.__getitem__):
        active = [i for i, n in enumerate(offered[model]) if n]
        if not active:
            continue
        total, values = sum(points[model][: active[0]]), []
        for i in range(active[0], active[-1] + 1):
            total += points[model][i]
            values.append(total)
        lines.append([ids[model], active[0], values])
    _, form_points, form_offered = per_step(entries, form_step)
    form: list[list[Any]] = []
    for model in sorted(form_points, key=ids.__getitem__):
        recent_points = sum(form_points[model][-FORM_STEPS:])
        recent_offered = sum(form_offered[model][-FORM_STEPS:])
        if recent_offered:
            form.append(
                [
                    ids[model],
                    _r(recent_points / recent_offered),
                    recent_points,
                    recent_offered,
                ]
            )
    return {"step": step, "steps": steps, "points": lines, "form": form}


def reliability_rows(
    coverage: dict[str, Coverage], ids: dict[str, int]
) -> list[list[Any]]:
    """Reliability rows (``RELIABILITY_COLUMNS``), least reliable last."""
    rows = [
        [
            ids[model],
            c.runs,
            c.missed_runs,
            c.offered,
            c.predicted,
            _share(c.predicted, c.offered),
            c.past_fixtures,
            c.past_predicted,
            _share(c.past_predicted, c.past_fixtures),
        ]
        for model, c in coverage.items()
        if c.offered or c.past_fixtures
    ]
    rows.sort(key=lambda row: (-row[5], row[0]))
    return rows


def late_joiners(tips: list[TableTip], active: list[str]) -> list[str]:
    """Active models whose first live tip came well into the newest season."""
    live = [t for t in tips if t.origin == LIVE]
    if not live:
        return []
    newest = max(t.season for t in live)
    season_first = min(t.predicted_at for t in live if t.season == newest)
    first_live: dict[str, datetime] = {}
    for tip in live:
        first_live[tip.provider] = min(
            first_live.get(tip.provider, tip.predicted_at), tip.predicted_at
        )
    return sorted(
        m
        for m in active
        if m in first_live and first_live[m] > season_first + LATE_JOIN_GRACE
    )


def model_info(
    tips: list[TableTip], active: list[str], late: list[str]
) -> list[dict[str, Any]]:
    """Describe every model and baseline; rows refer to them by list index.

    Active models first, then retired ones, then the baselines.
    """
    info: list[dict[str, Any]] = []
    by_model: dict[str, list[TableTip]] = defaultdict(list)
    for tip in tips:
        by_model[tip.provider].append(tip)
    for model in sorted(by_model, key=lambda m: (m not in active, m.lower())):
        model_tips = by_model[model]
        live = [t.predicted_at for t in model_tips if t.origin == LIVE]
        backfill = [t for t in model_tips if t.origin == BACKFILL]
        info.append(
            {
                "id": model,
                "label": model,
                "kind": "llm",
                "active": model in active,
                "late_joiner": model in late,
                "first_live": min(live).date().isoformat() if live else None,
                "last_tip": max(t.predicted_at for t in model_tips).date().isoformat(),
                "backfill": {
                    "tips": len(backfill),
                    "first_kickoff": min(t.kickoff for t in backfill)
                    .date()
                    .isoformat(),
                    "last_kickoff": max(t.kickoff for t in backfill).date().isoformat(),
                }
                if backfill
                else None,
            }
        )
    info.extend(
        {
            "id": model,
            "label": BASELINE_LABELS[model],
            "kind": "baseline",
            "active": True,
            "late_joiner": False,
            "first_live": None,
            "last_tip": None,
            "backfill": None,
        }
        for model in BASELINES
    )
    return info


def slice_stats(  # noqa: PLR0913
    entries: list[Entry],
    live_entries: list[Entry],
    tips: list[TableTip],
    all_tips: list[TableTip],
    *,
    active: list[str],
    late: list[str],
    step: str,
    form_step: str,
    now: datetime,
    ids: dict[str, int],
) -> dict[str, Any]:
    """Compute one slice (season x league)."""
    scores = aggregate(entries)
    with_backfill = {m for m, s in scores.items() if s.backfill}
    live_scores = aggregate(e for e in live_entries if e.model in with_backfill)
    fair = []
    group = {m for m in active if m in scores}
    groups = [("active", group)]
    if (established := group - set(late)) != group and len(established) > 1:
        groups.append(("established", established))
    for name, members in groups:
        if len(members) < 2:  # noqa: PLR2004
            continue
        common = common_entries(entries, members)
        fair.append(
            {
                "group": name,
                "models": sorted(ids[m] for m in members),
                "fixtures": len({e.fixture.match_key for e in common}),
                "rows": fair_rows(aggregate(common), ids),
            }
        )
    return {
        "fixtures": len({e.fixture.match_key for e in entries}),
        "matchdays": len({e.fixture.matchday for e in entries}),
        "leaderboard": leaderboard_rows(scores, ids),
        "live": leaderboard_rows(live_scores, ids),
        "timeline": timeline(entries, step=step, form_step=form_step, ids=ids),
        "fair": fair,
        "reliability": reliability_rows(
            reliability(tips, now, windows_from=all_tips), ids
        ),
    }


def recent_matchdays(
    entries: list[Entry], models: list[str], season: str, ids: dict[str, int]
) -> dict[str, Any]:
    """Return the last ``RECENT_MATCHDAYS`` matchdays per league of ``season``.

    ``models`` (as indices) gives the order of each fixture's tips.
    """
    by_fixture: dict[str, dict[str, Entry]] = defaultdict(dict)
    fixtures: dict[str, Fixture] = {}
    for entry in entries:
        if entry.fixture.season == season:
            by_fixture[entry.fixture.match_key][entry.model] = entry
            fixtures[entry.fixture.match_key] = entry.fixture
    by_league: dict[str, dict[str, list[Fixture]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for fixture in fixtures.values():
        by_league[fixture.league][fixture.matchday].append(fixture)
    leagues: dict[str, list[dict[str, Any]]] = {}
    for league in sorted(by_league):
        matchdays = sorted(
            by_league[league].values(), key=lambda fs: min(f.kickoff for f in fs)
        )[-RECENT_MATCHDAYS:]
        leagues[league] = []
        for day in reversed(matchdays):
            rows = []
            for fixture in sorted(day, key=lambda f: (f.kickoff, f.home)):
                tips: list[Any] = []
                for model in models:
                    entry = by_fixture[fixture.match_key].get(model)
                    if entry is None or entry.tip is None:
                        tips.append(None)
                        continue
                    tip = [*entry.tip, entry.points]
                    tips.append([*tip, 1] if entry.origin == BACKFILL else tip)
                rows.append(
                    [
                        fixture.kickoff.strftime("%Y-%m-%dT%H:%MZ"),
                        fixture.home,
                        fixture.away,
                        *fixture.result,
                        tips,
                    ]
                )
            start = min(f.kickoff for f in day).astimezone(BERLIN).date().isoformat()
            leagues[league].append({"matchday": start, "fixtures": rows})
    return {
        "season": season,
        "models": [ids[m] for m in models],
        "leagues": leagues,
    }


def build_stats(
    table_rows: Iterable[dict[str, str]],
    result_rows: Iterable[dict[str, str]],
    *,
    active: list[str],
    now: datetime | None = None,
    from_season: str = FROM_SEASON,
) -> dict[str, Any]:
    """Compute the dashboard statistics JSON.

    Parameters
    ----------
    table_rows : Iterable[dict[str, str]]
        Prediction table rows of every season.
    result_rows : Iterable[dict[str, str]]
        Results CSV rows of every season.
    active : list[str]
        Models currently run by the workflow (``llm_provider_options``).
    now : datetime | None, optional
        Generation time.
    from_season : str, optional
        First season to include.

    Returns
    -------
    dict[str, Any]
        ``version``, ``meta``, ``models``, ``slices`` (keyed ``season|league``
        with ``all`` for every season or league) and ``recent``.

    """
    now = now or datetime.now(tz=UTC)
    tips = [t for t in parse_table(table_rows) if t.season >= from_season]
    results = parse_results(result_rows)
    entries = score_entries(tips, results)
    live_entries = score_entries(tips, results, include_backfill=False, baselines=False)
    late = late_joiners(tips, active)
    seasons = sorted({e.fixture.season for e in entries})
    leagues = sorted({e.fixture.league for e in entries})
    models = model_info(tips, active, late)
    ids = {model["id"]: i for i, model in enumerate(models)}

    slices: dict[str, Any] = {}
    for season in [*seasons, ALL]:
        for league in [*leagues, ALL]:

            def wanted(
                fixture: Fixture | TableTip, s: str = season, lg: str = league
            ) -> bool:
                return s in (ALL, fixture.season) and lg in (ALL, fixture.league)

            chosen = [e for e in entries if wanted(e.fixture)]
            if not chosen:
                continue
            slices[f"{season}|{league}"] = slice_stats(
                chosen,
                [e for e in live_entries if wanted(e.fixture)],
                [t for t in tips if wanted(t)],
                tips,
                active=active,
                late=late,
                step="month"
                if season == ALL
                else "week"
                if league == ALL
                else "matchday",
                form_step="week" if league == ALL else "matchday",
                now=now,
                ids=ids,
            )
    scored = {e.fixture.match_key: e.fixture for e in entries}
    recent_models = [m for m in active if any(t.provider == m for t in tips)]
    return {
        "version": STATS_VERSION,
        "meta": {
            "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "data_through": max(
                (f.kickoff for f in scored.values()), default=now
            ).strftime("%Y-%m-%dT%H:%MZ"),
            "matches": len(scored),
            "tips": len(tips),
            "seasons": seasons,
            "leagues": leagues,
            "slice_key": "<season>|<league>, 'all' for every season or league",
            "model_ref": "models are referred to by their index in 'models'",
            "columns": {
                "leaderboard": LEADERBOARD_COLUMNS,
                "fair": FAIR_COLUMNS,
                "reliability": RELIABILITY_COLUMNS,
                "recent": "kickoff_utc, home, away, home_goals, away_goals, tips "
                "(per recent.models: [home, away, points(, 1 = backfill)] or null)",
            },
            "rules": RULES,
        },
        "models": models,
        "slices": slices,
        "recent": recent_matchdays(
            entries,
            [*recent_models, ODDS_FAVOURITE, CONSENSUS],
            seasons[-1] if seasons else "",
            ids,
        ),
    }


def to_json_bytes(stats: dict[str, Any]) -> bytes:
    """Serialize compactly (what goes to KV)."""
    return json.dumps(stats, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
