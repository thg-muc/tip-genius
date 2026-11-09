"""Prediction table in Vercel Blob: one row per fixture and provider and season.

``stats/predictions_<season>.csv`` (season as ``2026-27``) is the input of the
dashboard statistics (``lib.stats``). It is derived from the archive only:

- prediction files (``predictions/`` live, ``predictions/backfill/`` added
  later for late-joining providers) listed in ``index.json``
- odds files (``odds/``) and, for fixtures without them, the newest
  reconstructed odds CSV (``odds/reconstructed/``, estimated)

Pick rule (as ``utils/eval_providers.py`` always did): per fixture and
provider the LATEST prediction made BEFORE kickoff. A fixture is a pairing
(league, season, home, away) so that a rescheduled kickoff collapses into one
fixture; its kickoff is the one in the newest prediction record (any
provider), and a prediction only counts if it was made before both its own
and that kickoff. In UEFA competitions the same pairing can occur twice a
season, so their kickoffs are split into legs more than ``LEG_GAP`` apart.

``match_key`` is ``results_archive.match_key`` of that fixture, so a row joins
to its result without fuzzy matching. The odds are those of the latest odds
file (``odds_at`` = its ``run_ts``) before kickoff that lists the fixture,
else the reconstructed CSV (``odds_source`` says which). ``live_runs`` lists
the live runs in which the provider tipped the fixture before kickoff (for
the per-run reliability).

Maintained incrementally like the results archive (``results_archive.plan_scan``):
the season's state in ``index.json`` (``stats_scan``) records the newest
``run_ts`` read and a digest of every file up to it. A run reads only newer
prediction and odds files and merges them into the existing rows; it reads
every prediction file of the season instead (full scan) when the state is
missing or invalid, older files changed, or the last full scan is older than
``FULL_SCAN_INTERVAL`` (and whenever the results step scanned fully in the same
run, sharing its reads). Odds are carried over from the existing rows in a
full scan; all odds files are read only when the odds files up to the state
changed or the table is new. Guards: never shrink (row count in
``index.json`` ``stats``), fresh reads, read-back verification, and the state
only advances after a complete run.
"""
# * Author(s): Thomas Glanzer
# * Creation : Nov 2026
# * License: MIT license

from __future__ import annotations

import csv
import io
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from lib import blob_archive, results_archive
from lib.results_archive import (
    FULL_SCAN_INTERVAL,
    REPEATED_PAIRINGS,
    RUN_LOOKBACK,
    format_kickoff,
    match_key,
    parse_commence_time,
    paths_digest,
    season_of,
    season_start,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

logger = logging.getLogger(__name__)

# %% --------------------------------------------
# * Constants

TABLE_PREFIX = "stats/predictions_"
TABLE_COLUMNS = [
    "match_key",
    "league",
    "season",
    "kickoff_utc",
    "home_team",
    "away_team",
    "provider",
    "tip_home",
    "tip_away",
    "predicted_at",
    "origin",
    "tip_kickoff_utc",
    "live_runs",
    "odds_home",
    "odds_draw",
    "odds_away",
    "odds_at",
    "odds_source",
]
LIVE, BACKFILL = "live", "backfill"
ODDS_LOGGED, ODDS_RECONSTRUCTED = "logged", "reconstructed"
# Only the prompt variant the workflow runs is part of the statistics
PREDICTION_FILE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})_Match_Predictions_(.+)"
    r"_FourPointsScoring_named_with-info\.jsonl$"
)
BASE_KEY_SUFFIX = "_FourPointsScoring_named_with-info"
# UEFA pairings with kickoffs further apart are different legs
LEG_GAP = timedelta(days=7)
# A reconstructed odds row applies to a fixture this close to its kickoff
RECONSTRUCTED_TOLERANCE = timedelta(days=21)
RUNS_SEPARATOR = ";"


# %% --------------------------------------------
# * Data structures


@dataclass(frozen=True)
class Tip:
    """One provider's prediction for one fixture from one file (or table row)."""

    provider: str
    origin: str  # live | backfill
    predicted_at: datetime
    league: str
    kickoff: datetime  # kickoff stated in the prediction record
    home: str
    away: str
    goals: tuple[int, int]
    live_runs: frozenset[str] = frozenset()
    # Fixture kickoff this tip is grouped by (table rows: their fixture's)
    fixture_kickoff: datetime | None = None

    @property
    def anchor(self) -> datetime:
        """Kickoff used to assign the tip to a fixture (leg)."""
        return self.fixture_kickoff or self.kickoff


