"""Backfill predictions of late-joining providers for past runs (Blob only).

Mistral-Ministral and Nvidia-Nemotron-Lightning first ran on 14.10.2026, so
the 2026/27 fixtures that kicked off before have no prediction of theirs. This
script re-creates those predictions for the original runs and archives them as
``predictions/backfill/<run_ts>_<base_key>.jsonl`` (see
``src/tip_genius/lib/blob_archive.py``). It never writes to KV: back-dated KV
keys would pretend the predictions were made before kickoff.

How a run is rebuilt:

1. ``index.json`` (read fresh) lists every archived run. The prediction files
   of the other providers give the fixtures and kickoffs each run offered.
2. A fixture is a pairing per league and season; rescheduled kickoffs collapse
   into one fixture, UEFA repeat pairings (league phase and knockout) more
   than 7 days apart stay apart. Its final kickoff is the one of the latest
   run that offered it.
3. A provider gets a backfill task for a fixture if it has no pre-kickoff
   prediction of its own and the latest run that offered the fixture before
   kickoff ran before the provider's first live run. That run is the one the
   prediction belongs to. Fixtures the provider missed live (API errors) are
   out of scope and only counted.
4. Odds come from the run's archived odds files (``odds_source`` logged), else
   from the reconstructed odds CSV (estimated; rows with ``source=logged``
   count as logged). Default CSV: the newest ``odds/reconstructed/`` file in
   the index; ``--odds-csv`` uses a local file instead. Fixtures without all
   three odds are reported and not predicted, as in the live workflow.
5. The prompt is built with ``lib.api_data.build_odds_summary`` (shared with
   the workflow, so it is byte-identical for the same odds), and the rows go
   through ``TipGenius.predict_results`` with today's ``llm_config.yaml``, so
   retries, validation and the consistency check are the live ones. Only valid
   predictions are archived; each JSONL line carries a ``backfill`` marker.
6. Written files are added to ``index.json`` under ``backfill_predictions``
   (separate from ``runs``) in one write at the end of the run, also after a
   crash; the write is read back and retried, because a fresh read can lag
   behind a recent write. Files listed there or already present
   in Blob are skipped, so an interrupted run can simply be restarted.

``--dry-run`` (the default) makes no LLM calls and writes nothing; it prints
the fixtures per run and provider and the number of LLM calls. ``--execute``
calls the LLMs (real API spend) and writes to Blob; ``--max-calls`` caps the
number of predicted fixtures (each takes 1 to 4 requests); the run stops at
the first file that would exceed it. ``--execute`` refuses to start, and stops
between files, on Tue/Fri 02:00-17:00 UTC: the cron (03:00 UTC, often started
hours late) also updates ``index.json``, a read-modify-write.

Usage:
    uv run python utils/backfill_llm_predictions.py --env-file /path/.env.local
    uv run python utils/backfill_llm_predictions.py --env-file ... --execute \
        --max-calls 20
"""
# * Author(s): Thomas Glanzer
# * Creation : Nov 2026
# * License: MIT license

from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import sys
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from zoneinfo import ZoneInfo

import polars as pl
import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parents[1] / "src" / "tip_genius"))

from lib import blob_archive  # noqa: E402
from lib.api_data import (  # noqa: E402
    OddsAPI,
    build_odds_summary,
    prediction_columns,
)

from tip_genius import (
    TipGenius,  # noqa: E402  # pyright: ignore[reportAttributeAccessIssue]
)

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# %% --------------------------------------------
# * Constants

REPO_ROOT = Path(__file__).parents[1]
CFG_DIR = REPO_ROOT / "src" / "tip_genius" / "cfg"
SCRIPT_NAME = Path(__file__).name
BERLIN = ZoneInfo("Europe/Berlin")
PROVIDERS = ["Mistral-Ministral", "Nvidia-Nemotron-Lightning"]
DEFAULT_SEASON = "2026/27"
# Runs this long before the season start can already offer its first fixtures
RUN_LOOKBACK = timedelta(days=60)
# UEFA pairings can meet twice per season (league phase and knockout)
REPEAT_LEAGUE_MARKER = "UEFA"
LEG_GAP = timedelta(days=7)
# Reconstructed odds with another kickoff of the same pairing (reschedules)
ODDS_KICKOFF_TOLERANCE = timedelta(days=21)
DEFAULT_SLEEP = 1.0
# Tue/Fri, from shortly before the 03:00 UTC cron until delayed runs are done
CRON_WEEKDAYS = {1, 4}
CRON_WINDOW_START = 2
CRON_WINDOW_END = 17
# Columns of a live prediction entry, in the order the workflow writes them
MATCH_COLUMNS = [
    "commence_time_str",
    "home_team",
    "away_team",
    "prediction_home",
    "prediction_away",
    "outlook",
    "reasoning",
]

