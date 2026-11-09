"""Build the Blob prediction tables for the dashboard statistics.

Creates (or brings up to date) ``stats/predictions_<season>.csv`` (see
``lib/prediction_table.py``) for 2025/26 up to the current season from the
archive's prediction and odds files and the reconstructed odds CSV, and
computes the statistics JSON the workflow writes to KV (``lib/stats.py``),
so the numbers can be checked before anything is written.

``--dry-run`` (default) reads ``index.json``, every prediction and odds file of
the seasons, the reconstructed odds CSV and the results CSVs, and reports the
rows per table, odds and result coverage, the JSON size and the leaderboards;
it writes nothing (``--save-dir`` keeps local copies). ``--execute`` writes the
tables (one put each, read back) and updates ``index.json`` once (``stats`` row
counts and the ``stats_scan`` state, so the workflow continues incrementally).
It never writes KV: the next workflow run does. ``--rebuild`` ignores the
existing tables (refused if one would shrink, unless ``--force``). Writes are
refused on Tue/Fri 02:00-17:00 UTC (cron). Downloads of write-once files are
cached on disk, so re-runs cost only the overwritten files (index, tables,
results).

Usage::

    uv run python utils/backfill_stats.py                    # dry-run
    uv run python utils/backfill_stats.py --save-dir /tmp/stats-review
    uv run python utils/backfill_stats.py --execute
"""

# * Author(s): Thomas Glanzer
# * Creation : Nov 2026
# * License: MIT license

# %% --------------------------------------------
# * Libraries

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parents[1] / "src" / "tip_genius"))

import yaml  # noqa: E402
from lib import blob_archive, dashboard, prediction_table, results_archive  # noqa: E402
from lib import stats as st  # noqa: E402

# %% --------------------------------------------
# * Constants

REPO_ROOT = Path(__file__).parents[1]
CONFIG_PATH = REPO_ROOT / "src" / "tip_genius" / "cfg" / "tip_genius_config.yaml"
DEFAULT_CACHE_DIR = Path(tempfile.gettempdir()) / "tip-genius-stats-cache"


# %% --------------------------------------------
# * Report


def coverage_lines(
    rows: list[dict[str, str]], results: dict[str, tuple[int, int]], now: datetime
) -> list[str]:
    """Describe one table: fixtures, providers, odds sources, result joins."""
    fixtures: dict[str, dict[str, str]] = {}
    providers: Counter[str] = Counter()
    origins: Counter[str] = Counter()
    for row in rows:
        fixtures.setdefault(row["match_key"], row)
        providers[row["provider"]] += 1
        origins[row["origin"]] += 1
    odds = Counter(row["odds_source"] or "none" for row in fixtures.values())
    cutoff = now - results_archive.RESULT_DELAY
    past = [
        key
        for key, row in fixtures.items()
        if prediction_table.parse_key_time(row["kickoff_utc"]) <= cutoff
    ]
    joined = sum(key in results for key in past)
    lines = [
        f"  fixtures {len(fixtures)}, rows {len(rows)} "
        f"({', '.join(f'{k} {v}' for k, v in sorted(origins.items()))})",
        f"  odds per fixture: {', '.join(f'{k} {v}' for k, v in sorted(odds.items()))}",
        f"  past fixtures {len(past)}, joined to a result {joined}, "
        f"without {len(past) - joined}",
        "  rows per provider: "
        + ", ".join(f"{p} {n}" for p, n in sorted(providers.items())),
    ]
    no_odds = [row for row in fixtures.values() if not row["odds_source"]]
    lines.extend(
        f"    no odds: {row['kickoff_utc']} {row['league']}: "
        f"{row['home_team']} - {row['away_team']}"
        for row in no_odds[:20]
    )
    if len(no_odds) > 20:  # noqa: PLR2004
        lines.append(f"    ... and {len(no_odds) - 20} more")
    return lines


def print_leaderboard(data: dict[str, Any], key: str, title: str) -> None:
    """Print one slice's leaderboard and fair comparison from the JSON."""
    part = data["slices"].get(key)
    if part is None:
        return
    names = [model["id"] for model in data["models"]]
    columns = data["meta"]["columns"]["leaderboard"]
    print(f"\n--- {title} ({key}): {part['fixtures']} fixtures ---")
    print(
        f"{'#':>2} {'Model':<28} {'Pts':>5} {'Off':>5} {'Miss':>4} {'Avg0':>6} "
        f"{'SE':>5} {'Pts/MD':>6} {'BF':>4}"
    )
    for raw in part["leaderboard"]:
        row = dict(zip(columns, raw, strict=True))
        print(
            f"{row['rank']:>2} {names[row['model']]:<28} {row['points']:>5} "
            f"{row['offered']:>5} {row['missed']:>4} {row['avg0']:>6.3f} "
            f"{row['se']:>5.3f} {row['ppmd']:>6.2f} {row['backfill']:>4}"
        )
    fair_columns = data["meta"]["columns"]["fair"]
    for fair in part["fair"]:
        print(f"  fair ({fair['group']}): {fair['fixtures']} common fixtures")
        for raw in fair["rows"]:
            row = dict(zip(fair_columns, raw, strict=True))
            print(
                f"    {row['rank']:>2} {names[row['model']]:<28} {row['points']:>5} "
                f"{row['avg0']:>6.3f}"
            )