@dataclass(frozen=True)
class Quote:
    """1X2 odds of one fixture from an odds file, a CSV or a table row."""

    league: str
    kickoff: datetime
    home: str
    away: str
    odds: tuple[float, float, float]
    at: str  # run_ts of the odds file; "" for the CSV
    source: str  # logged | reconstructed


@dataclass(frozen=True)
class Observation:
    """A kickoff stated for a pairing at some time (the newest one wins)."""

    seen: datetime
    kickoff: datetime


def table_pathname(season: str) -> str:
    """Return the prediction table pathname of a season (``2026/27`` style)."""
    return f"{TABLE_PREFIX}{season.replace('/', '-')}.csv"


def parse_run_ts(text: str) -> datetime:
    """Parse a ``run_ts`` (``YYYY-MM-DD_HH-MM-SS``, UTC)."""
    return datetime.strptime(text, blob_archive.RUN_TS_FORMAT).replace(tzinfo=UTC)


def parse_key_time(text: str) -> datetime:
    """Parse a ``YYYY-MM-DDTHH:MMZ`` kickoff."""
    return datetime.strptime(text, results_archive.KEY_TIME_FORMAT).replace(tzinfo=UTC)


def pairing_of(league: str, kickoff: datetime, home: str, away: str) -> tuple:
    """Return the pairing key ``(league, season, home, away)``."""
    return (league, season_of(kickoff), home, away)


# %% --------------------------------------------
# * CSV


