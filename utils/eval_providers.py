"""Backtest LLM provider predictions against actual match results.

Scores every provider's past predictions with Kicktipp four-points scoring
(4 exact result, 3 correct goal difference, 2 correct tendency, 0 otherwise;
a draw with the wrong score is a tendency hit worth 2).

Predictions come from the timestamped snapshots in Vercel/Upstash KV
(read-only token) plus, for the period before KV history starts, the local
copies in ``data/match_predictions/``. For each match the last snapshot taken
before kickoff is used. Final scores come from openligadb.de (Bundesliga) and
ESPN's public scoreboard (all other leagues). HTTP responses are cached on
disk so reruns are fast. Nothing is written to KV or to the repository.

A reliability table shows how many offered matches each provider actually
predicted. The workflow drops failed or invalid LLM answers, so they show up
as gaps in the snapshots.

Usage::

    uv run python utils/eval_providers.py --env-file /path/to/.env.local
    uv run python utils/eval_providers.py --league Bundesliga --season 2026/27
"""

# * Author(s): Thomas Glanzer
# * Creation : Oct 2026
# * License: MIT license

# %% --------------------------------------------
# * Libraries

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import tempfile
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# %% --------------------------------------------
# * Constants

REPO_ROOT = Path(__file__).parents[1]
BERLIN = ZoneInfo("Europe/Berlin")
KEY_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})_Match_Predictions_(.+)"
    r"_FourPointsScoring_named_with-info(?:\.jsonl)?$"
)
ESPN_LEAGUES = {
    "Premier League - England": "eng.1",
    "La Liga - Spain": "esp.1",
    "Bundesliga - Germany": "ger.1",
    "UEFA Champions League": "uefa.champions",
    "UEFA Europa League": "uefa.europa",
    "Bundesliga - Austria": "aut.1",
}
BUNDESLIGA = "Bundesliga - Germany"
# Providers currently offered in the frontend (public/js/main.js LLM_PROVIDERS)
CURRENT_PROVIDERS = [
    "Deepseek-Chat",
    "Google-Gemma-Medium",
    "OpenRouter-GPT-OSS-Large",
    "Nvidia-Nemotron-Lightning",
    "Mistral-Ministral",
    "Meta-Llama-Mid",
    "Microsoft-Phi-Medium",
]
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
class Prediction:
    """One provider prediction for one match taken from one snapshot."""

    provider: str
    snapshot: datetime
    league: str
    kickoff: datetime
    home: str
    away: str
    home_goals: int
    away_goals: int


@dataclass(frozen=True)
class Result:
    """Final (90 minute) score of a finished match."""

    league: str
    kickoff: datetime
    home: str
    away: str
    home_goals: int
    away_goals: int


@dataclass
class Score:
    """Aggregated points for one provider."""

    matches: int = 0
    points: int = 0
    exact: int = 0
    goal_diff: int = 0
    tendency: int = 0
    squares: int = 0

    def add(self, points: int) -> None:
        """Count one scored match."""
        self.matches += 1
        self.points += points
        self.squares += points * points
        self.exact += points == 4  # noqa: PLR2004
        self.goal_diff += points >= 3  # noqa: PLR2004
        self.tendency += points >= 2  # noqa: PLR2004

    @property
    def std_error(self) -> float:
        """Standard error of the average points per match."""
        if self.matches < 2:  # noqa: PLR2004
            return 0.0
        mean = self.points / self.matches
        variance = (self.squares - self.matches * mean * mean) / (self.matches - 1)
        return (variance / self.matches) ** 0.5


# %% --------------------------------------------
# * Helpers


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


class HttpCache:
    """Tiny JSON-on-disk cache for GET requests."""

    def __init__(self, directory: Path) -> None:
        """Create cache directory."""
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()

    def get_json(self, url: str, *, cacheable: bool = True) -> object:
        """Fetch URL as JSON, reading from and writing to the cache."""
        path = self.directory / (re.sub(r"[^A-Za-z0-9]+", "_", url)[-180:] + ".json")
        if cacheable and path.exists():
            return json.loads(path.read_text())
        response = self.session.get(url, timeout=30)
        response.raise_for_status()
        data = response.json()
        if cacheable:
            path.write_text(json.dumps(data))
        return data


