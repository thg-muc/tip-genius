"""Archive of final match results in Vercel Blob (one CSV per league and season).

Results come from openligadb.de (Bundesliga) and ESPN's public scoreboard
(all other leagues) and are matched to the predicted fixtures once, at write
time, with fuzzy team-name matching. Each row carries a ``match_key`` built
from the prediction record alone::

    <league>|<kickoff UTC, YYYY-MM-DDTHH:MMZ>|<home_team>|<away_team>

with the league ``name`` and team names exactly as in the prediction files
(Odds API spelling), so any prediction record, old or new, joins to its result
without fuzzy matching. ``home_goals``/``away_goals`` are the score after 90
minutes (Kicktipp scores regular time), also for extra-time or penalty games.

Files: ``results/<season>_<League-Key>.csv`` (season as ``2026-27``), rewritten
in place when rows are added and listed in ``index.json`` under ``results``
(``{pathname: {"rows", "updated_at"}}``). Unmatched or low-confidence fixtures
are only logged, never written.

Guards:

- Gap detection: every run targets all fixtures of the season that have a
  prediction, kicked off more than ``RESULT_DELAY`` ago and have no row yet, so
  rows lost to a failed write or a stale read heal on the next run.
- Never shrink: a fresh read with fewer rows than the index records is stale;
  it is retried and, if still short, the file is skipped (nothing written).
- Read-back: each write is read back fresh and must show every row, mirroring
  ``blob_archive.update_backfill_index``.
"""
# * Author(s): Thomas Glanzer
# * Creation : Nov 2026
# * License: MIT license

from __future__ import annotations

import csv
import io
import json
import logging
import re
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import requests
from lib import blob_archive

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

logger = logging.getLogger(__name__)

# %% --------------------------------------------
# * Constants

BERLIN = ZoneInfo("Europe/Berlin")
PREDICTION_TIME_FORMAT = "%d.%m.%Y %H:%M"
KEY_TIME_FORMAT = "%Y-%m-%dT%H:%MZ"
RESULTS_PREFIX = "results/"
RESULT_COLUMNS = [
    "match_key",
    "league",
    "season",
    "kickoff_utc",
    "home_team",
    "away_team",
    "home_goals",
    "away_goals",
    "status",
    "source",
    "source_id",
    "source_home_team",
    "source_away_team",
    "match_score",
    "fetched_at",
]
# A match counts as finished this long after kickoff
RESULT_DELAY = timedelta(hours=3)
# The workflow step retries stale reads briefly; a skipped file heals next run
CRON_ATTEMPTS = 2
CRON_WAIT_SECONDS = 15.0
# Unmatched fixtures older than this are logged at DEBUG only
UNMATCHED_LOG_WINDOW = timedelta(days=14)
# Runs this long before a season start can already list its first fixtures
RUN_LOOKBACK = timedelta(days=30)

ESPN_LEAGUES = {
    "Premier League - England": "eng.1",
    "La Liga - Spain": "esp.1",
    "Bundesliga - Germany": "ger.1",
    "UEFA Champions League": "uefa.champions",
    "UEFA Europa League": "uefa.europa",
    "Bundesliga - Austria": "aut.1",
}
BUNDESLIGA = "Bundesliga - Germany"
# ESPN marks these as completed too, but they have no (valid) score
# Competitions where the same home/away pairing can occur twice in a season
REPEATED_PAIRINGS = ("UEFA Champions League", "UEFA Europa League")
ESPN_VOID_STATUS = ("CANCEL", "POSTPON", "ABANDON", "FORFEIT", "SUSPEND")

NAME_STOPWORDS = {
    "fc", "cf", "afc", "sc", "ac", "as", "ssc", "sv", "vfl", "vfb", "tsg", "fsv",
    "cd", "ud", "rcd", "sd", "rc", "club", "de", "the", "and", "1", "05", "04",
    "1899", "1846", "1910", "sk", "fk", "bk", "if", "ca", "us", "calcio",
}  # fmt: skip
NAME_ALIASES = {
    "munchen": "munich",
    "koln": "cologne",
    "nurnberg": "nuremberg",
    "hertha bsc": "hertha berlin",
    "st pauli": "st pauli",
    "atletico": "atletico",
    "inter": "internazionale",
    "psg": "paris saint germain",
    "man": "manchester",
    "utd": "united",
    "wolves": "wolverhampton wanderers",
    "spurs": "tottenham hotspur",
}
MATCH_WINDOW = timedelta(hours=36)
MIN_TEAM_SIMILARITY = 0.45
MIN_PAIR_SIMILARITY = 1.25