# %% --------------------------------------------
# * Data model


@dataclass(frozen=True)
class Offer:
    """A fixture as one run offered it (taken from a live prediction file)."""

    run_ts: str
    provider: str
    league: str
    home_team: str
    away_team: str
    commence_time_str: str  # Europe/Berlin, as in the prediction files
    home_logo: str | None = None
    away_logo: str | None = None

    @property
    def kickoff(self) -> datetime:
        """Return the kickoff in UTC."""
        return berlin_to_utc(self.commence_time_str)

    @property
    def run_start(self) -> datetime:
        """Return the run timestamp as a UTC datetime."""
        return parse_ts(self.run_ts)


@dataclass(frozen=True)
class Odds:
    """1X2 odds of one fixture and where they come from."""

    home: float
    draw: float
    away: float
    source: str  # "logged" or "reconstructed"


@dataclass
class Fixture:
    """One match of the season with every run's offer of it."""

    league: str
    home_team: str
    away_team: str
    offers: list[Offer] = field(default_factory=list)

    @property
    def final_offer(self) -> Offer:
        """Return the offer of the latest run (its kickoff is the final one)."""
        return max(self.offers, key=lambda o: o.run_ts)

    @property
    def kickoff(self) -> datetime:
        """Return the final kickoff (UTC)."""
        return self.final_offer.kickoff


@dataclass
class Task:
    """One fixture to predict for one provider in one original run."""

    provider: str
    offer: Offer
    odds: Odds | None


@dataclass
class Plan:
    """Backfill tasks plus what was left out and why."""

    first_live: dict[str, str | None] = field(default_factory=dict)
    tasks: list[Task] = field(default_factory=list)
    missed_live: Counter[str] = field(default_factory=Counter)
    no_offer: Counter[str] = field(default_factory=Counter)

    def by_file(self) -> dict[tuple[str, str], list[Task]]:
        """Group tasks with odds by ``(run_ts, provider)``, oldest run first."""
        files: dict[tuple[str, str], list[Task]] = defaultdict(list)
        for task in sorted(self.tasks, key=lambda t: (t.offer.run_ts, t.provider)):
            if task.odds is not None:
                files[task.offer.run_ts, task.provider].append(task)
        return dict(files)


@dataclass
class Outcome:
    """Result of one ``(run_ts, provider)`` file."""

    status: str  # planned, archived, indexed, written, unindexed, failed, skipped
    rows: int = 0
    predicted: int = 0
    failed: list[Task] = field(default_factory=list)
    pathname: str = ""


# %% --------------------------------------------
# * Helpers


def parse_ts(text: str) -> datetime:
    """Parse a ``YYYY-MM-DD_HH-MM-SS`` timestamp (UTC)."""
    return datetime.strptime(text, blob_archive.RUN_TS_FORMAT).replace(tzinfo=UTC)


def parse_iso(text: str) -> datetime:
    """Parse an ISO timestamp such as ``2026-10-30T09:49:58Z`` (UTC)."""
    return datetime.fromisoformat(text).astimezone(UTC)


def berlin_to_utc(text: str) -> datetime:
    """Convert a prediction's ``dd.mm.YYYY HH:MM`` (Europe/Berlin) to UTC."""
    local = datetime.strptime(text, "%d.%m.%Y %H:%M").replace(tzinfo=BERLIN)
    return local.astimezone(UTC)


def season_of(moment: datetime) -> str:
    """Return the season label like ``2026/27`` (seasons start on 1 July)."""
    start = moment.year if moment.month >= 7 else moment.year - 1  # noqa: PLR2004
    return f"{start}/{(start + 1) % 100:02d}"


def season_start(season: str) -> datetime:
    """Return 1 July of the season's first year (UTC)."""
    return datetime(int(season[:4]), 7, 1, tzinfo=UTC)


@dataclass(frozen=True)
class PromptOptions:
    """The single prompt combination the workflow runs (tip_genius_config)."""

    prediction_type: str
    named_teams: bool
    additional_info: bool

    @property
    def suffix(self) -> str:
        """Return the base key suffix, e.g. ``FourPointsScoring_named_with-info``."""
        return (
            f"{self.prediction_type}_"
            f"{'named' if self.named_teams else 'anonymized'}_"
            f"{'with-info' if self.additional_info else 'no-info'}"
        )

    def base_key(self, provider: str) -> str:
        """Return the prediction base key of a provider."""
        return f"{blob_archive.PREDICTIONS_PREFIX}{provider}_{self.suffix}"

    def provider_key(self, provider: str) -> str:
        """Return the index/manifest key of a provider."""
        return blob_archive.provider_key(self.base_key(provider))