def rows_to_csv(rows: Iterable[dict[str, str]]) -> bytes:
    """Serialize table rows sorted by kickoff, key and provider."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=TABLE_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in sorted(rows, key=row_sort_key):
        writer.writerow({column: row.get(column, "") for column in TABLE_COLUMNS})
    return buffer.getvalue().encode("utf-8")


def rows_from_csv(body: bytes) -> list[dict[str, str]]:
    """Parse a prediction table; the header must match ``TABLE_COLUMNS``.

    Raises
    ------
    ValueError
        If the header differs, so a foreign file is never extended.

    """
    reader = csv.DictReader(io.StringIO(body.decode("utf-8")))
    if reader.fieldnames and list(reader.fieldnames) != TABLE_COLUMNS:
        msg = f"Unexpected prediction table header: {reader.fieldnames}"
        raise ValueError(msg)
    return list(reader)


def row_sort_key(row: dict[str, str]) -> tuple[str, str, str]:
    """Sort rows by kickoff, ``match_key`` and provider."""
    return (row["kickoff_utc"], row["match_key"], row["provider"])


def row_identity(row: dict[str, str]) -> tuple[str, str, str]:
    """Return what a read-back must show: fixture, provider and pick."""
    return (row["match_key"], row["provider"], row["predicted_at"])


# %% --------------------------------------------
# * Reading archive files


def parse_prediction_pathname(pathname: str) -> tuple[str, str, str] | None:
    """Return ``(file_ts, provider, origin)`` of a prediction file, else None.

    Live files carry the time they were written, backfill files the
    ``run_ts`` of the past run they were generated for.
    """
    if pathname.startswith(blob_archive.BACKFILL_PREFIX):
        parsed = blob_archive.parse_backfill_pathname(pathname)
        if parsed is None or not parsed[1].endswith(BASE_KEY_SUFFIX):
            return None
        return parsed[0], parsed[1].removesuffix(BASE_KEY_SUFFIX), BACKFILL
    found = PREDICTION_FILE.match(pathname.removeprefix("predictions/"))
    if not found or not pathname.startswith("predictions/"):
        return None
    return found[1], found[2], LIVE


def tips_from_lines(
    lines: list[dict[str, Any]],
    pathname: str,
    run_ts: str,
    season: str,
) -> list[Tip]:
    """Turn one prediction file into tips of fixtures in ``season``."""
    parsed = parse_prediction_pathname(pathname)
    if parsed is None:
        return []
    file_ts, provider, origin = parsed
    predicted_at = parse_run_ts(file_ts)
    runs = frozenset({run_ts}) if origin == LIVE else frozenset()
    tips = []
    for line in lines:
        league = line.get("name", "")
        for record in line.get("matches", []):
            try:
                kickoff = parse_commence_time(record["commence_time_str"])
                goals = (int(record["prediction_home"]), int(record["prediction_away"]))
                home, away = record["home_team"], record["away_team"]
            except (KeyError, TypeError, ValueError):
                continue
            if not league or not home or not away or min(goals) < 0:
                continue
            if season_of(kickoff) != season:
                continue
            tips.append(
                Tip(
                    provider,
                    origin,
                    predicted_at,
                    league,
                    kickoff,
                    home,
                    away,
                    goals,
                    runs,
                )
            )
    return tips


def valid_odds(home: Any, draw: Any, away: Any) -> tuple[float, float, float] | None:
    """Return the three odds as floats, or None unless all are positive."""
    try:
        values = (float(home), float(draw), float(away))
    except (TypeError, ValueError):
        return None
    return values if all(v > 0 for v in values) else None


def quotes_from_lines(
    lines: list[dict[str, Any]], run_ts: str, season: str
) -> list[Quote]:
    """Turn one odds file (``odds/``) into quotes of fixtures in ``season``."""
    quotes = []
    for line in lines:
        league = line.get("name", "")
        columns = {name: i for i, name in enumerate(line.get("columns", []))}
        for row in line.get("matches", []):
            try:
                kickoff = datetime.fromisoformat(
                    row[columns["commence_time"]]
                ).astimezone(UTC)
                home = row[columns["home_team"]]
                away = row[columns["away_team"]]
                odds = valid_odds(
                    row[columns["odds_home"]],
                    row[columns["odds_draw"]],
                    row[columns["odds_away"]],
                )
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            if odds is None or season_of(kickoff) != season:
                continue
            quotes.append(Quote(league, kickoff, home, away, odds, run_ts, ODDS_LOGGED))
    return quotes


def quotes_from_reconstructed(body: bytes, season: str) -> list[Quote]:
    """Read the reconstructed odds CSV (``utils/reconstruct_odds.py``).

    Rows the CSV marks ``logged`` were taken from real odds files and keep
    that source; all others are estimated (``reconstructed``).
    """
    quotes = []
    for row in csv.DictReader(io.StringIO(body.decode("utf-8"))):
        odds = valid_odds(
            row.get("odds_home"), row.get("odds_draw"), row.get("odds_away")
        )
        try:
            kickoff = datetime.fromisoformat(row["kickoff_utc"]).astimezone(UTC)
            league, home, away = row["league"], row["home_team"], row["away_team"]
        except (KeyError, TypeError, ValueError):
            continue
        if odds is None or not (league and home and away):
            continue
        if season_of(kickoff) != season:
            continue
        source = ODDS_LOGGED if row.get("source") == ODDS_LOGGED else ODDS_RECONSTRUCTED
        quotes.append(
            Quote(
                league,
                kickoff,
                home,
                away,
                odds,
                "",
                source,
            )
        )
    return quotes


def from_rows(
    rows: Iterable[dict[str, str]],
) -> tuple[list[Tip], list[tuple[tuple, Observation]], list[Quote]]:
    """Turn existing table rows back into tips, kickoff observations and odds.

    Raises
    ------
    ValueError
        If a row cannot be parsed (the table is not touched then).

    """
    tips, observations, quotes = [], [], []
    for row in rows:
        kickoff = parse_key_time(row["kickoff_utc"])
        predicted_at = parse_run_ts(row["predicted_at"])
        runs = frozenset(r for r in row["live_runs"].split(RUNS_SEPARATOR) if r)
        home, away, league = row["home_team"], row["away_team"], row["league"]
        tips.append(
            Tip(
                row["provider"],
                row["origin"],
                predicted_at,
                league,
                parse_key_time(row["tip_kickoff_utc"]),
                home,
                away,
                (int(row["tip_home"]), int(row["tip_away"])),
                runs,
                kickoff,
            )
        )
        pairing = pairing_of(league, kickoff, home, away)
        observations.append((pairing, Observation(predicted_at, kickoff)))
        odds = valid_odds(row["odds_home"], row["odds_draw"], row["odds_away"])
        if odds is not None:
            quotes.append(
                Quote(
                    league,
                    kickoff,
                    home,
                    away,
                    odds,
                    row["odds_at"],
                    row["odds_source"],
                )
            )
    return tips, observations, quotes


# %% --------------------------------------------
# * Building rows


def canonical_names(tips: Iterable[Tip]) -> dict[str, str]:
    """Map case variants of provider names to the most used spelling."""
    counts: dict[str, int] = defaultdict(int)
    for tip in tips:
        counts[tip.provider] += 1
    by_lower: dict[str, list[str]] = defaultdict(list)
    for name in counts:
        by_lower[name.lower()].append(name)
    return {
        name: max(variants, key=lambda v: (counts[v], v))
        for variants in by_lower.values()
        for name in variants
    }


def split_legs(league: str, kickoffs: Iterable[datetime]) -> list[list[datetime]]:
    """Group a pairing's kickoffs into legs (UEFA only: gap > ``LEG_GAP``)."""
    ordered = sorted(set(kickoffs))
    if not ordered:
        return []
    if league not in REPEATED_PAIRINGS:
        return [ordered]
    legs = [[ordered[0]]]
    for kickoff in ordered[1:]:
        if kickoff - legs[-1][-1] > LEG_GAP:
            legs.append([])
        legs[-1].append(kickoff)
    return legs