# %% --------------------------------------------
# * Data structures


@dataclass(frozen=True)
class Fixture:
    """One predicted fixture, as the prediction records name it."""

    league: str
    kickoff: datetime
    home: str
    away: str

    @property
    def key(self) -> str:
        """Return the ``match_key`` of this fixture."""
        return match_key(self.league, self.kickoff, self.home, self.away)

    @property
    def season(self) -> str:
        """Return the season label, e.g. ``2026/27``."""
        return season_of(self.kickoff)


@dataclass(frozen=True)
class Result:
    """Final (90 minute) score of a finished match, as the source names it."""

    league: str
    kickoff: datetime
    home: str
    away: str
    home_goals: int
    away_goals: int
    matchday: str | None = None
    status: str = ""
    source: str = ""
    source_id: str = ""


# %% --------------------------------------------
# * Keys, seasons, pathnames


def kicktipp_points(pred: tuple[int, int], actual: tuple[int, int]) -> int:
    """Return Kicktipp four-points score for a predicted vs. actual result."""
    if pred == actual:
        return 4
    pred_diff, actual_diff = pred[0] - pred[1], actual[0] - actual[1]
    if (pred_diff > 0) != (actual_diff > 0) or (pred_diff < 0) != (actual_diff < 0):
        return 0
    if actual_diff == 0:
        return 2
    return 3 if pred_diff == actual_diff else 2


def season_of(moment: datetime) -> str:
    """Return season label like '2026/27' (seasons start on 1 July)."""
    start = moment.year if moment.month >= 7 else moment.year - 1  # noqa: PLR2004
    return f"{start}/{(start + 1) % 100:02d}"


def season_start(season: str) -> datetime:
    """Return 1 July of the season's first year (UTC)."""
    return datetime(int(season[:4]), 7, 1, tzinfo=UTC)


def format_kickoff(kickoff: datetime) -> str:
    """Format a kickoff as UTC ISO to the minute, e.g. ``2026-11-07T14:30Z``."""
    return kickoff.astimezone(UTC).strftime(KEY_TIME_FORMAT)


def match_key(league: str, kickoff: datetime, home: str, away: str) -> str:
    """Return the key joining a prediction record to its result row."""
    return f"{league}|{format_kickoff(kickoff)}|{home}|{away}"


def parse_commence_time(text: str) -> datetime:
    """Convert a prediction's ``commence_time_str`` (Berlin local) to UTC."""
    local = datetime.strptime(text, PREDICTION_TIME_FORMAT).replace(tzinfo=BERLIN)
    return local.astimezone(UTC)


def fixture_from_record(league: str, record: dict[str, Any]) -> Fixture | None:
    """Build the fixture of one prediction record; None if fields are missing."""
    try:
        kickoff = parse_commence_time(record["commence_time_str"])
        home, away = record["home_team"], record["away_team"]
    except (KeyError, TypeError, ValueError):
        return None
    if not home or not away:
        return None
    return Fixture(league, kickoff, home, away)


def prediction_match_key(league: str, record: dict[str, Any]) -> str | None:
    """Return the ``match_key`` of a prediction record under league ``name``."""
    fixture = fixture_from_record(league, record)
    return fixture.key if fixture else None


def results_pathname(season: str, league: str) -> str:
    """Return the CSV pathname of one league and season (``2026/27`` style)."""
    label = season.replace("/", "-")
    return f"{RESULTS_PREFIX}{label}_{blob_archive.league_key(league)}.csv"


# %% --------------------------------------------
# * Team name matching