def load_prompt_options(config_path: Path) -> PromptOptions:
    """Read the prompt combination; the backfill supports exactly one."""
    with config_path.open(encoding="utf-8") as f:
        config = yaml.safe_load(f)
    options = [
        config["prediction_type_options"],
        config["named_teams_options"],
        config["additional_info_options"],
    ]
    if any(len(values) != 1 for values in options):
        msg = f"Expected exactly one prompt combination in {config_path}"
        raise ValueError(msg)
    return PromptOptions(options[0][0], bool(options[1][0]), bool(options[2][0]))


def load_sport_titles(config_path: Path) -> dict[str, str]:
    """Map league names to the Odds API ``sport_title`` used in the prompt."""
    with config_path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)["odds_api"]["sport_titles"]


class CachedReader(blob_archive.BlobArchiveReader):
    """``BlobArchiveReader`` with a disk cache for downloaded files.

    Archived run files are write-once, so a cached copy never goes stale.
    """

    def __init__(self, cache_dir: Path | None) -> None:
        """Initialize; ``cache_dir`` None disables the cache."""
        super().__init__()
        self.cache_dir = cache_dir

    def fetch_bytes(self, pathname: str) -> bytes:
        """Download one file, or read it from the cache."""
        cached = self.cache_dir / pathname if self.cache_dir else None
        if cached is not None and cached.is_file():
            return cached.read_bytes()
        body = super().fetch_bytes(pathname)
        if cached is not None:
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(body)
        return body


# %% --------------------------------------------
# * Run reconstruction


def load_offers(
    runs: list[dict[str, Any]],
    options: PromptOptions,
    fetch_jsonl: Callable[[str], list[dict[str, Any]]],
    since: datetime,
) -> list[Offer]:
    """Collect every fixture each run offered, per provider that predicted it."""
    suffix = f"_{options.suffix}"
    offers: list[Offer] = []
    for run in runs:
        if parse_ts(run["run_ts"]) < since:
            continue
        for key, pathname in sorted(run.get("predictions", {}).items()):
            if not key.endswith(suffix):
                continue
            provider = key.removesuffix(suffix)
            for league in fetch_jsonl(pathname):
                offers.extend(
                    Offer(
                        run_ts=run["run_ts"],
                        provider=provider,
                        league=league["name"],
                        home_team=m["home_team"],
                        away_team=m["away_team"],
                        commence_time_str=m["commence_time_str"],
                        home_logo=m.get("home_logo"),
                        away_logo=m.get("away_logo"),
                    )
                    for m in league.get("matches", [])
                )
    return offers


def group_fixtures(offers: list[Offer]) -> list[Fixture]:
    """Group offers into fixtures (pairing per league and season, UEFA legs apart).

    Kickoffs of one pairing collapse into one fixture (reschedules), except in
    UEFA competitions, where kickoffs more than LEG_GAP apart are separate
    meetings (league phase and knockout).
    """
    pairings: dict[tuple[str, str, str, str], list[Offer]] = defaultdict(list)
    for offer in offers:
        key = (
            offer.league,
            offer.home_team,
            offer.away_team,
            season_of(offer.kickoff),
        )
        pairings[key].append(offer)
    fixtures: list[Fixture] = []
    for (league, home, away, _), pairing_offers in pairings.items():
        ordered = sorted(pairing_offers, key=lambda o: (o.kickoff, o.run_ts))
        current = Fixture(league, home, away)
        for offer in ordered:
            if (
                current.offers
                and REPEAT_LEAGUE_MARKER in league
                and offer.kickoff - current.offers[-1].kickoff > LEG_GAP
            ):
                fixtures.append(current)
                current = Fixture(league, home, away)
            current.offers.append(offer)
        fixtures.append(current)
    return sorted(fixtures, key=lambda f: (f.kickoff, f.league, f.home_team))


def first_live_runs(
    runs: list[dict[str, Any]], providers: list[str], options: PromptOptions
) -> dict[str, str | None]:
    """Return each provider's first run with a live prediction file."""
    first: dict[str, str | None] = {}
    for provider in providers:
        key = options.provider_key(provider)
        first[provider] = min(
            (run["run_ts"] for run in runs if key in run.get("predictions", {})),
            default=None,
        )
    return first