def print_report(
    inputs: dashboard.DashboardInputs,
    data: dict[str, Any],
    body: bytes,
    reader: CountingReader,
    *,
    execute: bool,
    now: datetime,
) -> None:
    """Print table status, coverage, Blob operations and the leaderboards."""
    mode = "EXECUTE" if execute else "DRY RUN - nothing written"
    print(f"\n=== Prediction tables ({mode}) ===")
    results = st.parse_results(inputs.all_result_rows())
    for season, report in sorted(inputs.tables.items()):
        print(
            f"{season} {report.pathname}: {report.mode} ({report.reason}), "
            f"read {report.files_read} prediction / {report.odds_read} odds files"
            f"{' + reconstructed CSV' if report.reconstructed_read else ''}, "
            f"{report.existing} -> {report.rows} rows, {report.status}"
        )
        for line in coverage_lines(report.contents or [], results, now):
            print(line)
        for path in report.unread:
            print(f"  UNREAD: {path}")
    writes = sum(r.status in {"dry-run", "written"} for r in inputs.tables.values())
    print(
        f"\nBlob operations: {reader.downloads} downloads of write-once files "
        f"(cached on disk, {reader.cache_hits} from the cache), ~{inputs.reads} "
        f"simple operations for index/tables/results; puts: {writes} table(s) "
        f"+ 1 index (each read back: +2 simple operations)"
    )
    print(f"Statistics JSON: {len(body):,} bytes, {len(data['slices'])} slices")
    current = results_archive.season_of(now)
    print_leaderboard(data, f"{current}|all", f"Season {current}, all leagues")
    print_leaderboard(data, "all|all", "All seasons, all leagues")


# %% --------------------------------------------
# * Main


class CountingReader(blob_archive.CachedArchiveReader):
    """``CachedArchiveReader`` that counts downloads and cache hits."""

    def __init__(self, cache_dir: Path | None) -> None:
        """Initialize the counters."""
        super().__init__(cache_dir)
        self.downloads = 0
        self.cache_hits = 0

    def fetch_bytes(self, pathname: str) -> bytes:
        """Count, then download or read from the cache."""
        cached = self.cache_dir / pathname if self.cache_dir else None
        if cached is not None and cached.is_file():
            self.cache_hits += 1
        else:
            self.downloads += 1
        return super().fetch_bytes(pathname)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--env-file", type=Path, default=REPO_ROOT / ".env.local")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="report tables, coverage and statistics, write nothing (default)",
    )
    mode.add_argument(
        "--execute", action="store_true", help="write the tables and index.json"
    )
    parser.add_argument(
        "--seasons",
        nargs="+",
        help=f"seasons like 2026/27 (default: {st.FROM_SEASON} to the current one)",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="ignore the existing tables and rebuild them from every file",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="let --rebuild write tables with fewer rows than index.json lists",
    )
    parser.add_argument(
        "--save-dir",
        type=Path,
        metavar="DIR",
        help="also write the tables, results CSVs and the statistics JSON to DIR",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help="disk cache for prediction and odds files",
    )
    return parser.parse_args()


def main() -> None:
    """Run the backfill (dry-run unless --execute)."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for name in ("httpx", "httpx2"):
        logging.getLogger(name).setLevel(logging.WARNING)
    args = parse_args()
    load_dotenv(args.env_file)
    if not os.environ.get("BLOB_READ_WRITE_TOKEN"):
        sys.exit("BLOB_READ_WRITE_TOKEN is not set (check --env-file)")
    now = datetime.now(tz=UTC)
    if args.execute and blob_archive.near_cron(now):
        sys.exit("Not writing on Tue/Fri 02:00-17:00 UTC (cron window)")
    seasons = args.seasons or dashboard.seasons_since(st.FROM_SEASON, now)
    with CONFIG_PATH.open(encoding="utf-8") as handle:
        active = list(yaml.safe_load(handle)["llm_provider_options"])

    index = blob_archive.read_index(fresh=True)
    reader = CountingReader(args.cache_dir / "blob")
    inputs = dashboard.collect_inputs(
        index,
        now=now,
        seasons=sorted(seasons),
        update=set(seasons),
        fetch_jsonl=reader.fetch_jsonl,
        fetch_bytes=reader.fetch_bytes,
        rebuild=args.rebuild,
        force=args.force,
        dry_run=not args.execute,
    )
    data, body = dashboard.build_json(inputs, active=active, now=now)
    print_report(inputs, data, body, reader, execute=args.execute, now=now)

    if args.save_dir:
        for season, rows in inputs.table_rows.items():
            if rows is None:
                continue
            target = args.save_dir / prediction_table.table_pathname(season)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(prediction_table.rows_to_csv(rows))
        for pathname, rows in inputs.result_rows.items():
            target = args.save_dir / pathname
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(results_archive.rows_to_csv(rows))
        (args.save_dir / "dashboard_stats.json").write_bytes(body)
        (args.save_dir / "dashboard_stats.pretty.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=1)
        )
        print(f"\nSaved tables and JSON under {args.save_dir}")

    ok = not inputs.unread and all(r.ok for r in inputs.tables.values())
    if args.execute:
        entries = {
            r.pathname: r.index_entry for r in inputs.tables.values() if r.index_entry
        }
        states: dict[str, dict[str, Any] | None] = {}
        for season, report in inputs.tables.items():
            if report.scan_state:
                states[season] = report.scan_state
        if entries or states:
            updated = blob_archive.update_stats_index(
                entries, expected=index, scan=dict(states) or None
            )
            print(f"\nIndex updated: {updated}")
            ok = ok and updated
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