# %% --------------------------------------------
# * Loading predictions


def kv_command(url: str, token: str, *args: str) -> object:
    """Run one read-only Upstash REST command."""
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}"},
        json=list(args),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["result"]


def load_kv_snapshots() -> dict[tuple[datetime, str], list[dict]]:
    """Load all timestamped prediction snapshots from KV."""
    url = os.environ["KV_REST_API_URL"]
    token = (
        os.environ.get("KV_REST_API_READ_ONLY_TOKEN") or os.environ["KV_REST_API_TOKEN"]
    )
    keys: list[str] = []
    cursor = "0"
    while True:
        result = kv_command(url, token, "SCAN", cursor, "COUNT", "1000")
        cursor, batch = result  # type: ignore[misc]
        keys.extend(batch)
        if cursor == "0":
            break
    keys = sorted(k for k in keys if KEY_PATTERN.match(k))
    snapshots: dict[tuple[datetime, str], list[dict]] = {}
    for start in range(0, len(keys), 25):
        batch = keys[start : start + 25]
        values = kv_command(url, token, "MGET", *batch)
        for key, value in zip(batch, values, strict=True):  # type: ignore[arg-type]
            if value is None:
                continue
            match = KEY_PATTERN.match(key)
            assert match  # noqa: S101
            stamp = datetime.strptime(match[1], "%Y-%m-%d_%H-%M-%S").replace(tzinfo=UTC)
            snapshots[stamp, match[2]] = json.loads(value)
    logger.info("Loaded %d snapshots from KV", len(snapshots))
    return snapshots


def load_local_snapshots(
    directory: Path, before: datetime
) -> dict[tuple[datetime, str], list[dict]]:
    """Load local jsonl snapshots taken before the first KV snapshot."""
    snapshots: dict[tuple[datetime, str], list[dict]] = {}
    if not directory.is_dir():
        return snapshots
    for path in sorted(directory.glob("20*.jsonl")):
        match = KEY_PATTERN.match(path.name)
        if not match:
            continue
        stamp = datetime.strptime(match[1], "%Y-%m-%d_%H-%M-%S").replace(tzinfo=UTC)
        if stamp >= before:
            continue
        lines = path.read_text().splitlines()
        snapshots[stamp, match[2]] = [json.loads(line) for line in lines if line]
    logger.info("Loaded %d local snapshots from %s", len(snapshots), directory)
    return snapshots


def canonical_provider(name: str, counts: dict[str, int]) -> str:
    """Map case variants (e.g. meta-llama-70b) to the most used spelling."""
    variants = [c for c in counts if c.lower() == name.lower()]
    return max(variants, key=lambda c: counts[c])


def flatten(snapshots: dict[tuple[datetime, str], list[dict]]) -> list[Prediction]:
    """Turn snapshots into a flat list of predictions."""
    counts: dict[str, int] = defaultdict(int)
    for _, provider in snapshots:
        counts[provider] += 1
    predictions = []
    for (stamp, provider), leagues in snapshots.items():
        name = canonical_provider(provider, counts)
        for league in leagues:
            for match in league.get("matches", []):
                try:
                    kickoff = (
                        datetime.strptime(match["commence_time_str"], "%d.%m.%Y %H:%M")
                        .replace(tzinfo=BERLIN)
                        .astimezone(UTC)
                    )
                    home_goals = int(match["prediction_home"])
                    away_goals = int(match["prediction_away"])
                except (KeyError, TypeError, ValueError):
                    continue
                predictions.append(
                    Prediction(
                        name,
                        stamp,
                        league["name"],
                        kickoff,
                        match["home_team"],
                        match["away_team"],
                        home_goals,
                        away_goals,
                    )
                )
    return predictions