def plan_tasks(
    fixtures: list[Fixture],
    providers: list[str],
    first_live: dict[str, str | None],
    season: str,
    odds_lookup: Callable[[Offer], Odds | None],
) -> Plan:
    """Decide which fixtures each provider needs and in which original run.

    A provider needs a fixture of ``season`` if it has no pre-kickoff
    prediction of its own. The task belongs to the latest run that offered the
    fixture before its kickoff; if that run is at or after the provider's first
    live run, the provider missed it live (out of scope).
    """
    plan = Plan(first_live=dict(first_live))
    for fixture in fixtures:
        if season_of(fixture.kickoff) != season:
            continue
        # As eval_providers.last_before_kickoff: before the offered and the
        # final kickoff (a match moved earlier makes later offers stale)
        final_kickoff = fixture.kickoff
        pre_kickoff = [
            o for o in fixture.offers if o.run_start < min(o.kickoff, final_kickoff)
        ]
        for provider in providers:
            if any(o.provider == provider for o in pre_kickoff):
                continue
            candidates = [o for o in pre_kickoff if o.provider != provider]
            if not candidates:
                plan.no_offer[provider] += 1
                continue
            latest_run = max(o.run_ts for o in candidates)
            live_from = first_live.get(provider)
            if live_from is not None and latest_run >= live_from:
                plan.missed_live[provider] += 1
                continue
            run_offers = [o for o in candidates if o.run_ts == latest_run]
            # Any provider's entry works; prefer one with logos
            offer = max(run_offers, key=lambda o: (bool(o.home_logo), o.provider))
            plan.tasks.append(Task(provider, offer, odds_lookup(offer)))
    return plan


# %% --------------------------------------------
# * Odds


def valid_odds(home: Any, draw: Any, away: Any) -> tuple[float, float, float] | None:
    """Return the three odds as floats, or None unless all are positive."""
    try:
        values = (float(home), float(draw), float(away))
    except (TypeError, ValueError):
        return None
    return values if all(v > 0 for v in values) else None


class OddsLookup:
    """Find odds for an offer: the run's logged odds first, then the CSV."""

    def __init__(
        self,
        runs: list[dict[str, Any]],
        fetch_jsonl: Callable[[str], list[dict[str, Any]]],
        csv_text: str | None,
    ) -> None:
        """Index the CSV; run odds files are loaded lazily per run."""
        self.runs = {run["run_ts"]: run for run in runs}
        self.fetch_jsonl = fetch_jsonl
        self._run_odds: dict[str, dict[tuple[str, str, datetime], Odds]] = {}
        self.csv: dict[tuple[str, str, str, str], list[tuple[datetime, Odds]]] = (
            defaultdict(list)
        )
        if csv_text:
            for row in csv.DictReader(io.StringIO(csv_text)):
                values = valid_odds(
                    row["odds_home"], row["odds_draw"], row["odds_away"]
                )
                if values is None:
                    continue
                kickoff = parse_iso(row["kickoff_utc"])
                source = "logged" if row.get("source") == "logged" else "reconstructed"
                key = (row["league"], row["home_team"], row["away_team"], row["season"])
                self.csv[key].append((kickoff, Odds(*values, source=source)))

    def run_odds(self, run_ts: str) -> dict[tuple[str, str, datetime], Odds]:
        """Return the logged odds of one run keyed by ``(home, away, kickoff)``."""
        if run_ts not in self._run_odds:
            odds: dict[tuple[str, str, datetime], Odds] = {}
            for pathname in self.runs.get(run_ts, {}).get("odds", {}).values():
                for league in self.fetch_jsonl(pathname):
                    col = {name: i for i, name in enumerate(league["columns"])}
                    for row in league["matches"]:
                        values = valid_odds(
                            row[col["odds_home"]],
                            row[col["odds_draw"]],
                            row[col["odds_away"]],
                        )
                        if values is None:
                            continue
                        key = (
                            row[col["home_team"]],
                            row[col["away_team"]],
                            parse_iso(row[col["commence_time"]]),
                        )
                        odds[key] = Odds(*values, source="logged")
            self._run_odds[run_ts] = odds
        return self._run_odds[run_ts]

    def __call__(self, offer: Offer) -> Odds | None:
        """Return the odds for an offer, or None if none are known."""
        logged = self.run_odds(offer.run_ts).get(
            (offer.home_team, offer.away_team, offer.kickoff)
        )
        if logged is not None:
            return logged
        key = (offer.league, offer.home_team, offer.away_team, season_of(offer.kickoff))
        nearby = [
            (abs(kickoff - offer.kickoff), odds)
            for kickoff, odds in self.csv.get(key, [])
            if abs(kickoff - offer.kickoff) <= ODDS_KICKOFF_TOLERANCE
        ]
        return min(nearby, key=lambda item: item[0])[1] if nearby else None


