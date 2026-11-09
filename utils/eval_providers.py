"""Backtest LLM provider predictions against actual match results.

Scores every provider's past predictions with Kicktipp four-points scoring
(4 exact result, 3 correct goal difference, 2 correct tendency, 0 otherwise;
a draw with the wrong score is a tendency hit worth 2), using ``lib/stats.py``,
the same code that computes the dashboard statistics.

Reads only Vercel Blob (``BLOB_READ_WRITE_TOKEN``): the prediction tables
(``stats/predictions_<season>.csv``, one row per fixture and provider with the
last prediction before kickoff and the odds) and the results CSVs
(``results/``), joined on ``match_key``. Downloads are cached on disk and
re-used while a HEAD shows the same etag (``--offline`` skips the HEADs).
Nothing is written to Blob, KV or the repository.

The main tables are the live ranking (predictions made before kickoff by the
workflow). ``--include-backfill`` adds "incl. backfill" tables with the
predictions ``utils/backfill_llm_predictions.py`` generated after the fact for
late-joining providers. Baseline rows (``Odds-Favourite``, ``Always-2:1``,
``Consensus``) appear in every table. A reliability table shows how many
offered matches each provider actually predicted.

Usage::

    uv run python utils/eval_providers.py --env-file /path/to/.env.local
    uv run python utils/eval_providers.py --league Bundesliga --season 2026/27
    uv run python utils/eval_providers.py --include-backfill --offline
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
import sys
import tempfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parents[1] / "src" / "tip_genius"))
from lib import blob_archive, dashboard, prediction_table, results_archive  # noqa: E402
from lib import stats as st  # noqa: E402
from lib.results_archive import BUNDESLIGA  # noqa: E402

logger = logging.getLogger(__name__)

# %% --------------------------------------------
# * Constants

REPO_ROOT = Path(__file__).parents[1]
CONFIG_PATH = REPO_ROOT / "src" / "tip_genius" / "cfg" / "tip_genius_config.yaml"
DEFAULT_CACHE_DIR = Path(tempfile.gettempdir()) / "tip-genius-eval-cache"


# %% --------------------------------------------
# * Blob reads with an etag cache


class EtagCache:
    """Disk cache for files that are overwritten in place (index, CSVs).

    Each read sends one HEAD (simple operation) and downloads the file only
    if its etag changed; ``offline`` uses the cached copy without a HEAD.
    """

    def __init__(self, directory: Path, *, offline: bool = False) -> None:
        """Create the cache in ``directory``."""
        self.directory = directory
        self.offline = offline
        self.heads = 0
        self.downloads = 0

    def read(self, pathname: str) -> bytes | None:
        """Return the file's content; None if it does not exist."""
        body_path = self.directory / pathname
        etag_path = body_path.with_name(body_path.name + ".etag")
        if self.offline:
            return body_path.read_bytes() if body_path.is_file() else None
        self.heads += 1
        try:
            head = blob_archive.vercel_blob.head_blob(pathname)
        except blob_archive.vercel_blob.BlobNotFoundError:
            return None
        if (
            body_path.is_file()
            and etag_path.is_file()
            and etag_path.read_text() == head.etag
        ):
            return body_path.read_bytes()
        self.downloads += 1
        target = f"{head.url}?etag={quote(head.etag, safe='')}"
        body = blob_archive.vercel_blob.get_blob(target, access="private").body
        body_path.parent.mkdir(parents=True, exist_ok=True)
        body_path.write_bytes(body)
        etag_path.write_text(head.etag)
        return body


def active_models() -> list[str]:
    """Return the providers the workflow runs (``llm_provider_options``)."""
    with CONFIG_PATH.open(encoding="utf-8") as handle:
        return list(yaml.safe_load(handle)["llm_provider_options"])


def load_archive(
    cache: EtagCache, seasons: list[str]
) -> tuple[list[st.TableTip], dict[str, tuple[int, int]]]:
    """Read the prediction tables and results CSVs of ``seasons``."""
    raw_index = cache.read(blob_archive.INDEX_PATHNAME)
    if raw_index is None:
        sys.exit("index.json not found (or not cached with --offline)")
    index = json.loads(raw_index)
    rows: list[dict[str, str]] = []
    result_rows: list[dict[str, str]] = []
    for season in seasons:
        pathname = prediction_table.table_pathname(season)
        body = cache.read(pathname) if pathname in index.get("stats", {}) else None
        if body is None:
            logger.warning(
                "No prediction table %s (run utils/backfill_stats.py)", pathname
            )
        else:
            rows.extend(prediction_table.rows_from_csv(body))
        prefix = f"{results_archive.RESULTS_PREFIX}{season.replace('/', '-')}_"
        for path in sorted(index.get(blob_archive.RESULTS_KEY, {})):
            if path.startswith(prefix) and (body := cache.read(path)) is not None:
                result_rows.extend(results_archive.rows_from_csv(body))
    logger.info(
        "Loaded %d table rows and %d results (%d HEADs, %d downloads)",
        len(rows),
        len(result_rows),
        cache.heads,
        cache.downloads,
    )
    return st.parse_table(rows), st.parse_results(result_rows)