def last_before_kickoff(predictions: list[Prediction]) -> list[Prediction]:
    """Keep, per provider and fixture, the last snapshot taken before kickoff.

    A fixture is identified by league, teams and season so that rescheduled
    kickoffs collapse into one entry; the kickoff from the newest snapshot wins.
    """
    groups: dict[tuple, list[Prediction]] = defaultdict(list)
    for pred in predictions:
        season = season_of(pred.kickoff)
        key = (pred.provider, pred.league, pred.home, pred.away, season)
        groups[key].append(pred)
    selected = []
    for group in groups.values():
        latest_kickoff = max(group, key=lambda p: p.snapshot).kickoff
        valid = [p for p in group if p.snapshot < min(p.kickoff, latest_kickoff)]
        if valid:
            selected.append(max(valid, key=lambda p: p.snapshot))
    return selected


# %% --------------------------------------------
# * Loading results


def openligadb_results(cache: HttpCache, seasons: set[int]) -> list[Result]:
    """Fetch finished Bundesliga matches from openligadb.de."""
    results = []
    current = season_of(datetime.now(tz=UTC))
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
                )
            )
    return results


def espn_regular_time(competitor: dict) -> int | None:
    """Return goals after 90 minutes from ESPN period line scores."""
    lines = competitor.get("linescores") or []
    if len(lines) < 2:  # noqa: PLR2004
        return None
    return int(sum(float(line.get("value", 0)) for line in lines[:2]))


def espn_results(cache: HttpCache, league: str, days: set[date]) -> list[Result]:
    """Fetch finished matches for one league on the given days from ESPN."""
    code = ESPN_LEAGUES[league]
    today = datetime.now(tz=UTC).date()
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
            status = event["status"]["type"]
            if not status.get("completed"):
                continue
            comp = event["competitions"][0]
            teams = {c["homeAway"]: c for c in comp["competitors"]}
            if "home" not in teams or "away" not in teams:
                continue
            if any(tag in status.get("name", "") for tag in ("AET", "PEN")):
                home_goals = espn_regular_time(teams["home"])
                away_goals = espn_regular_time(teams["away"])
                if home_goals is None or away_goals is None:
                    continue
            else:
                home_goals = int(teams["home"]["score"])
                away_goals = int(teams["away"]["score"])
            results.append(
                Result(
                    league,
                    datetime.fromisoformat(event["date"]),
                    teams["home"]["team"]["displayName"],
                    teams["away"]["team"]["displayName"],
                    home_goals,
                    away_goals,
                )
            )
    return results


def load_results(cache: HttpCache, predictions: list[Prediction]) -> list[Result]:
    """Fetch results for every league and day that has predictions."""
    days: dict[str, set[date]] = defaultdict(set)
    bl_seasons: set[int] = set()
    for pred in predictions:
        if pred.league == BUNDESLIGA:
            bl_seasons.add(int(season_of(pred.kickoff)[:4]))
        elif pred.league in ESPN_LEAGUES:
            days[pred.league].add(pred.kickoff.date())
            days[pred.league].add(pred.kickoff.astimezone(BERLIN).date())
    results = openligadb_results(cache, bl_seasons)
    for league, league_days in days.items():
        logger.info("Fetching %d days of %s results", len(league_days), league)
        results.extend(espn_results(cache, league, league_days))
    unknown = {p.league for p in predictions} - set(ESPN_LEAGUES)
    if unknown:
        logger.warning("No result source for leagues: %s", sorted(unknown))
    logger.info("Loaded %d finished results", len(results))
    return results


def match_results(
    predictions: list[Prediction], results: list[Result]
) -> dict[tuple, Result]:
    """Map each fixture (league, home, away, kickoff) to its actual result."""
    by_league: dict[str, list[Result]] = defaultdict(list)
    for result in results:
        by_league[result.league].append(result)
    fixtures = {(p.league, p.home, p.away, p.kickoff) for p in predictions}
    matched = {}
    for league, home, away, kickoff in fixtures:
        best, best_score = None, 0.0
        for result in by_league.get(league, []):
            gap = abs(result.kickoff - kickoff)
            if gap > MATCH_WINDOW:
                continue
            sim_home = team_similarity(home, result.home)
            sim_away = team_similarity(away, result.away)
            if min(sim_home, sim_away) < MIN_TEAM_SIMILARITY:
                continue
            score = sim_home + sim_away - gap / MATCH_WINDOW * 0.2
            if score > best_score:
                best, best_score = result, score
        if best and best_score >= MIN_PAIR_SIMILARITY:
            matched[league, home, away, kickoff] = best
    return matched