def newest_reconstructed(index: dict[str, Any]) -> str | None:
    """Return the current (newest) reconstructed odds CSV of the index."""
    files = sorted(index.get("reconstructed_odds", []))
    return files[-1] if files else None


# %% --------------------------------------------
# * Prediction


def build_frame(
    tasks: list[Task], sport_title: str, options: PromptOptions
) -> pl.DataFrame:
    """Build the frame ``TipGenius.predict_results`` expects for one league."""
    rows = []
    for task in tasks:
        assert task.odds is not None
        rows.append(
            {
                "commence_time_str": task.offer.commence_time_str,
                "home_team": task.offer.home_team,
                "away_team": task.offer.away_team,
                "odds_summary": build_odds_summary(
                    home_team=task.offer.home_team,
                    away_team=task.offer.away_team,
                    odds_home=task.odds.home,
                    odds_away=task.odds.away,
                    odds_draw=task.odds.draw,
                    sport_title=sport_title,
                    named_teams=options.named_teams,
                    additional_info=options.additional_info,
                ),
                "odds_home": task.odds.home,
                "odds_away": task.odds.away,
                "odds_draw": task.odds.draw,
            }
        )
    return pl.DataFrame(rows).with_columns(*prediction_columns())


def valid_matches(data: pl.DataFrame, tasks: list[Task]) -> list[dict[str, Any]]:
    """Return the valid predictions as live match entries (with logos)."""
    matches = []
    for row, task in zip(data.iter_rows(named=True), tasks, strict=True):
        if not row["validity"]:
            continue
        entry = {column: row[column] for column in MATCH_COLUMNS}
        entry["home_logo"] = task.offer.home_logo
        entry["away_logo"] = task.offer.away_logo
        matches.append(entry)
    return matches


class Predictor(Protocol):
    """Predicts one league frame for one provider."""

    @property
    def abandoned(self) -> set[str]:
        """Providers given up on after sustained API errors."""
        ...

    def __call__(self, data: pl.DataFrame, provider: str) -> pl.DataFrame:
        """Return the frame with the prediction columns filled in."""
        ...


class WorkflowPredictor:
    """Runs the workflow's own ``TipGenius.predict_results``."""

    def __init__(self, prediction_type: str) -> None:
        """Create the TipGenius instance (no odds fetch, no storage)."""
        # The odds key is only needed to fetch odds, never to predict
        os.environ.setdefault("ODDS_API_KEY", "unused-for-backfill")
        # TipGenius would write debug log files into ./logs when the env file
        # enables DEBUG_MODE; this script logs to the console only
        os.environ["DEBUG_LOG_FILE"] = "FALSE"
        self.prediction_type = prediction_type
        self.tip_genius = TipGenius(api_pipeline=OddsAPI())
        # TipGenius resets the root logger; keep per-download lines quiet
        logging.getLogger("httpx").setLevel(logging.WARNING)

    @property
    def abandoned(self) -> set[str]:
        """Providers TipGenius gave up on after sustained API errors."""
        return self.tip_genius.abandoned_providers

    def __call__(self, data: pl.DataFrame, provider: str) -> pl.DataFrame:
        """Predict one league frame for one provider."""
        return self.tip_genius.predict_results(
            data=data, llm_provider=provider, prediction_type=self.prediction_type
        )


def near_cron(moment: datetime) -> bool:
    """Whether ``moment`` falls into the workflow window (Tue/Fri, UTC).

    The cron fires at 03:00 UTC, but GitHub often starts it hours late (runs
    in the index start up to ~16:00 UTC), so the whole window is avoided.
    """
    return moment.astimezone(UTC).weekday() in CRON_WEEKDAYS and (
        CRON_WINDOW_START <= moment.astimezone(UTC).hour < CRON_WINDOW_END
    )


def exists_in_blob(pathname: str) -> bool | None:
    """HEAD one pathname: True/False, or None if the check itself failed."""
    try:
        blob_archive.vercel_blob.head_blob(pathname)
    except blob_archive.vercel_blob.BlobNotFoundError:
        return False
    except Exception as e:
        logger.warning(
            "HEAD failed for %s (HTTP %s): %s",
            pathname,
            getattr(e, "status_code", None),
            e,
        )
        return None
    return True