def leg_of(legs: list[list[datetime]], kickoff: datetime) -> int | None:
    """Return the leg a kickoff belongs to (nearest within ``LEG_GAP``)."""
    if len(legs) == 1:
        return 0
    best, best_gap = None, LEG_GAP
    for i, leg in enumerate(legs):
        gap = min(abs(kickoff - k) for k in leg)
        if gap <= best_gap:
            best, best_gap = i, gap
    return best


def pick_odds(
    quotes: list[Quote], kickoff: datetime, csv_quotes: list[Quote] | None
) -> Quote | None:
    """Pick the odds of one fixture.

    The latest odds file before kickoff wins; else the nearest reconstructed
    CSV row (within ``RECONSTRUCTED_TOLERANCE``) when the CSV was read; else
    odds carried over from the table without an odds file time.
    """
    logged = [
        q
        for q in quotes
        if q.at and q.source == ODDS_LOGGED and parse_run_ts(q.at) < kickoff
    ]
    if logged:
        return max(logged, key=lambda q: q.at)
    if csv_quotes:
        near = [
            q for q in csv_quotes if abs(q.kickoff - kickoff) <= RECONSTRUCTED_TOLERANCE
        ]
        if near:
            return min(near, key=lambda q: abs(q.kickoff - kickoff))
    carried = [q for q in quotes if not q.at]
    return carried[0] if carried else None


def format_odds(value: float) -> str:
    """Format one odds value (round-trips through the CSV)."""
    return repr(round(value, 4))