# %% --------------------------------------------
# * Reporting


def print_table(title: str, scores: dict[str, st.Score], active: list[str]) -> None:
    """Print one ranking table, ordered by points per offered match."""
    ranked = st.ranked(scores)
    print(f"\n=== {title} ===")
    if not ranked:
        print("(no scored matches)")
        return
    print(
        f"{'#':>2} {'Provider':<30} {'Offered':>7} {'Missed':>6} {'Points':>6} "
        f"{'Avg':>5} {'Avg0':>5} {'±SE':>5} {'MDs':>4} {'Pts/MD':>6} {'2:1/MD':>6} "
        f"{'Exact':>6} {'GoalDiff':>8} {'Tendency':>8}"
    )
    for rank, (provider, s) in enumerate(ranked, 1):
        marker = "*" if provider in active else " "
        tipped = max(s.tipped, 1)
        matchdays = max(len(s.matchdays), 1)
        print(
            f"{rank:>2} {provider + marker:<30} {s.offered:>7} "
            f"{s.missed:>6} {s.points:>6} "
            f"{s.avg:>5.2f} {s.avg0:>5.2f} "
            f"{s.std_error:>5.2f} {len(s.matchdays):>4} "
            f"{s.ppmd:>6.2f} "
            f"{s.two_one / matchdays:>6.2f} {s.exact / tipped:>6.1%} "
            f"{(s.exact + s.diff) / tipped:>8.1%} "
            f"{(s.exact + s.diff + s.tendency) / tipped:>8.1%}"
        )


def print_head_to_head(
    entries: list[st.Entry], label: str, active: list[str], late: list[str]
) -> None:
    """Rank active providers on fixtures offered to every one of them.

    Missing tips score 0. Also ranks the active providers without the late
    joiners on a larger sample. Baselines are scored on the same fixtures.
    """
    present = {e.model for e in entries}
    group = {m for m in active if m in present}
    groups = [("all current providers", group)]
    if late and (established := group - set(late)) != group:
        groups.append((f"current providers excl. {'/'.join(late)}", established))
    for group_label, members in groups:
        common = st.common_entries(entries, members)
        fixtures = len({e.fixture.match_key for e in common})
        print_table(
            f"{label}, head-to-head on {fixtures} common fixtures, {group_label}",
            st.aggregate(common),
            active,
        )


def in_season(entries: list[st.Entry], season: str) -> list[st.Entry]:
    """Keep entries of one season."""
    return [e for e in entries if e.fixture.season == season]


def print_report(
    entries: list[st.Entry],
    season: str,
    from_season: str,
    active: list[str],
    late: list[str],
) -> None:
    """Print overall, per-league, per-season and head-to-head tables."""
    print("\n* = currently run by the workflow")
    print(
        "Offered = finished matches while the provider was active, "
        "Missed = offered but no tip before kickoff (scores 0), "
        "Avg = points per tipped match, Avg0 = points per offered match "
        "(ranking key), ±SE = standard error of Avg0, MDs = matchdays, "
        "Pts/MD = points per matchday, 2:1/MD = 2:1 tips per matchday; "
        "Exact = 4 pts, GoalDiff = 3+ pts, "
        "Tendency = 2+ pts, as share of tipped matches"
    )
    print_table(f"All leagues, since {from_season}", st.aggregate(entries), active)
    for league in sorted({e.fixture.league for e in entries}):
        league_entries = [e for e in entries if e.fixture.league == league]
        print_table(
            f"{league}, since {from_season}", st.aggregate(league_entries), active
        )
    for one_season in sorted({e.fixture.season for e in entries}):
        print_table(
            f"Season {one_season}, all leagues",
            st.aggregate(in_season(entries, one_season)),
            active,
        )
    season_entries = in_season(entries, season)
    bundesliga = [e for e in season_entries if e.fixture.league == BUNDESLIGA]
    print_table(f"Season {season}, {BUNDESLIGA}", st.aggregate(bundesliga), active)
    print_head_to_head(bundesliga, f"Season {season}, {BUNDESLIGA}", active, late)
    print_head_to_head(season_entries, f"Season {season}, all leagues", active, late)