# %% --------------------------------------------
# * Execution


def predict_file(
    tasks: list[Task],
    provider: str,
    sport_titles: dict[str, str],
    options: PromptOptions,
    predict: Predictor,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str], list[Task]]:
    """Predict one run's tasks per league.

    Returns matches per league, the odds source per league and the tasks
    without a valid prediction.
    """
    by_league: dict[str, list[Task]] = defaultdict(list)
    for task in tasks:
        by_league[task.offer.league].append(task)
    prediction_data: dict[str, list[dict[str, Any]]] = {}
    odds_sources: dict[str, str] = {}
    failed: list[Task] = []
    for league, league_tasks in by_league.items():
        frame = build_frame(league_tasks, sport_titles[league], options)
        matches = valid_matches(predict(frame, provider), league_tasks)
        predicted = {(m["home_team"], m["away_team"]) for m in matches}
        used = []
        for task in league_tasks:
            if (task.offer.home_team, task.offer.away_team) in predicted:
                used.append(task)
            else:
                failed.append(task)
        if matches:
            prediction_data[league] = matches
            odds_sources[league] = blob_archive.combine_odds_sources(
                [t.odds.source for t in used if t.odds is not None]
            )
    return prediction_data, odds_sources, failed


def execute_plan(  # noqa: C901, PLR0913
    plan: Plan,
    index: dict[str, Any],
    options: PromptOptions,
    sport_titles: dict[str, str],
    *,
    execute: bool,
    max_calls: int | None,
    sleep: float,
    predict_factory: Callable[[], Predictor],
    now: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
) -> dict[tuple[str, str], Outcome]:
    """Predict and archive every planned file, or only count in dry-run mode.

    Files already listed in ``backfill_predictions`` are skipped. Written files
    are indexed together at the end (in ``finally``) with a verified,
    stale-read-safe update; if that fails they show as ``unindexed`` and the
    next run indexes them. In a real run, a file already present in Blob
    (index update failed earlier) is only indexed, without new LLM calls; if
    that HEAD check fails, the file is skipped rather than paid for twice.
    Once a file would exceed ``max_calls``, or the cron window starts, no
    further file is predicted (still-pending files show as skipped). Files of
    a provider TipGenius abandoned after API errors are skipped too; the file
    during which it was abandoned is not written, so a re-run can complete it.
    """
    indexed = blob_archive.index_pathnames(index)
    outcomes: dict[tuple[str, str], Outcome] = {}
    pending: dict[str, dict[str, str]] = {}
    try:
        _run_files(
            plan,
            options,
            sport_titles,
            indexed,
            outcomes,
            pending,
            execute=execute,
            max_calls=max_calls,
            sleep=sleep,
            predict_factory=predict_factory,
            now=now,
        )
    finally:
        # One verified index write per run, also after a crash or Ctrl-C
        if pending and not blob_archive.update_backfill_index(
            pending, expected=indexed
        ):
            for outcome in outcomes.values():
                if outcome.status in {"written", "indexed"}:
                    outcome.status = "unindexed"
    return outcomes


def _run_files(  # noqa: C901, PLR0913
    plan: Plan,
    options: PromptOptions,
    sport_titles: dict[str, str],
    indexed: set[str],
    outcomes: dict[tuple[str, str], Outcome],
    pending: dict[str, dict[str, str]],
    *,
    execute: bool,
    max_calls: int | None,
    sleep: float,
    predict_factory: Callable[[], Predictor],
    now: Callable[[], datetime],
) -> None:
    """Predict and upload file by file; collect index entries in ``pending``."""
    predict: Predictor | None = None
    calls = 0
    stopped = False
    for (run_ts, provider), tasks in plan.by_file().items():
        base_key = options.base_key(provider)
        pathname = blob_archive.backfill_predictions_pathname(run_ts, base_key)
        outcome = Outcome("planned", rows=len(tasks), pathname=pathname)
        outcomes[run_ts, provider] = outcome
        key = options.provider_key(provider)
        if pathname in indexed:
            outcome.status = "archived"
            continue
        if not execute:
            continue
        if stopped:
            outcome.status = "skipped"
            continue
        exists = exists_in_blob(pathname)
        if exists:
            pending.setdefault(run_ts, {})[key] = pathname
            outcome.status = "indexed"
            continue
        if exists is None:
            outcome.status = "skipped"
            continue
        if (max_calls is not None and calls + len(tasks) > max_calls) or near_cron(
            now()
        ):
            logger.warning("Stopping before %s / %s (limit or cron)", run_ts, provider)
            stopped = True
            outcome.status = "skipped"
            continue
        if predict is None:
            predict = predict_factory()
        if provider in predict.abandoned:
            outcome.status = "skipped"
            continue
        calls += len(tasks)
        prediction_data, odds_sources, failed = predict_file(
            tasks, provider, sport_titles, options, predict
        )
        outcome.failed = failed
        outcome.predicted = len(tasks) - len(failed)
        if provider in predict.abandoned:
            # Outage mid-file: files are write-once, so a partial file could
            # never be completed; drop it and let a re-run redo the whole file
            outcome.status = "failed"
            logger.warning("%s abandoned during %s, file not written", provider, run_ts)
        elif not prediction_data:
            outcome.status = "failed"
            logger.warning("No valid prediction for %s / %s", run_ts, provider)
        else:
            payload = blob_archive.build_backfill_payload(
                prediction_data,
                blob_archive.format_payload_ts(parse_ts(run_ts)),
                odds_sources=odds_sources,
                generated_at=blob_archive.format_payload_ts(now()),
                script=SCRIPT_NAME,
            )
            written = blob_archive.put_with_retry(
                pathname,
                blob_archive.to_jsonl(payload),
                content_type=blob_archive.JSONL_CONTENT_TYPE,
            )
            if written:
                pending.setdefault(run_ts, {})[key] = pathname
                outcome.status = "written"
            else:
                outcome.status = "failed"
        if sleep > 0:
            time.sleep(sleep)


