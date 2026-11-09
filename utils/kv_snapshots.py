"""Legacy loaders for prediction snapshots in Vercel KV and local files.

Used by ``utils/reconstruct_odds.py`` (a one-off for odds before they were
archived). ``utils/eval_providers.py`` reads the Blob prediction table instead
(``lib/prediction_table.py``). Reads KV with the read-only token; writes
nothing.
"""

# * Author(s): Thomas Glanzer
# * Creation : Oct 2026
# * License: MIT license

# %% --------------------------------------------
# * Libraries

from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parents[1] / "src" / "tip_genius"))
from lib import results_archive  # noqa: E402
from lib.results_archive import (  # noqa: E402
    BERLIN,
    NAME_STOPWORDS,
    HttpCache,
    Result,
    normalize_team,
    season_of,
)

# Shared with the results archive; re-exported for existing callers
__all__ = ["NAME_STOPWORDS", "HttpCache", "normalize_team", "season_of"]

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# %% --------------------------------------------
# * Constants

REPO_ROOT = Path(__file__).parents[1]
KEY_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})_Match_Predictions_(.+)"
    r"_FourPointsScoring_named_with-info(?:\.jsonl)?$"
)
# 2024/25 is incomplete (no reasoning texts, partial history) and not needed
DEFAULT_FROM_SEASON = "2025/26"
RUN_GAP = timedelta(minutes=30)


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
    reasoning: str = ""


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
                        match.get("reasoning") or "",
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


def as_fixture(pred: Prediction) -> results_archive.Fixture:
    """Return the archive fixture of a prediction."""
    return results_archive.Fixture(pred.league, pred.kickoff, pred.home, pred.away)


def load_results(cache: HttpCache, predictions: list[Prediction]) -> list[Result]:
    """Fetch results for every league and day that has predictions."""
    return results_archive.fetch_results(cache, map(as_fixture, predictions))


def match_results(
    predictions: list[Prediction], results: list[Result]
) -> dict[tuple, Result]:
    """Map each fixture (league, home, away, kickoff) to its actual result."""
    matched = results_archive.match_results(map(as_fixture, predictions), results)
    return {
        (fixture.league, fixture.home, fixture.away, fixture.kickoff): result
        for fixture, (result, _) in matched.items()
    }


# %% --------------------------------------------
# * Runs


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