def build_rows(
    tips: list[Tip],
    observations: list[tuple[tuple, Observation]],
    quotes: list[Quote],
    csv_quotes: list[Quote] | None = None,
) -> list[dict[str, str]]:
    """Apply the pick rule and attach odds; return the table rows.

    Parameters
    ----------
    tips : list[Tip]
        Every candidate prediction: tips from prediction files and the
        existing rows (``from_rows``).
    observations : list[tuple[tuple, Observation]]
        Extra kickoff observations per pairing (existing rows); every tip
        from a file is an observation by itself.
    quotes : list[Quote]
        Odds from odds files and existing rows.
    csv_quotes : list[Quote] | None, optional
        Reconstructed CSV odds (fallback), if read.

    Returns
    -------
    list[dict[str, str]]
        One row per fixture and provider with a valid pre-kickoff tip.

    """
    names = canonical_names(tips)
    by_pairing_obs: dict[tuple, list[Observation]] = defaultdict(list)
    for pairing, obs in observations:
        by_pairing_obs[pairing].append(obs)
    by_pairing_tips: dict[tuple, list[Tip]] = defaultdict(list)
    for tip in tips:
        pairing = pairing_of(tip.league, tip.anchor, tip.home, tip.away)
        by_pairing_tips[pairing].append(tip)
        if tip.fixture_kickoff is None:
            by_pairing_obs[pairing].append(Observation(tip.predicted_at, tip.kickoff))
    by_pairing_quotes: dict[tuple, list[Quote]] = defaultdict(list)
    for quote in quotes:
        by_pairing_quotes[
            pairing_of(quote.league, quote.kickoff, quote.home, quote.away)
        ].append(quote)
    by_pairing_csv: dict[tuple, list[Quote]] = defaultdict(list)
    for quote in csv_quotes or []:
        by_pairing_csv[
            pairing_of(quote.league, quote.kickoff, quote.home, quote.away)
        ].append(quote)

    rows: list[dict[str, str]] = []
    for pairing, pairing_tips in by_pairing_tips.items():
        league, season, home, away = pairing
        obs = by_pairing_obs[pairing]
        legs = split_legs(league, (o.kickoff for o in obs))
        leg_obs: dict[int, list[Observation]] = defaultdict(list)
        for o in obs:
            if (i := leg_of(legs, o.kickoff)) is not None:
                leg_obs[i].append(o)
        leg_tips: dict[int, list[Tip]] = defaultdict(list)
        for tip in pairing_tips:
            if (i := leg_of(legs, tip.anchor)) is not None:
                leg_tips[i].append(tip)
        leg_quotes: dict[int, list[Quote]] = defaultdict(list)
        for quote in by_pairing_quotes.get(pairing, []):
            if (i := leg_of(legs, quote.kickoff)) is not None:
                leg_quotes[i].append(quote)
        for i, candidates in leg_tips.items():
            kickoff = max(leg_obs[i], key=lambda o: (o.seen, o.kickoff)).kickoff
            odds = pick_odds(
                leg_quotes.get(i, []),
                kickoff,
                [
                    q
                    for q in by_pairing_csv.get(pairing, [])
                    if leg_of(legs, q.kickoff) == i
                ]
                or None,
            )
            rows.extend(
                _leg_rows(
                    (league, season, home, away),
                    kickoff,
                    candidates,
                    names,
                    odds,
                )
            )
    rows.sort(key=row_sort_key)
    return rows


def _leg_rows(
    pairing: tuple,
    kickoff: datetime,
    candidates: list[Tip],
    names: dict[str, str],
    odds: Quote | None,
) -> list[dict[str, str]]:
    """Return the rows of one fixture: the pick per provider."""
    league, season, home, away = pairing
    by_provider: dict[str, list[Tip]] = defaultdict(list)
    for tip in candidates:
        by_provider[names.get(tip.provider, tip.provider)].append(tip)
    rows = []
    for provider, tips in sorted(by_provider.items()):
        valid = [t for t in tips if t.predicted_at < min(t.kickoff, kickoff)]
        if not valid:
            continue
        pick = max(valid, key=lambda t: (t.predicted_at, t.origin == LIVE))
        runs = sorted(
            {r for t in tips for r in t.live_runs if parse_run_ts(r) < kickoff}
        )
        rows.append(
            {
                "match_key": match_key(league, kickoff, home, away),
                "league": league,
                "season": season,
                "kickoff_utc": format_kickoff(kickoff),
                "home_team": home,
                "away_team": away,
                "provider": provider,
                "tip_home": str(pick.goals[0]),
                "tip_away": str(pick.goals[1]),
                "predicted_at": blob_archive.format_run_ts(pick.predicted_at),
                "origin": pick.origin,
                "tip_kickoff_utc": format_kickoff(pick.kickoff),
                "live_runs": RUNS_SEPARATOR.join(runs),
                "odds_home": format_odds(odds.odds[0]) if odds else "",
                "odds_draw": format_odds(odds.odds[1]) if odds else "",
                "odds_away": format_odds(odds.odds[2]) if odds else "",
                "odds_at": odds.at if odds else "",
                "odds_source": odds.source if odds else "",
            }
        )
    return rows


# %% --------------------------------------------
# * Scan plan


def newest_reconstructed(index: dict[str, Any], season: str) -> str | None:
    """Return the current reconstructed odds CSV if it can cover ``season``.

    Only a CSV made after the season started can hold its fixtures.
    """
    files = sorted(index.get("reconstructed_odds", []))
    if not files:
        return None
    stamp = files[-1].rsplit("/", 1)[-1][:19]
    try:
        made = parse_run_ts(stamp)
    except ValueError:
        return files[-1]
    return files[-1] if made >= season_start(season) - RUN_LOOKBACK else None