# %% --------------------------------------------
# * Reporting


def print_table(title: str, scores: dict[str, Score]) -> list[str]:
    """Print one ranking table and return providers ordered by average."""
    ranked = sorted(
        (s for s in scores.items() if s[1].matches),
        key=lambda item: (-item[1].points / item[1].matches, -item[1].matches),
    )
    print(f"\n=== {title} ===")
    if not ranked:
        print("(no scored matches)")
        return []
    print(
        f"{'#':>2} {'Provider':<30} {'Matches':>7} {'Points':>6} {'Avg':>5} "
        f"{'±SE':>5} {'Exact':>6} {'GoalDiff':>8} {'Tendency':>8}"
    )
    for rank, (provider, s) in enumerate(ranked, 1):
        marker = "*" if provider in CURRENT_PROVIDERS else " "
        print(
            f"{rank:>2} {provider + marker:<30} {s.matches:>7} {s.points:>6} "
            f"{s.points / s.matches:>5.2f} "
            f"{s.std_error:>5.2f} {s.exact / s.matches:>6.1%} "
            f"{s.goal_diff / s.matches:>8.1%} {s.tendency / s.matches:>8.1%}"
        )
    return [provider for provider, _ in ranked]


def aggregate(scored: list[tuple[Prediction, int]]) -> dict[str, Score]:
    """Aggregate points per provider."""
    scores: dict[str, Score] = defaultdict(Score)
    for pred, points in scored:
        scores[pred.provider].add(points)
    return scores


def print_report(scored: list[tuple[Prediction, int]], season: str) -> None:
    """Print overall, per-league, per-season and head-to-head tables."""
    print("\n* = currently offered in the frontend")
    print(
        "Exact = 4 pts, GoalDiff = 3+ pts (incl. exact), "
        "Tendency = 2+ pts (any correct tendency), "
        "±SE = standard error of Avg"
    )
    print_table("All leagues, all seasons", aggregate(scored))
    for league in sorted({p.league for p, _ in scored}):
        rows = [row for row in scored if row[0].league == league]
        print_table(f"{league}, all seasons", aggregate(rows))
    for one_season in sorted({season_of(p.kickoff) for p, _ in scored}):
        rows = [row for row in scored if season_of(row[0].kickoff) == one_season]
        print_table(f"Season {one_season}, all leagues", aggregate(rows))

    season_rows = [row for row in scored if season_of(row[0].kickoff) == season]
    bundesliga_rows = [row for row in season_rows if row[0].league == BUNDESLIGA]
    print_table(f"Season {season}, {BUNDESLIGA}", aggregate(bundesliga_rows))
    print_head_to_head(bundesliga_rows, f"Season {season}, {BUNDESLIGA}")
    print_head_to_head(season_rows, f"Season {season}, all leagues")


def print_head_to_head(rows: list[tuple[Prediction, int]], label: str) -> None:
    """Rank current providers on fixtures every one of them predicted.

    Also ranks the current providers without Nemotron and Ministral, which
    only joined recently, so the others are compared on a larger sample.
    """
    by_fixture: dict[tuple, dict[str, int]] = defaultdict(dict)
    for pred, points in rows:
        if pred.provider in CURRENT_PROVIDERS:
            fixture = (pred.league, pred.home, pred.away, pred.kickoff.date())
            by_fixture[fixture][pred.provider] = points
    recent = {"Nvidia-Nemotron-Lightning", "Mistral-Ministral"}
    groups = [
        ("all current providers", set(CURRENT_PROVIDERS)),
        ("current providers excl. Nemotron/Ministral", set(CURRENT_PROVIDERS) - recent),
    ]
    for group_label, group in groups:
        scores: dict[str, Score] = defaultdict(Score)
        for points_by_provider in by_fixture.values():
            if group <= points_by_provider.keys():
                for provider in group:
                    scores[provider].add(points_by_provider[provider])
        print_table(f"{label}, head-to-head on common fixtures, {group_label}", scores)


