"""Gather the dashboard statistics inputs from Vercel Blob and build the JSON.

Shared by the workflow step (``StorageManager.update_dashboard_stats``) and
``utils/backfill_stats.py``. Updates the prediction tables of the given
seasons (``lib.prediction_table``), reads the other seasons' tables and every
results CSV, and computes the statistics (``lib.stats``).

Blob reads are the scarce resource (Hobby plan): the workflow passes in the
index and the results CSV contents its results step already read, and shares
the prediction file downloads with it. Tables and results CSVs of past
seasons are read with a plain download (1 simple operation) and only re-read
fresh (HEAD + download) if they have fewer rows than ``index.json`` lists;
the current season's are always read fresh.
"""
# * Author(s): Thomas Glanzer
# * Creation : Nov 2026
# * License: MIT license

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from lib import blob_archive, prediction_table, results_archive, stats

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

logger = logging.getLogger(__name__)

# Keep the JSON well below the KV value limits and the frontend's budget
TARGET_JSON_BYTES = 100_000


def seasons_since(first: str, now: datetime) -> list[str]:
    """Return the seasons from ``first`` up to the current one."""
    start, end = int(first[:4]), int(results_archive.season_of(now)[:4])
    return [f"{year}/{(year + 1) % 100:02d}" for year in range(start, end + 1)]


def verified_results(
    report: results_archive.ArchiveReport | None,
) -> dict[str, list[dict[str, str]]]:
    """Return the results CSV contents a results step left in Blob.

    Written or unchanged files as computed; a file whose write failed only
    with the rows it had before; stale files not at all.
    """
    if report is None:
        return {}
    contents: dict[str, list[dict[str, str]]] = {}
    for file_report in report.files:
        rows = report.contents.get(file_report.pathname)
        if rows is None:
            continue
        if file_report.status in {"unchanged", "written"}:
            contents[file_report.pathname] = rows
        elif file_report.status == "write-failed":
            contents[file_report.pathname] = rows[: file_report.existing]
    return contents


def read_rows(  # noqa: PLR0913
    pathname: str,
    expected: int,
    parse: Callable[[bytes], list[dict[str, str]]],
    *,
    fresh: bool,
    attempts: int,
    wait: float,
) -> tuple[list[dict[str, str]] | None, int]:
    """Read an overwritten CSV; return its rows (None if unusable) and reads.

    A plain download is trusted only if it has at least ``expected`` rows
    (``index.json``); otherwise, and with ``fresh``, the file is read with
    ``results_archive.read_rows_guarded`` (fresh, retried while stale).
    """
    reads = 0
    if not fresh:
        reads += 1
        try:
            rows = parse(blob_archive.BlobArchiveReader().fetch_bytes(pathname))
        except blob_archive.vercel_blob.BlobNotFoundError:
            if expected == 0:
                return [], reads
        except Exception as e:
            logger.info("Plain read of %s failed (%s), reading fresh", pathname, e)
        else:
            if len(rows) >= expected:
                return rows, reads
    rows = results_archive.read_rows_guarded(
        pathname, expected, attempts=attempts, wait=wait, parse=parse
    )
    return rows, reads + 2


@dataclass
class DashboardInputs:
    """What the statistics are computed from, and how it was obtained."""

    # Updated seasons' table reports
    tables: dict[str, prediction_table.TableReport] = field(default_factory=dict)
    # Rows per season (None: table unusable this run)
    table_rows: dict[str, list[dict[str, str]] | None] = field(default_factory=dict)
    # Results CSV rows per pathname
    result_rows: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    # Overwritten files read (fresh reads count as two simple operations)
    reads: int = 0
    # Files that could not be read
    unread: list[str] = field(default_factory=list)

    def all_table_rows(self) -> list[dict[str, str]]:
        """Every usable table row."""
        return [row for rows in self.table_rows.values() if rows for row in rows]

    def all_result_rows(self) -> list[dict[str, str]]:
        """Every results row."""
        return [row for rows in self.result_rows.values() for row in rows]