@dataclass
class TablePlan:
    """Which files of one season the prediction table update reads."""

    season: str
    mode: str  # full | incremental
    reason: str
    # Every prediction / odds file of the season with its run_ts
    runs: dict[str, str]
    odds: dict[str, str]
    # Files to read this run
    read: list[str]
    read_odds: list[str]
    # Re-read every odds file (state changed or table new)
    all_odds: bool = False
    reconstructed: str | None = None
    full_scan_at: str = ""
    # Files a previous run could not read (read again this run)
    retry: list[str] = field(default_factory=list)

    @property
    def through(self) -> str:
        """Newest run_ts of all files of the season (the next watermark)."""
        return max([*self.runs.values(), *self.odds.values()], default="")


def plan_table_scan(  # noqa: C901
    index: dict[str, Any],
    season: str,
    now: datetime,
    *,
    force_full: str | None = None,
) -> TablePlan:
    """Decide which prediction and odds files the table update reads.

    The season's state in ``index.json`` (``stats_scan``) holds ``through``
    (newest run_ts read), ``files``/``digest`` and ``odds_files``/``odds_digest``
    (count and SHA-256 of the prediction and odds files up to ``through``),
    ``reconstructed`` (the CSV used), ``retry`` (files a previous run could not
    read) and ``full_scan_at``. An incremental run reads only files newer than
    ``through`` plus ``retry``. A full scan (``force_full``
    gives the reason, e.g. the results step scanned fully) reads every
    prediction file; it re-reads every odds file only if the odds files up
    to ``through`` changed.
    """
    runs = results_archive.prediction_runs(index, {season})
    odds = results_archive.season_runs(index, {season}, "odds")
    reconstructed = newest_reconstructed(index, season)

    def plan(mode: str, reason: str, state: dict[str, Any] | None = None) -> TablePlan:
        through = str((state or {}).get("through", ""))
        all_odds = state is None or mode == "rebuild"
        if state is not None and mode == "full":
            scanned = [p for p, ts in odds.items() if ts <= through]
            all_odds = len(scanned) != state.get("odds_files") or paths_digest(
                scanned
            ) != state.get("odds_digest")
        retry = list((state or {}).get("retry", []))
        new = [p for p, ts in runs.items() if ts > through or p in retry]
        new_odds = [p for p, ts in odds.items() if ts > through or p in retry]
        return TablePlan(
            season,
            "full" if mode == "rebuild" else mode,
            reason,
            runs,
            odds,
            sorted(runs) if mode != "incremental" else sorted(new),
            sorted(odds) if all_odds else sorted(new_odds),
            all_odds=all_odds,
            reconstructed=reconstructed,
            full_scan_at=str((state or {}).get("full_scan_at", "")),
            retry=retry,
        )

    states = index.get(blob_archive.STATS_SCAN_KEY, {})
    state: Any = states.get(season) if isinstance(states, dict) else None
    if state is None:
        return plan("rebuild", "no scan state")
    try:
        through = str(state["through"])
        parse_run_ts(through)
        last_full = datetime.strptime(
            str(state["full_scan_at"]), blob_archive.PAYLOAD_TS_FORMAT
        ).replace(tzinfo=UTC)
        files, digest = int(state["files"]), str(state["digest"])
        int(state["odds_files"])
        str(state["odds_digest"])
        retry = state.get("retry", [])
        if not isinstance(retry, list) or not all(isinstance(p, str) for p in retry):
            msg = f"bad retry list {retry!r}"
            raise TypeError(msg)
    except (KeyError, TypeError, ValueError) as exc:
        return plan("rebuild", f"invalid scan state ({exc!r})")
    if state.get("reconstructed") != reconstructed:
        return plan("rebuild", "reconstructed odds CSV changed")
    if force_full:
        return plan("full", force_full, state)
    if now - last_full >= FULL_SCAN_INTERVAL:
        return plan("full", f"last full scan {state['full_scan_at']}", state)
    scanned = [path for path, run_ts in runs.items() if run_ts <= through]
    if len(scanned) != files or paths_digest(scanned) != digest:
        return plan("full", f"files up to {through} changed since the last scan", state)
    scanned_odds = [path for path, run_ts in odds.items() if run_ts <= through]
    if len(scanned_odds) != state["odds_files"] or (
        paths_digest(scanned_odds) != state["odds_digest"]
    ):
        return plan("full", f"odds files up to {through} changed", state)
    return plan("incremental", f"after {through}", state)