# %% --------------------------------------------
# * Report


def short_league(league: str) -> str:
    """Return a short league label for tables."""
    return league.split(" - ")[0].replace("UEFA ", "")


def print_report(
    plan: Plan,
    outcomes: dict[tuple[str, str], Outcome],
    providers: list[str],
    *,
    execute: bool,
    odds_label: str,
    llm_attempts: int,
) -> None:
    """Print the tasks per run and provider, LLM calls and what is left out."""
    title = (
        "Backfill finished" if execute else "DRY RUN - no LLM calls, nothing written"
    )
    print(f"\n=== {title} ===")
    for provider in providers:
        live = plan.first_live.get(provider) or "none"
        print(f"{provider}: first live run {live}")
    print(f"Odds: run odds files, else {odds_label}")

    leagues = sorted({t.offer.league for t in plan.tasks})
    header = "".join(f"{short_league(lg):>18}" for lg in leagues)
    print(f"\n{'run_ts':<21}{header}" + "".join(f"{p:>28}" for p in providers))
    runs = sorted({t.offer.run_ts for t in plan.tasks})
    for run_ts in runs:
        run_tasks = [t for t in plan.tasks if t.offer.run_ts == run_ts]
        fixtures = {
            (t.offer.league, t.offer.home_team, t.offer.away_team) for t in run_tasks
        }
        counts = Counter(league for league, _, _ in fixtures)
        cells = "".join(f"{counts.get(lg, 0):>18}" for lg in leagues)
        statuses = ""
        for provider in providers:
            outcome = outcomes.get((run_ts, provider))
            if outcome is None:
                statuses += f"{'no odds':>28}"
                continue
            text = f"{outcome.rows} {outcome.status}"
            if outcome.status in {"written", "failed", "unindexed"} and execute:
                text = f"{outcome.predicted}/{outcome.rows} {outcome.status}"
            statuses += f"{text:>28}"
        print(f"{run_ts:<21}{cells}{statuses}")

    print("\nFixtures per league and provider (unique):")
    for provider in providers:
        tasks = [t for t in plan.tasks if t.provider == provider]
        per_league = Counter(t.offer.league for t in tasks)
        parts = ", ".join(f"{short_league(lg)} {per_league[lg]}" for lg in leagues)
        print(f"  {provider}: {len(tasks)} ({parts})")

    print("\nLLM calls per provider (pending files only):")
    for provider in providers:
        pending = [
            o
            for (_, prov), o in outcomes.items()
            if prov == provider and o.status in {"planned", "skipped"}
        ]
        rows = sum(o.rows for o in pending)
        sources = Counter(
            t.odds.source
            for t in plan.tasks
            if t.provider == provider and t.odds is not None
        )
        print(
            f"  {provider}: {rows} fixtures in {len(pending)} files -> "
            f"{rows} to {rows * llm_attempts} requests (retries); odds "
            + ", ".join(f"{k} {v}" for k, v in sorted(sources.items()))
        )

    no_odds = [t for t in plan.tasks if t.odds is None]
    print(f"\nWithout odds, not predicted: {len(no_odds)}")
    for task in sorted(no_odds, key=lambda t: (t.offer.kickoff, t.provider)):
        print(
            f"  {task.offer.kickoff:%Y-%m-%d %H:%M} UTC  {task.offer.league:<26} "
            f"{task.offer.home_team} - {task.offer.away_team}  ({task.provider})"
        )
    for provider in providers:
        if plan.missed_live[provider] or plan.no_offer[provider]:
            print(
                f"{provider}: {plan.missed_live[provider]} fixtures missed live "
                f"(out of scope), {plan.no_offer[provider]} never offered before "
                "kickoff"
            )

    failed = [t for o in outcomes.values() for t in o.failed]
    if execute:
        print(f"\nFailed (no valid prediction): {len(failed)}")
        for task in sorted(failed, key=lambda t: (t.offer.kickoff, t.provider)):
            print(
                f"  {task.offer.run_ts}  {task.offer.league:<26} "
                f"{task.offer.home_team} - {task.offer.away_team}  ({task.provider})"
            )
        for (run_ts, provider), o in sorted(outcomes.items()):
            if o.status in {"failed", "skipped", "unindexed"} and not o.failed:
                print(f"  {run_ts} {provider}: {o.status} ({o.rows} fixtures)")