# %% --------------------------------------------
# * Reliability

RUN_GAP = timedelta(minutes=30)


@dataclass
class Coverage:
    """Prediction coverage counts for one provider."""

    runs: int = 0
    missed_runs: int = 0
    offered: int = 0
    predicted: int = 0
    past_fixtures: int = 0
    past_predicted: int = 0


def group_runs(stamps: set[datetime]) -> dict[datetime, datetime]:
    """Map each snapshot timestamp to the start of its workflow run.

    One run writes a snapshot per provider within seconds, so timestamps
    closer than RUN_GAP belong to the same run.
    """
    run_of: dict[datetime, datetime] = {}
    run_start: datetime | None = None
    previous: datetime | None = None
    for stamp in sorted(stamps):
        if previous is None or stamp - previous > RUN_GAP:
            run_start = stamp
        assert run_start is not None  # noqa: S101
        run_of[stamp] = run_start
        previous = stamp
    return run_of


def reliability(
    raw: list[Prediction], selected: list[Prediction], season: str | None
) -> dict[str, Coverage]:
    """Measure how many offered matches each provider actually predicted.

    The workflow drops matches whose LLM call failed or returned an invalid
    tip, and writes no snapshot at all for a provider without any valid tip.
    So per run, a fixture predicted by any provider counts as offered to all
    providers active then (between their first and last run); a provider with
    no snapshot in such a run missed the whole run. Pre-kickoff coverage
    compares the selected last-before-kickoff tips the same way, since a later
    run can fill a gap.
    """
    run_of = group_runs({p.snapshot for p in raw})
    offered: dict[datetime, set[tuple]] = defaultdict(set)
    delivered: dict[tuple[str, datetime], set[tuple]] = defaultdict(set)
    for pred in raw:
        fixture = (pred.league, pred.home, pred.away)
        offered[run_of[pred.snapshot]].add(fixture)
        delivered[pred.provider, run_of[pred.snapshot]].add(fixture)
    runs = sorted(offered)
    providers = {p.provider for p in raw}
    window = {
        provider: (
            min(run for prov, run in delivered if prov == provider),
            max(run for prov, run in delivered if prov == provider),
        )
        for provider in providers
    }
    coverage: dict[str, Coverage] = defaultdict(Coverage)
    for provider, (first, last) in window.items():
        for run in runs:
            if not first <= run <= last:
                continue
            if season and season_of(run) != season:
                continue
            got = delivered.get((provider, run), set())
            cov = coverage[provider]
            cov.runs += 1
            cov.missed_runs += not got
            cov.offered += len(offered[run])
            cov.predicted += len(got & offered[run])

    now = datetime.now(tz=UTC)
    fixtures: dict[tuple, datetime] = {}
    tipped: dict[str, set[tuple]] = defaultdict(set)
    for pred in selected:
        if pred.kickoff >= now:
            continue
        fixture = (pred.league, pred.home, pred.away, season_of(pred.kickoff))
        fixtures[fixture] = pred.kickoff
        tipped[pred.provider].add(fixture)
    for provider, (first, last) in window.items():
        last_kickoff = max(
            (p.kickoff for p in selected if p.provider == provider), default=last
        )
        for fixture, kickoff in fixtures.items():
            if not first < kickoff <= last_kickoff:
                continue
            if season and season_of(kickoff) != season:
                continue
            coverage[provider].past_fixtures += 1
            coverage[provider].past_predicted += fixture in tipped[provider]
    return coverage