def table_state(
    plan: TablePlan, now: datetime, retry: Iterable[str] = ()
) -> dict[str, Any]:
    """Return the scan state to store after ``plan`` was carried out.

    ``retry`` lists the files this run could not read; the next run reads
    them again instead of scanning the whole season.
    """
    through = plan.through
    files = [p for p, ts in plan.runs.items() if ts <= through]
    odds = [p for p, ts in plan.odds.items() if ts <= through]
    full_scan_at = plan.full_scan_at
    if plan.mode == "full" or not full_scan_at:
        full_scan_at = blob_archive.format_payload_ts(now)
    return {
        "through": through,
        "files": len(files),
        "digest": paths_digest(files),
        "odds_files": len(odds),
        "odds_digest": paths_digest(odds),
        "reconstructed": plan.reconstructed,
        "retry": sorted(set(retry)),
        "full_scan_at": full_scan_at,
        "updated_at": blob_archive.format_payload_ts(now),
    }


# %% --------------------------------------------
# * Update


@dataclass
class TableReport:
    """What happened to one season's prediction table."""

    season: str
    pathname: str
    mode: str = ""
    reason: str = ""
    files_read: int = 0
    odds_read: int = 0
    reconstructed_read: bool = False
    existing: int = 0
    rows: int = 0
    # empty | unchanged | dry-run | written | stale | write-failed |
    # shrink-refused | aborted
    status: str = "empty"
    unread: list[str] = field(default_factory=list)
    # Rows to compute statistics from (verified Blob content or dry-run result)
    contents: list[dict[str, str]] | None = None
    index_entry: dict[str, Any] | None = None
    # State to store (also after unread files, which it lists under retry)
    scan_state: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        """Whether every file was read and the table is in Blob as computed."""
        return not self.unread and self.status in {
            "empty",
            "unchanged",
            "written",
            "dry-run",
        }