# %% --------------------------------------------
# * Main


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--env-file", type=Path, default=REPO_ROOT / ".env.local")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="count fixtures and LLM calls, call and write nothing (default)",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="call the LLMs (real API spend) and write to Blob",
    )
    parser.add_argument(
        "--providers",
        nargs="+",
        choices=PROVIDERS,
        default=PROVIDERS,
        help="providers to backfill (default: both)",
    )
    parser.add_argument("--season", default=DEFAULT_SEASON)
    parser.add_argument(
        "--odds-csv",
        type=Path,
        help="local reconstructed odds CSV instead of the newest archived one",
    )
    parser.add_argument(
        "--max-calls",
        type=int,
        help="stop before the first file that would exceed this many predicted "
        "fixtures (each takes 1 to 4 requests)",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=DEFAULT_SLEEP,
        help="seconds between files, on top of the providers' rate limits",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "tip-genius-backfill-cache",
        help="cache for downloaded (write-once) archive files",
    )
    return parser.parse_args()


def main() -> None:
    """Run the backfill (dry-run unless --execute)."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # One INFO line per downloaded archive file is noise
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = parse_args()
    load_dotenv(args.env_file)
    if not os.environ.get("BLOB_READ_WRITE_TOKEN"):
        sys.exit("BLOB_READ_WRITE_TOKEN is not set (check --env-file)")

    options = load_prompt_options(CFG_DIR / "tip_genius_config.yaml")
    sport_titles = load_sport_titles(CFG_DIR / "api_config.yaml")
    index = blob_archive.read_index(fresh=True)
    runs = index.get("runs", [])
    reader = CachedReader(args.cache_dir)

    if args.odds_csv:
        csv_text = args.odds_csv.read_text(encoding="utf-8")
        odds_label = str(args.odds_csv)
    elif archived := newest_reconstructed(index):
        csv_text = reader.fetch_bytes(archived).decode("utf-8")
        odds_label = archived
    else:
        sys.exit("No reconstructed odds CSV in index.json; pass --odds-csv")

    since = season_start(args.season) - RUN_LOOKBACK
    offers = load_offers(runs, options, reader.fetch_jsonl, since)
    fixtures = group_fixtures(offers)
    first_live = first_live_runs(runs, args.providers, options)
    lookup = OddsLookup(runs, reader.fetch_jsonl, csv_text)
    plan = plan_tasks(fixtures, args.providers, first_live, args.season, lookup)
    missing_titles = {t.offer.league for t in plan.tasks} - set(sport_titles)
    if missing_titles:
        sys.exit(f"No sport_titles entry in api_config.yaml for {missing_titles}")
    if args.execute and near_cron(datetime.now(tz=UTC)):
        sys.exit("Not running --execute on Tue/Fri 02:00-17:00 UTC (cron window)")

    outcomes = execute_plan(
        plan,
        index,
        options,
        sport_titles,
        execute=args.execute,
        max_calls=args.max_calls,
        sleep=args.sleep if args.execute else 0.0,
        predict_factory=lambda: WorkflowPredictor(options.prediction_type),
    )
    print_report(
        plan,
        outcomes,
        args.providers,
        execute=args.execute,
        odds_label=odds_label,
        llm_attempts=TipGenius.llm_attempts,
    )


if __name__ == "__main__":
    main()