def print_reliability(title: str, coverage: dict[str, Coverage]) -> None:
    """Print one reliability table, least reliable providers last."""
    rows = [(p, c) for p, c in coverage.items() if c.offered or c.past_fixtures]
    rows.sort(key=lambda item: -item[1].predicted / max(item[1].offered, 1))
    print(f"\n=== Reliability: {title} ===")
    if not rows:
        print("(no data)")
        return
    print(
        f"{'Provider':<30} {'Runs':>5} {'Missed':>6} {'Offered':>7} "
        f"{'Tipped':>7} {'PerRun':>6} {'Past':>6} {'Tipped':>7} {'PreKO':>6}"
    )
    for provider, c in rows:
        marker = "*" if provider in CURRENT_PROVIDERS else " "
        per_run = c.predicted / c.offered if c.offered else 0.0
        pre_ko = c.past_predicted / c.past_fixtures if c.past_fixtures else 0.0
        print(
            f"{provider + marker:<30} {c.runs:>5} {c.missed_runs:>6} "
            f"{c.offered:>7} {c.predicted:>7} {per_run:>6.1%} "
            f"{c.past_fixtures:>6} {c.past_predicted:>7} {pre_ko:>6.1%}"
        )


def print_reliability_report(
    raw: list[Prediction], selected: list[Prediction], season: str
) -> None:
    """Print reliability for all seasons and for the focused season."""
    print(
        "\nReliability: Runs = workflow runs while the provider was active, "
        "Missed = runs with no snapshot at all, PerRun = share of matches "
        "offered in those runs that got a valid tip, PreKO = share of past "
        "matches with a tip before kickoff"
    )
    print_reliability("all seasons", reliability(raw, selected, None))
    print_reliability(f"season {season}", reliability(raw, selected, season))


# %% --------------------------------------------
# * Main


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--env-file",
        type=Path,
        default=REPO_ROOT / ".env.local",
        help="dotenv file with KV_REST_API_URL and a KV token",
    )
    parser.add_argument(
        "--local-dir",
        type=Path,
        default=REPO_ROOT / "data" / "match_predictions",
        help="directory with older local jsonl snapshots",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "tip-genius-eval-cache",
        help="where to cache result API responses",
    )
    parser.add_argument("--league", help="only leagues containing this text")
    parser.add_argument(
        "--season",
        default=season_of(datetime.now(tz=UTC)),
        help="season for the focused ranking, e.g. 2026/27",
    )
    parser.add_argument(
        "--show-unmatched",
        action="store_true",
        help="list past fixtures without a matched result",
    )
    return parser.parse_args()


def main() -> None:
    """Run the backtest and print the report."""
    args = parse_args()
    load_dotenv(args.env_file)
    snapshots = load_kv_snapshots()
    first_kv = min(stamp for stamp, _ in snapshots)
    snapshots |= load_local_snapshots(args.local_dir, before=first_kv)

    raw = flatten(snapshots)
    if args.league:
        wanted = args.league.lower()
        raw = [p for p in raw if wanted in p.league.lower()]
    predictions = last_before_kickoff(raw)
    now = datetime.now(tz=UTC)
    past = [p for p in predictions if p.kickoff < now - timedelta(hours=3)]

    cache = HttpCache(args.cache_dir)
    results = load_results(cache, past)
    matched = match_results(past, results)

    scored = []
    unmatched = set()
    for pred in past:
        result = matched.get((pred.league, pred.home, pred.away, pred.kickoff))
        if result is None:
            unmatched.add((pred.league, pred.kickoff, pred.home, pred.away))
            continue
        points = kicktipp_points(
            (pred.home_goals, pred.away_goals),
            (result.home_goals, result.away_goals),
        )
        scored.append((pred, points))

    fixtures = {(p.league, p.home, p.away, p.kickoff) for p in past}
    print(
        f"\nPast fixtures with predictions: {len(fixtures)}, "
        f"matched to results: {len(matched)}, unmatched: {len(unmatched)}"
    )
    if args.show_unmatched:
        for league, kickoff, home, away in sorted(unmatched):
            print(f"  unmatched: {league} {kickoff:%Y-%m-%d %H:%M} {home} - {away}")
    print_report(scored, args.season)
    print_reliability_report(raw, predictions, args.season)


if __name__ == "__main__":
    main()