def print_backfill_report(
    entries: list[st.Entry], season: str, active: list[str], late: list[str]
) -> None:
    """Print the "incl. backfill" tables, separate from the live ranking."""
    providers = sorted({e.model for e in entries if e.origin == "backfill"})
    names = ", ".join(providers) or "none"
    print(
        "\n##### incl. backfill: live predictions plus predictions generated "
        f"after the fact for past runs ({names}); not a live result #####"
    )
    season_entries = in_season(entries, season)
    label = f"Season {season}, all leagues (incl. backfill)"
    print_table(label, st.aggregate(season_entries), active)
    for league in sorted({e.fixture.league for e in season_entries}):
        league_entries = [e for e in season_entries if e.fixture.league == league]
        print_table(
            f"Season {season}, {league} (incl. backfill)",
            st.aggregate(league_entries),
            active,
        )
    print_head_to_head(season_entries, label, active, late)


def print_reliability(
    title: str, coverage: dict[str, st.Coverage], active: list[str]
) -> None:
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
        marker = "*" if provider in active else " "
        per_run = c.predicted / c.offered if c.offered else 0.0
        pre_ko = c.past_predicted / c.past_fixtures if c.past_fixtures else 0.0
        print(
            f"{provider + marker:<30} {c.runs:>5} {c.missed_runs:>6} "
            f"{c.offered:>7} {c.predicted:>7} {per_run:>6.1%} "
            f"{c.past_fixtures:>6} {c.past_predicted:>7} {pre_ko:>6.1%}"
        )


def print_reliability_report(
    tips: list[st.TableTip], season: str, from_season: str, active: list[str]
) -> None:
    """Print reliability since the first season and for the focused season."""
    print(
        "\nReliability (live predictions): Runs = workflow runs while the "
        "provider was active, Missed = runs without any tip, PerRun = share of "
        "matches listed in those runs that got a tip, PreKO = share of past "
        "matches with a tip before kickoff"
    )
    now = datetime.now(tz=UTC)
    print_reliability(f"since {from_season}", st.reliability(tips, now), active)
    season_tips = [t for t in tips if t.season == season]
    print_reliability(
        f"season {season}",
        st.reliability(season_tips, now, windows_from=tips),
        active,
    )


def print_coverage(
    tips: list[st.TableTip], results: dict[str, tuple[int, int]], *, show: bool
) -> None:
    """Print how many past fixtures joined to a result."""
    cutoff = datetime.now(tz=UTC) - results_archive.RESULT_DELAY
    past: dict[str, st.TableTip] = {}
    for tip in tips:
        if tip.kickoff <= cutoff:
            past.setdefault(tip.match_key, tip)
    missing = sorted(k for k in past if k not in results)
    print(
        f"\nPast fixtures with predictions: {len(past)}, "
        f"with a result: {len(past) - len(missing)}, without: {len(missing)}"
    )
    if show:
        for key in missing:
            print(f"  no result: {key}")


# %% --------------------------------------------
# * Main


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--env-file",
        type=Path,
        default=REPO_ROOT / ".env.local",
        help="dotenv file with BLOB_READ_WRITE_TOKEN",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help="where to cache Blob downloads",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="use the cached files only (no Blob requests)",
    )
    parser.add_argument("--league", help="only leagues containing this text")
    parser.add_argument(
        "--season",
        default=results_archive.season_of(datetime.now(tz=UTC)),
        help="season for the focused ranking, e.g. 2026/27",
    )
    parser.add_argument(
        "--from-season",
        default=st.FROM_SEASON,
        help="first season to include (default %(default)s)",
    )
    parser.add_argument(
        "--include-backfill",
        action="store_true",
        help="also print 'incl. backfill' tables with the backfilled predictions",
    )
    parser.add_argument(
        "--show-unmatched",
        action="store_true",
        help="list past fixtures without a result",
    )
    return parser.parse_args()


def main() -> None:
    """Run the backtest and print the report."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for name in ("httpx", "httpx2"):
        logging.getLogger(name).setLevel(logging.WARNING)
    args = parse_args()
    load_dotenv(args.env_file)
    cache = EtagCache(args.cache_dir / "blob", offline=args.offline)
    now = datetime.now(tz=UTC)
    tips, results = load_archive(cache, dashboard.seasons_since(args.from_season, now))
    if args.league:
        wanted = args.league.lower()
        tips = [t for t in tips if wanted in t.league.lower()]
    active = active_models()
    late = st.late_joiners(tips, active)
    print_coverage(tips, results, show=args.show_unmatched)

    live = st.score_entries(tips, results, include_backfill=False)
    print_report(live, args.season, args.from_season, active, late)
    print_reliability_report(tips, args.season, args.from_season, active)
    backfill_tips = defaultdict(int)
    for tip in tips:
        if tip.origin == "backfill":
            backfill_tips[tip.provider] += 1
    if args.include_backfill:
        print_backfill_report(
            st.score_entries(tips, results), args.season, active, late
        )
    elif backfill_tips:
        counts = ", ".join(f"{p} {n}" for p, n in sorted(backfill_tips.items()))
        print(f"\nBackfill tips not shown ({counts}): pass --include-backfill")


if __name__ == "__main__":
    main()
