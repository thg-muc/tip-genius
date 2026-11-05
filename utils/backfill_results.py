"""Backfill final match results into the Vercel Blob results archive.

Fills ``results/<season>_<League-Key>.csv`` (see ``lib/results_archive.py``)
for every league with predictions in the Blob, for the current season and the
two before it. Uses the same gap detection, never-shrink guard and read-back
verification as the workflow's results step, so it is idempotent: a re-run
only adds missing rows.

``--dry-run`` (default) reads ``index.json``, the prediction files and the
result sources and reports the rows per file and the unmatched fixtures; it
writes nothing. ``--execute`` writes the CSVs (one put per changed file) and
updates ``index.json`` once. ``--rebuild SEASON`` rebuilds one season's files
from the sources instead of adding to them (refused if a file would shrink,
unless ``--force``). Writes are refused on Tue/Fri 02:00-17:00 UTC (cron).

Usage::

    uv run python utils/backfill_results.py                  # dry-run
    uv run python utils/backfill_results.py --execute
    uv run python utils/backfill_results.py --rebuild 2025/26 --execute
"""

# * Author(s): Thomas Glanzer
# * Creation : Nov 2026
# * License: MIT license

# %% --------------------------------------------
# * Libraries

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parents[1] / "src" / "tip_genius"))

from lib import blob_archive, results_archive  # noqa: E402

# %% --------------------------------------------
# * Constants

REPO_ROOT = Path(__file__).parents[1]
DEFAULT_CACHE_DIR = Path(tempfile.gettempdir()) / "tip-genius-results-cache"
PAST_SEASONS = 2
# Workflow window (Tue/Fri, UTC): runs start 03:00, manual reruns until ~16:00
CRON_WEEKDAYS = {1, 4}
CRON_WINDOW_START = 2
CRON_WINDOW_END = 17


# %% --------------------------------------------
# * Helpers


def near_cron(moment: datetime) -> bool:
    """Whether ``moment`` falls into the workflow window (Tue/Fri, UTC)."""
    utc = moment.astimezone(UTC)
    return (
        utc.weekday() in CRON_WEEKDAYS
        and CRON_WINDOW_START <= utc.hour < CRON_WINDOW_END
    )


def default_seasons(now: datetime) -> list[str]:
    """Return the current season and the ``PAST_SEASONS`` before it."""
    start = int(results_archive.season_of(now)[:4])
    return [
        f"{year}/{(year + 1) % 100:02d}"
        for year in range(start - PAST_SEASONS, start + 1)
    ]


class CachedReader(blob_archive.BlobArchiveReader):
    """``BlobArchiveReader`` with a disk cache (prediction files are write-once)."""

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
            tmp = cached.with_name(f"{cached.name}.tmp")
            tmp.write_bytes(body)
            tmp.replace(cached)
        return body


# %% --------------------------------------------
# * Report


def print_report(
    report: results_archive.ArchiveReport, *, execute: bool, rebuild: bool
) -> None:
    """Print rows per file, unmatched fixtures and the index outcome."""
    mode = "EXECUTE" if execute else "DRY RUN - nothing written"
    print(f"\n=== Results archive {'rebuild ' if rebuild else ''}({mode}) ===")
    print(
        f"{'Season':<8} {'League':<26} {'Existing':>8} {'Added':>6} {'Rows':>5}  Status"
    )
    for f in sorted(report.files, key=lambda f: (f.season, f.league)):
        print(
            f"{f.season:<8} {f.league:<26} {f.existing:>8} {f.added:>6} "
            f"{f.rows:>5}  {f.status}"
        )
    total = sum(f.rows for f in report.files)
    added = sum(f.added for f in report.files)
    print(f"Total rows: {total} ({added} new) in {len(report.files)} files")
    puts = sum(f.status in {"dry-run", "written"} for f in report.files)
    index_puts = 1 if puts or report.index_entries else 0
    print(
        f"Blob puts: {puts} CSV(s) + {index_puts} index "
        "(plus retries if a read-back lags)"
    )
    if report.unread:
        print(f"\nUnreadable prediction files ({len(report.unread)}):")
        for path in report.unread:
            print(f"  {path}")
    print(f"\nUnmatched fixtures (not written, {len(report.unmatched)}):")
    for fx in report.unmatched:
        print(
            f"  {fx.season} {fx.league:<26} "
            f"{results_archive.format_kickoff(fx.kickoff)} {fx.home} - {fx.away}"
        )
    if execute:
        print(f"\nIndex updated: {report.index_updated}; all ok: {report.ok}")


# %% --------------------------------------------
# * Main


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--env-file", type=Path, default=REPO_ROOT / ".env.local")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="report rows per file and unmatched fixtures, write nothing (default)",
    )
    mode.add_argument(
        "--execute", action="store_true", help="write the CSVs and index.json"
    )
    parser.add_argument(
        "--seasons",
        nargs="+",
        help="seasons like 2026/27 (default: current season and the two before)",
    )
    parser.add_argument(
        "--rebuild",
        metavar="SEASON",
        help="rebuild this season's files from the sources instead of adding rows",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="let --rebuild write files with fewer rows than index.json lists",
    )
    parser.add_argument(
        "--save-csv",
        type=Path,
        metavar="DIR",
        help="also write each file's resulting CSV to DIR (local review)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help="disk cache for prediction files and result API responses",
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
    if args.execute and near_cron(now):
        sys.exit("Not writing on Tue/Fri 02:00-17:00 UTC (cron window)")
    seasons = {args.rebuild} if args.rebuild else set(args.seasons or [])
    seasons = seasons or set(default_seasons(now))

    reader = CachedReader(args.cache_dir / "blob")
    report = results_archive.update_results_archive(
        seasons,
        now=now,
        cache=results_archive.HttpCache(args.cache_dir / "http"),
        fetch_jsonl=reader.fetch_jsonl,
        rebuild=bool(args.rebuild),
        force=args.force,
        dry_run=not args.execute,
    )
    print_report(report, execute=args.execute, rebuild=bool(args.rebuild))
    if args.save_csv:
        for pathname, rows in report.contents.items():
            target = args.save_csv / pathname
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(results_archive.rows_to_csv(rows))
        print(f"\nSaved {len(report.contents)} CSV(s) under {args.save_csv}")
    if args.execute and not report.ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