def normalize_team(name: str) -> str:
    """Normalize a team name for fuzzy comparison across data sources."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9 ]", " ", text.lower().replace("&", " and "))
    words = [NAME_ALIASES.get(w, w) for w in text.split()]
    words = [w for w in words if w not in NAME_STOPWORDS]
    return " ".join(words)


def team_similarity(a: str, b: str) -> float:
    """Return similarity in [0, 1] between two team names."""
    na, nb = normalize_team(a), normalize_team(b)
    if not na or not nb:
        return 0.0
    if na == nb or na in nb or nb in na:
        return 1.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    wa, wb = set(na.split()), set(nb.split())
    overlap = len(wa & wb) / min(len(wa), len(wb))
    return max(ratio, overlap)


def match_results(
    fixtures: Iterable[Fixture], results: list[Result]
) -> dict[Fixture, tuple[Result, float]]:
    """Map each fixture to its best-matching result and the match score.

    The score is the sum of both team similarities minus a small kickoff-gap
    penalty (max. 2.0). Fixtures below ``MIN_PAIR_SIMILARITY`` stay unmatched.
    """
    by_league: dict[str, list[Result]] = defaultdict(list)
    for result in results:
        by_league[result.league].append(result)
    matched: dict[Fixture, tuple[Result, float]] = {}
    for fixture in set(fixtures):
        best, best_score = None, 0.0
        for result in by_league.get(fixture.league, []):
            gap = abs(result.kickoff - fixture.kickoff)
            if gap > MATCH_WINDOW:
                continue
            sim_home = team_similarity(fixture.home, result.home)
            sim_away = team_similarity(fixture.away, result.away)
            if min(sim_home, sim_away) < MIN_TEAM_SIMILARITY:
                continue
            score = sim_home + sim_away - gap / MATCH_WINDOW * 0.2
            if score > best_score:
                best, best_score = result, score
        if best and best_score >= MIN_PAIR_SIMILARITY:
            matched[fixture] = (best, best_score)
    return matched


# %% --------------------------------------------
# * Result sources


class HttpCache:
    """Tiny JSON cache for GET requests: on disk, or in memory without a dir."""

    def __init__(self, directory: Path | None = None) -> None:
        """Create the cache (directory None = in memory only)."""
        self.directory = directory
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)
        self.memory: dict[str, object] = {}
        self.session = requests.Session()

    def get_json(self, url: str, *, cacheable: bool = True) -> object:
        """Fetch URL as JSON, reading from and writing to the cache."""
        if url in self.memory:
            return self.memory[url]
        path = None
        if self.directory is not None:
            name = re.sub(r"[^A-Za-z0-9]+", "_", url)[-180:] + ".json"
            path = self.directory / name
            if cacheable and path.exists():
                return json.loads(path.read_text())
        response = self.session.get(url, timeout=30)
        response.raise_for_status()
        data = response.json()
        self.memory[url] = data
        if cacheable and path is not None:
            path.write_text(json.dumps(data))
        return data


def openligadb_results(
    cache: HttpCache, seasons: set[int], now: datetime | None = None
) -> list[Result]:
    """Fetch finished Bundesliga matches from openligadb.de.

    ``resultTypeID`` 2 is the final score; the Bundesliga has no extra time.
    """
    results = []
    current = season_of(now or datetime.now(tz=UTC))
    for season in sorted(seasons):
        url = f"https://api.openligadb.de/getmatchdata/bl1/{season}"
        is_current = current.startswith(str(season))
        for match in cache.get_json(url, cacheable=not is_current):  # type: ignore[union-attr]
            final = [r for r in match["matchResults"] if r["resultTypeID"] == 2]  # noqa: PLR2004
            if not match["matchIsFinished"] or not final:
                continue
            results.append(
                Result(
                    BUNDESLIGA,
                    datetime.fromisoformat(match["matchDateTimeUTC"]).astimezone(UTC),
                    match["team1"]["teamName"],
                    match["team2"]["teamName"],
                    final[0]["pointsTeam1"],
                    final[0]["pointsTeam2"],
                    f"{BUNDESLIGA} {season} MD {match['group']['groupOrderID']:02d}",
                    status="finished",
                    source="openligadb",
                    source_id=str(match.get("matchID", "")),
                )
            )
    return results


def _goal_count(raw: object) -> int | None:
    """Parse a non-negative goal count such as ``2``, ``2.0`` or ``"2"``."""
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return int(value) if value >= 0 and value.is_integer() else None


def espn_regular_time(competitor: dict) -> int | None:
    """Return goals after 90 minutes from ESPN period line scores.

    Scoreboard line scores carry ``value``, summary ones only ``displayValue``.
    Periods 1-4 are regular and extra time (5 is the shootout). If the
    competitor has a ``score``, periods 1-4 must add up to it; ESPN sometimes
    publishes inconsistent line scores (e.g. negative periods). None if the
    line scores are missing or fail that check.
    """
    lines = competitor.get("linescores") or []
    if len(lines) < 2:  # noqa: PLR2004
        return None
    periods = [
        _goal_count(line.get("value", line.get("displayValue"))) for line in lines[:4]
    ]
    if any(goals is None for goals in periods):
        return None
    counted = [goals or 0 for goals in periods]
    final = _goal_count(competitor.get("score"))
    if final is not None and sum(counted) != final:
        return None
    return counted[0] + counted[1]


def espn_key_event_regular_time(data: dict) -> tuple[int, int] | None:
    """Count goals in periods 1-2 per side from an ESPN summary's key events.

    Only trusted if all scoring plays of periods 1-4 add up to each side's
    final score (an own goal credited to the wrong side fails the check).
    """
    try:
        competitors = data["header"]["competitions"][0]["competitors"]
        sides = {c["team"]["id"]: c["homeAway"] for c in competitors}
        finals = {c["homeAway"]: _goal_count(c.get("score")) for c in competitors}
    except (KeyError, IndexError, TypeError):
        return None
    regular = {"home": 0, "away": 0}
    total = {"home": 0, "away": 0}
    for event in data.get("keyEvents") or []:
        if not event.get("scoringPlay"):
            continue
        period = (event.get("period") or {}).get("number")
        side = sides.get((event.get("team") or {}).get("id"))
        if side not in total or not isinstance(period, int) or period > 4:  # noqa: PLR2004
            continue
        total[side] += 1
        regular[side] += period <= 2  # noqa: PLR2004
    if any(finals.get(side) != total[side] for side in total):
        return None
    return regular["home"], regular["away"]


def espn_summary_regular_time(
    cache: HttpCache, code: str, event_id: str
) -> tuple[int, int] | None:
    """Return the 90-minute score from ESPN's event summary.

    The scoreboard often lacks line scores for extra-time games. The summary
    has scoring key events with their period (preferred, checked against the
    final score) and per-period line scores (checked the same way).
    """
    url = (
        "https://site.api.espn.com/apis/site/v2/sports/soccer/"
        f"{code}/summary?event={event_id}"
    )
    try:
        data = cache.get_json(url)
    except requests.RequestException as exc:
        logger.warning("ESPN summary failed for event %s: %s", event_id, exc)
        return None
    if not isinstance(data, dict):
        return None
    if (from_events := espn_key_event_regular_time(data)) is not None:
        return from_events
    try:
        competitors = data["header"]["competitions"][0]["competitors"]
    except (KeyError, IndexError, TypeError):
        return None
    teams = {c.get("homeAway"): c for c in competitors}
    if "home" not in teams or "away" not in teams:
        return None
    home, away = espn_regular_time(teams["home"]), espn_regular_time(teams["away"])
    return None if home is None or away is None else (home, away)


def espn_results(
    cache: HttpCache, league: str, days: set[date], now: datetime | None = None
) -> list[Result]:
    """Fetch finished matches for one league on the given (past) days from ESPN.

    Games decided after extra time or on penalties get their 90-minute score
    from the period line scores (scoreboard, else the event summary); without
    them the game is skipped.
    """
    code = ESPN_LEAGUES[league]
    today = (now or datetime.now(tz=UTC)).date()
    results = []
    for day in sorted(days):
        if day >= today:
            continue
        url = (
            "https://site.api.espn.com/apis/site/v2/sports/soccer/"
            f"{code}/scoreboard?dates={day:%Y%m%d}"
        )
        try:
            data = cache.get_json(url, cacheable=day < today - timedelta(days=2))
        except requests.RequestException as exc:
            logger.warning("ESPN request failed for %s %s: %s", league, day, exc)
            continue
        for event in data.get("events", []):  # type: ignore[union-attr]
            try:
                result = _espn_event_result(cache, league, code, event)
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                logger.warning(
                    "Skipping malformed ESPN event %s (%s): %r",
                    event.get("id") if isinstance(event, dict) else None,
                    league,
                    exc,
                )
                continue
            if result is not None:
                results.append(result)
    return results


def _espn_event_result(
    cache: HttpCache, league: str, code: str, event: dict
) -> Result | None:
    """Turn one ESPN scoreboard event into a result; None if not final."""
    status = event["status"]["type"]
    name = status.get("name", "")
    if not status.get("completed") or any(v in name for v in ESPN_VOID_STATUS):
        return None
    comp = event["competitions"][0]
    teams = {c["homeAway"]: c for c in comp["competitors"]}
    if "home" not in teams or "away" not in teams:
        return None
    if any(tag in name for tag in ("AET", "PEN")):
        home_goals = espn_regular_time(teams["home"])
        away_goals = espn_regular_time(teams["away"])
        if home_goals is None or away_goals is None:
            regular = espn_summary_regular_time(cache, code, str(event.get("id", "")))
            if regular is None:
                logger.warning(
                    "No 90-minute score for %s event %s (%s), skipped",
                    league,
                    event.get("id"),
                    name,
                )
                return None
            home_goals, away_goals = regular
    else:
        home_goals = int(teams["home"]["score"])
        away_goals = int(teams["away"]["score"])
    return Result(
        league,
        datetime.fromisoformat(event["date"]),
        teams["home"]["team"]["displayName"],
        teams["away"]["team"]["displayName"],
        home_goals,
        away_goals,
        status=name,
        source="espn",
        source_id=str(event.get("id", "")),
    )


def fetch_results(
    cache: HttpCache, fixtures: Iterable[Fixture], now: datetime | None = None
) -> list[Result]:
    """Fetch results for every league and day (Bundesliga: season) in fixtures."""
    days: dict[str, set[date]] = defaultdict(set)
    bl_seasons: set[int] = set()
    unknown: set[str] = set()
    for fixture in fixtures:
        if fixture.league == BUNDESLIGA:
            bl_seasons.add(int(fixture.season[:4]))
        elif fixture.league in ESPN_LEAGUES:
            days[fixture.league].add(fixture.kickoff.date())
            days[fixture.league].add(fixture.kickoff.astimezone(BERLIN).date())
        else:
            unknown.add(fixture.league)
    results: list[Result] = []
    if bl_seasons:
        try:
            results = openligadb_results(cache, bl_seasons, now)
        except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
            logger.warning("openligadb request failed for %s: %s", bl_seasons, exc)
    for league, league_days in days.items():
        logger.info("Fetching %d days of %s results", len(league_days), league)
        results.extend(espn_results(cache, league, league_days, now))
    if unknown:
        logger.warning("No result source for leagues: %s", sorted(unknown))
    logger.info("Loaded %d finished results", len(results))
    return results


# %% --------------------------------------------
# * Predicted fixtures


def prediction_pathnames(index: dict[str, Any], seasons: set[str]) -> list[str]:
    """Return live and backfill prediction files of runs that can hold ``seasons``.

    A run belongs if its timestamp falls in a season (or up to ``RUN_LOOKBACK``
    before its start); each fixture is later assigned by its own kickoff.
    """
    windows = [
        (season_start(s) - RUN_LOOKBACK, season_start(s).replace(year=int(s[:4]) + 1))
        for s in seasons
    ]

    def wanted(run_ts: str) -> bool:
        try:
            moment = datetime.strptime(run_ts, blob_archive.RUN_TS_FORMAT).replace(
                tzinfo=UTC
            )
        except ValueError:
            return False
        return any(start <= moment < end for start, end in windows)

    paths: set[str] = set()
    for run in index.get("runs", []):
        if wanted(run["run_ts"]):
            paths.update(run.get("predictions", {}).values())
    for run_ts, files in index.get(blob_archive.BACKFILL_KEY, {}).items():
        if wanted(run_ts):
            paths.update(files.values())
    return sorted(paths)


def collect_fixtures(
    paths: list[str],
    fetch_jsonl: Callable[[str], list[dict[str, Any]]],
    seasons: set[str],
) -> tuple[set[Fixture], list[str]]:
    """Read prediction files and return their fixtures in ``seasons``.

    Returns
    -------
    tuple[set[Fixture], list[str]]
        The fixtures and the pathnames that could not be read (logged).

    """
    fixtures: set[Fixture] = set()
    failed: list[str] = []
    for path in paths:
        try:
            lines = fetch_jsonl(path)
        except Exception as e:
            logger.warning(
                "Could not read %s (HTTP %s): %s",
                path,
                getattr(e, "status_code", None),
                e,
            )
            failed.append(path)
            continue
        for line in lines:
            for record in line.get("matches", []):
                fixture = fixture_from_record(line.get("name", ""), record)
                if fixture and fixture.season in seasons:
                    fixtures.add(fixture)
    return fixtures, failed


# %% --------------------------------------------
# * CSV rows


def result_row(
    fixture: Fixture, result: Result, score: float, fetched_at: datetime
) -> dict[str, str]:
    """Build one CSV row (all values as strings)."""
    return {
        "match_key": fixture.key,
        "league": fixture.league,
        "season": fixture.season,
        "kickoff_utc": format_kickoff(fixture.kickoff),
        "home_team": fixture.home,
        "away_team": fixture.away,
        "home_goals": str(result.home_goals),
        "away_goals": str(result.away_goals),
        "status": result.status,
        "source": result.source,
        "source_id": result.source_id,
        "source_home_team": result.home,
        "source_away_team": result.away,
        "match_score": f"{score:.3f}",
        "fetched_at": blob_archive.format_payload_ts(fetched_at),
    }


def rows_to_csv(rows: Iterable[dict[str, str]]) -> bytes:
    """Serialize rows sorted by kickoff and key, with a header line."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=RESULT_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in sorted(rows, key=lambda r: (r["kickoff_utc"], r["match_key"])):
        writer.writerow({column: row.get(column, "") for column in RESULT_COLUMNS})
    return buffer.getvalue().encode("utf-8")