def collect_inputs(  # noqa: PLR0913
    index: dict[str, Any],
    *,
    now: datetime,
    seasons: list[str],
    update: set[str],
    fetch_jsonl: Callable[[str], list[dict[str, Any]]],
    fetch_bytes: Callable[[str], bytes],
    results_contents: dict[str, list[dict[str, str]]] | None = None,
    force_full: str | None = None,
    rebuild: bool = False,
    force: bool = False,
    dry_run: bool = False,
    attempts: int = blob_archive.INDEX_VERIFY_ATTEMPTS,
    wait: float = blob_archive.INDEX_VERIFY_WAIT_SECONDS,
) -> DashboardInputs:
    """Update the tables of ``update`` and read everything else for ``seasons``.

    Parameters
    ----------
    index : dict[str, Any]
        A current ``index.json``.
    now : datetime
        Current time.
    seasons : list[str]
        Seasons to include (``2025/26`` style).
    update : set[str]
        Seasons whose prediction table is brought up to date first.
    fetch_jsonl, fetch_bytes : Callable
        Read write-once archive files (shared with the results step).
    results_contents : dict | None, optional
        Results CSV rows already read this run (``verified_results``).
    force_full, rebuild, force, dry_run, attempts, wait : optional
        Passed to ``prediction_table.update_prediction_table``.

    Returns
    -------
    DashboardInputs
        Table reports, rows and read counts.

    """
    inputs = DashboardInputs()
    current = results_archive.season_of(now)
    indexed_tables = index.get(blob_archive.STATS_KEY, {})
    indexed_results = index.get(blob_archive.RESULTS_KEY, {})
    contents = results_contents or {}
    for season in seasons:
        pathname = prediction_table.table_pathname(season)
        if season in update:
            report = prediction_table.update_prediction_table(
                season,
                index=index,
                now=now,
                fetch_jsonl=fetch_jsonl,
                fetch_bytes=fetch_bytes,
                force_full=force_full,
                rebuild=rebuild,
                force=force,
                dry_run=dry_run,
                attempts=attempts,
                wait=wait,
            )
            inputs.tables[season] = report
            inputs.table_rows[season] = report.contents
            inputs.reads += 0 if rebuild else 2
            inputs.unread.extend(report.unread)
        else:
            expected = int(indexed_tables.get(pathname, {}).get("rows", 0))
            if pathname not in indexed_tables:
                inputs.table_rows[season] = []
                continue
            rows, reads = read_rows(
                pathname,
                expected,
                prediction_table.rows_from_csv,
                fresh=season == current,
                attempts=attempts,
                wait=wait,
            )
            inputs.reads += reads
            inputs.table_rows[season] = rows
            if rows is None:
                inputs.unread.append(pathname)

        prefix = f"{results_archive.RESULTS_PREFIX}{season.replace('/', '-')}_"
        for path in sorted(indexed_results):
            if not path.startswith(prefix):
                continue
            if path in contents:
                inputs.result_rows[path] = contents[path]
                continue
            rows, reads = read_rows(
                path,
                int(indexed_results[path].get("rows", 0)),
                results_archive.rows_from_csv,
                fresh=season == current,
                attempts=attempts,
                wait=wait,
            )
            inputs.reads += reads
            if rows is None:
                inputs.unread.append(path)
            else:
                inputs.result_rows[path] = rows
    return inputs


def build_json(
    inputs: DashboardInputs, *, active: list[str], now: datetime
) -> tuple[dict[str, Any], bytes]:
    """Compute the statistics and their compact JSON bytes."""
    data = stats.build_stats(
        inputs.all_table_rows(), inputs.all_result_rows(), active=active, now=now
    )
    body = stats.to_json_bytes(data)
    if len(body) > TARGET_JSON_BYTES:
        logger.warning(
            "Dashboard stats JSON is %d bytes (target < %d)",
            len(body),
            TARGET_JSON_BYTES,
        )
    return data, body