def update_prediction_table(  # noqa: C901, PLR0912, PLR0913, PLR0915
    season: str,
    *,
    index: dict[str, Any],
    now: datetime,
    fetch_jsonl: Callable[[str], list[dict[str, Any]]],
    fetch_bytes: Callable[[str], bytes],
    force_full: str | None = None,
    rebuild: bool = False,
    force: bool = False,
    dry_run: bool = False,
    attempts: int = blob_archive.INDEX_VERIFY_ATTEMPTS,
    wait: float = blob_archive.INDEX_VERIFY_WAIT_SECONDS,
) -> TableReport:
    """Bring one season's prediction table up to date (see the module docstring).

    Parameters
    ----------
    season : str
        Season like ``2026/27``.
    index : dict[str, Any]
        A current ``index.json`` (read fresh by the caller).
    now : datetime
        Current time (for the scan state).
    fetch_jsonl, fetch_bytes : Callable
        Read a write-once archive file (prediction/odds file, reconstructed CSV).
    force_full : str | None, optional
        Scan every prediction file, with this reason.
    rebuild : bool, optional
        Ignore the existing table and rebuild it from every file.
    force : bool, optional
        Allow a rebuild to shrink the table.
    dry_run : bool, optional
        Compute everything, write nothing.
    attempts, wait : optional
        Stale-read and read-back retries and the pause between them.

    Returns
    -------
    TableReport
        Counts, status, the resulting rows and the index entry / scan state
        to store (None unless the run was complete).

    """
    pathname = table_pathname(season)
    report = TableReport(season, pathname)
    if rebuild:
        index = {**index, blob_archive.STATS_SCAN_KEY: {}}
    plan = plan_table_scan(index, season, now, force_full=force_full)
    report.mode, report.reason = plan.mode, plan.reason
    if not plan.runs:
        # Summer break before the season's first run: nothing to tabulate
        logger.info("Prediction table %s: no prediction files yet", season)
        report.contents = []
        return report

    expected = int(
        index.get(blob_archive.STATS_KEY, {}).get(pathname, {}).get("rows", 0)
    )
    if rebuild:
        existing: list[dict[str, str]] | None = []
    else:
        existing = results_archive.read_rows_guarded(
            pathname, expected, attempts=attempts, wait=wait, parse=rows_from_csv
        )
    if existing is None:
        report.status = "stale"
        return report
    report.existing = len(existing)
    try:
        base_tips, observations, base_quotes = from_rows(existing)
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("%s has unreadable rows (%r), not touching it", pathname, exc)
        report.status = "aborted"
        return report
    if not existing and plan.mode == "incremental" and plan.through:
        # The table is gone although the state says it was built: rebuild it
        # from every file rather than from the new ones only
        plan.mode, plan.reason = "full", "table missing"
        plan.read = sorted(plan.runs)
        report.mode, report.reason = plan.mode, plan.reason
    if not existing and not plan.all_odds:
        # A missing table cannot carry odds over: read them all
        plan.read_odds = sorted(plan.odds)
        plan.all_odds = True

    tips: list[Tip] = [] if rebuild else list(base_tips)
    for path in plan.read:
        try:
            lines = fetch_jsonl(path)
        except Exception as e:
            logger.warning(
                "Could not read %s (HTTP %s): %s",
                path,
                getattr(e, "status_code", None),
                e,
            )
            report.unread.append(path)
            continue
        tips.extend(tips_from_lines(lines, path, plan.runs[path], season))
    report.files_read = len(plan.read)

    quotes: list[Quote] = [] if rebuild or plan.all_odds else list(base_quotes)
    for path in plan.read_odds:
        try:
            lines = fetch_jsonl(path)
        except Exception as e:
            logger.warning(
                "Could not read %s (HTTP %s): %s",
                path,
                getattr(e, "status_code", None),
                e,
            )
            report.unread.append(path)
            continue
        quotes.extend(quotes_from_lines(lines, plan.odds[path], season))
    report.odds_read = len(plan.read_odds)

    csv_quotes = None
    if plan.reconstructed and (
        rebuild or plan.all_odds or not existing or plan.reconstructed in plan.retry
    ):
        try:
            csv_quotes = quotes_from_reconstructed(
                fetch_bytes(plan.reconstructed), season
            )
            report.reconstructed_read = True
        except Exception as e:
            logger.warning(
                "Could not read %s (HTTP %s): %s",
                plan.reconstructed,
                getattr(e, "status_code", None),
                e,
            )
            report.unread.append(plan.reconstructed)
    if rebuild and report.unread:
        report.status = "aborted"
        logger.warning(
            "%d files unreadable, not rebuilding %s", len(report.unread), pathname
        )
        return report

    rows = build_rows(tips, [] if rebuild else observations, quotes, csv_quotes)
    report.rows = len(rows)
    logger.info(
        "Prediction table %s: %s (%s), read %d prediction and %d odds files, "
        "%d -> %d rows",
        season,
        plan.mode,
        plan.reason,
        report.files_read,
        report.odds_read,
        report.existing,
        report.rows,
    )
    stamp = blob_archive.format_payload_ts(now)
    if rows == sorted(existing, key=row_sort_key):
        report.status = "unchanged"
        report.contents = existing
        if rows and expected != len(rows):
            # Self-heal an index entry that a lagging update dropped
            report.index_entry = {"rows": len(rows), "updated_at": stamp}
    elif len(rows) < expected and not force:
        logger.warning(
            "%s would shrink from %d to %d rows (index): not written",
            pathname,
            expected,
            len(rows),
        )
        report.status = "shrink-refused"
        report.contents = existing
        return report
    elif dry_run:
        report.status = "dry-run"
        report.contents = rows
    elif results_archive.write_rows_verified(
        pathname,
        rows,
        attempts=attempts,
        wait=wait,
        serialize=rows_to_csv,
        parse=rows_from_csv,
        row_key=row_identity,
    ):
        report.status = "written"
        report.contents = rows
        report.index_entry = {"rows": len(rows), "updated_at": stamp}
    else:
        report.status = "write-failed"
        report.contents = existing
        return report
    if not dry_run:
        # Unread files are retried next run (see plan_table_scan); the state
        # still advances so a lasting failure never forces full scans
        report.scan_state = table_state(plan, now, report.unread)
    return report