def rows_from_csv(body: bytes) -> list[dict[str, str]]:
    """Parse a results CSV; the header must match ``RESULT_COLUMNS``.

    Raises
    ------
    ValueError
        If the header differs, so a foreign file is never extended.

    """
    reader = csv.DictReader(io.StringIO(body.decode("utf-8")))
    if reader.fieldnames and list(reader.fieldnames) != RESULT_COLUMNS:
        msg = f"Unexpected results CSV header: {reader.fieldnames}"
        raise ValueError(msg)
    return list(reader)


def reschedule_rows(
    fixtures: Iterable[Fixture], rows: Iterable[dict[str, str]]
) -> dict[Fixture, dict[str, str]]:
    """Reuse rows for fixtures predicted with a kickoff that later moved.

    In a domestic league each home/away pairing occurs once per season, so an
    unmatched fixture whose exact team names (Odds API spelling) already have
    a row in the same league and season gets a copy of that row under its own
    ``match_key``. This keeps old prediction records joinable after a
    reschedule. Repeated pairings (UEFA) and ambiguous pairs are skipped.
    """
    by_pair: dict[tuple[str, str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_pair[
            row["league"], row["season"], row["home_team"], row["away_team"]
        ].append(row)
    aliases: dict[Fixture, dict[str, str]] = {}
    for fixture in fixtures:
        if fixture.league in REPEATED_PAIRINGS:
            continue
        candidates = by_pair.get(
            (fixture.league, fixture.season, fixture.home, fixture.away), []
        )
        if len({(r["source"], r["source_id"]) for r in candidates}) != 1:
            continue
        aliases[fixture] = {
            **candidates[0],
            "match_key": fixture.key,
            "kickoff_utc": format_kickoff(fixture.kickoff),
        }
    return aliases


# %% --------------------------------------------
# * Blob I/O with guards


def read_rows_guarded(
    pathname: str,
    expected_rows: int,
    *,
    attempts: int = blob_archive.INDEX_VERIFY_ATTEMPTS,
    wait: float = blob_archive.INDEX_VERIFY_WAIT_SECONDS,
) -> list[dict[str, str]] | None:
    """Read a results CSV fresh; None if it stays shorter than the index says.

    A missing file counts as zero rows. A read with fewer rows than
    ``expected_rows`` (from ``index.json``) is stale: it is retried up to
    ``attempts`` times, then given up, so nothing is written from it.
    Read errors are retried the same way. Never raises.
    """
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            time.sleep(wait)
        try:
            body = blob_archive.read_fresh(pathname)
            rows = [] if body is None else rows_from_csv(body)
        except ValueError:
            logger.warning("%s has an unexpected format, not touching it", pathname)
            return None
        except Exception as e:
            logger.warning(
                "Could not read %s (HTTP %s, attempt %d/%d): %s",
                pathname,
                getattr(e, "status_code", None),
                attempt,
                attempts,
                e,
            )
            continue
        if len(rows) >= expected_rows:
            return rows
        logger.warning(
            "%s looks stale (attempt %d/%d): %d rows, index says %d",
            pathname,
            attempt,
            attempts,
            len(rows),
            expected_rows,
        )
    logger.warning("Skipping %s: no complete read, nothing written", pathname)
    return None


def write_rows_verified(
    pathname: str,
    rows: list[dict[str, str]],
    *,
    attempts: int = blob_archive.INDEX_VERIFY_ATTEMPTS,
    wait: float = blob_archive.INDEX_VERIFY_WAIT_SECONDS,
) -> bool:
    """Overwrite a results CSV and read it back until every row shows.

    Each attempt puts the full file (idempotent) and reads it back fresh;
    success needs every ``match_key`` of ``rows``. Never raises.
    """
    body = rows_to_csv(rows)
    wanted = {row["match_key"] for row in rows}
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            time.sleep(wait)
        if not blob_archive.put_with_retry(
            pathname,
            body,
            content_type=blob_archive.CSV_CONTENT_TYPE,
            allow_overwrite=True,
            cache_control_max_age=blob_archive.INDEX_CACHE_MAX_AGE,
        ):
            continue
        try:
            check = blob_archive.read_fresh(pathname)
            seen = {r["match_key"] for r in rows_from_csv(check)} if check else set()
        except Exception as e:
            logger.warning("Could not read back %s: %s", pathname, e)
            seen = set()
        if wanted <= seen:
            return True
        logger.warning(
            "%s read-back misses %d of %d rows (attempt %d/%d)",
            pathname,
            len(wanted - seen),
            len(wanted),
            attempt,
            attempts,
        )
    logger.warning("%s NOT verified after %d attempts", pathname, attempts)
    return False


# %% --------------------------------------------
# * Archive update


@dataclass
class FileReport:
    """What happened to one results file."""

    pathname: str
    league: str
    season: str
    existing: int = 0
    added: int = 0
    rows: int = 0
    # unchanged | dry-run | written | stale | write-failed | shrink-refused
    status: str = "unchanged"


@dataclass
class ArchiveReport:
    """Summary of one results archive update."""

    files: list[FileReport] = field(default_factory=list)
    unmatched: list[Fixture] = field(default_factory=list)
    unread: list[str] = field(default_factory=list)
    index_entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    index_updated: bool | None = None
    # Full content per file after this update (also in a dry-run)
    contents: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    # Set when the update stopped before looking at any file
    aborted: str | None = None

    @property
    def ok(self) -> bool:
        """Whether every file and the index update went through."""
        bad = {"stale", "write-failed", "shrink-refused"}
        return (
            self.aborted is None
            and not any(f.status in bad for f in self.files)
            and self.index_updated is not False
        )


def update_results_archive(  # noqa: C901, PLR0912, PLR0913, PLR0915
    seasons: set[str],
    *,
    now: datetime | None = None,
    cache: HttpCache | None = None,
    fetch_jsonl: Callable[[str], list[dict[str, Any]]] | None = None,
    rebuild: bool = False,
    force: bool = False,
    dry_run: bool = False,
    attempts: int = blob_archive.INDEX_VERIFY_ATTEMPTS,
    wait: float = blob_archive.INDEX_VERIFY_WAIT_SECONDS,
) -> ArchiveReport:
    """Add missing results of ``seasons`` to the archive (one CSV per league).

    Parameters
    ----------
    seasons : set[str]
        Seasons like ``{"2026/27"}``.
    now : datetime | None, optional
        Current time (fixtures must have kicked off ``RESULT_DELAY`` before).
    cache : HttpCache | None, optional
        Result source cache, by default in memory.
    fetch_jsonl : Callable | None, optional
        Reads one prediction file, by default ``BlobArchiveReader.fetch_jsonl``.
    rebuild : bool, optional
        Ignore existing rows and rebuild the files from the sources. A rebuild
        with fewer rows than the index lists is refused unless ``force``.
    force : bool, optional
        Allow a rebuild to shrink a file.
    dry_run : bool, optional
        Compute everything, write nothing.
    attempts, wait : optional
        Stale-read retries and the pause between them (seconds).

    Returns
    -------
    ArchiveReport
        Per-file counts and status, unmatched fixtures, index outcome.

    Raises
    ------
    vercel_blob.BlobError
        If ``index.json`` cannot be read (nothing is written then).

    """
    now = now or datetime.now(tz=UTC)
    cache = cache or HttpCache()
    fetch_jsonl = fetch_jsonl or blob_archive.BlobArchiveReader().fetch_jsonl
    report = ArchiveReport()

    index = blob_archive.read_index(fresh=True)
    indexed = index.get(blob_archive.RESULTS_KEY, {})
    paths = prediction_pathnames(index, seasons)
    fixtures, report.unread = collect_fixtures(paths, fetch_jsonl, seasons)
    if report.unread and rebuild:
        # A rebuild from an incomplete set of predictions would drop rows
        report.aborted = f"{len(report.unread)} prediction files unreadable"
        logger.warning("%s, not rebuilding", report.aborted)
        return report
    logger.info(
        "Results archive: %d prediction files, %d fixtures in %s",
        len(paths),
        len(fixtures),
        ", ".join(sorted(seasons)),
    )

    by_file: dict[str, list[Fixture]] = defaultdict(list)
    for fixture in fixtures:
        by_file[results_pathname(fixture.season, fixture.league)].append(fixture)

    existing: dict[str, list[dict[str, str]]] = {}
    gaps: list[Fixture] = []
    due = now - RESULT_DELAY
    for pathname, file_fixtures in sorted(by_file.items()):
        first = file_fixtures[0]
        file_report = FileReport(pathname, first.league, first.season)
        report.files.append(file_report)
        expected = int(indexed.get(pathname, {}).get("rows", 0))
        if rebuild:
            rows: list[dict[str, str]] | None = []
        else:
            rows = read_rows_guarded(pathname, expected, attempts=attempts, wait=wait)
        if rows is None:
            file_report.status = "stale"
            continue
        existing[pathname] = rows
        file_report.existing = file_report.rows = len(rows)
        known = {row["match_key"] for row in rows}
        gaps.extend(f for f in file_fixtures if f.kickoff <= due and f.key not in known)

    matched = match_results(gaps, fetch_results(cache, gaps, now)) if gaps else {}
    new_rows: dict[str, list[dict[str, str]]] = defaultdict(list)
    for fixture, (result, score) in matched.items():
        pathname = results_pathname(fixture.season, fixture.league)
        new_rows[pathname].append(result_row(fixture, result, score, now))
    known_rows = [
        row
        for pathname in existing
        for row in (*existing[pathname], *new_rows.get(pathname, []))
    ]
    leftover = [f for f in gaps if f not in matched]
    aliases = reschedule_rows(leftover, known_rows)
    for fixture, row in aliases.items():
        new_rows[results_pathname(fixture.season, fixture.league)].append(row)
        logger.info(
            "Rescheduled: %s reuses the result of %s", fixture.key, row["source_id"]
        )
    report.unmatched = sorted(
        (f for f in leftover if f not in aliases),
        key=lambda f: (f.league, f.kickoff, f.home),
    )
    recent = now - UNMATCHED_LOG_WINDOW
    for fixture in report.unmatched:
        # Older ones (relegation playoffs, abandoned games) recur every run
        logger.log(
            logging.INFO if fixture.kickoff >= recent else logging.DEBUG,
            "No result matched: %s %s %s - %s",
            fixture.league,
            format_kickoff(fixture.kickoff),
            fixture.home,
            fixture.away,
        )

    stamp = blob_archive.format_payload_ts(now)
    for file_report in report.files:
        pathname = file_report.pathname
        if pathname not in existing:
            continue
        added = new_rows.get(pathname, [])
        rows = existing[pathname] + added
        file_report.added = len(added)
        file_report.rows = len(rows)
        report.contents[pathname] = rows
        indexed_rows = indexed.get(pathname, {}).get("rows")
        if rebuild and file_report.rows < int(indexed_rows or 0) and not force:
            logger.warning(
                "Rebuild of %s has %d rows, index lists %d: not written (--force)",
                pathname,
                file_report.rows,
                indexed_rows,
            )
            file_report.status = "shrink-refused"
            continue
        if not added:
            if rows and indexed_rows != len(rows):
                # Self-heal an index entry that a lagging update dropped
                report.index_entries[pathname] = {
                    "rows": len(rows),
                    "updated_at": stamp,
                }
            continue
        if dry_run:
            file_report.status = "dry-run"
            continue
        if write_rows_verified(pathname, rows, attempts=attempts, wait=wait):
            file_report.status = "written"
            report.index_entries[pathname] = {"rows": len(rows), "updated_at": stamp}
        else:
            file_report.status = "write-failed"

    if report.index_entries and not dry_run:
        report.index_updated = blob_archive.update_results_index(
            report.index_entries, expected=index, attempts=attempts, wait=wait
        )
    written = [f for f in report.files if f.status == "written"]
    logger.info(
        "Results archive: %d rows added in %d files, %d fixtures unmatched%s",
        sum(f.added for f in written),
        len(written),
        len(report.unmatched),
        "" if report.ok else " (see warnings)",
    )
    return report
