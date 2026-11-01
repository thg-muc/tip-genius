"""Backfill the Vercel Blob archive with existing history (one-time, idempotent).

Copies past predictions and odds into the private Blob store with the same
naming the workflow uses (see ``src/tip_genius/lib/blob_archive.py``), writes a
manifest per run and merges every run into ``index.json``. Coverage starts
with season 2025/26 (``--since``, default 2025-07-01); 2024/25 was the beta
phase and is not archived.

Sources, and how their schemas differ (deliberately not hidden):

1. KV predictions: every timestamped key ``<ts>_Match_Predictions_...`` with
   ``<ts> >= --since`` (``_debug`` keys excluded), read with the read-only
   token, never modified -> ``predictions/<ts>_<base_key>.jsonl``. The content
   is the KV value, one JSONL line per league, exactly as the workflow writes.
2. ``ODDS_SNAPSHOT`` lines from GitHub Actions logs (via ``gh``, 90-day
   retention, logging started 2026-10-30) -> ``odds/<run start>_Match_Odds_
   <League>.jsonl``. These have NO ``id`` column, and their ``timestamp`` is
   the per-league fetch time, while the pathname uses the run's start time.
   Runs logged in debug mode are skipped, like the workflow does.
3. Local ``data/api_result/<ts>_<League>_odds_api.json`` -> processed with the
   same ``process_api_data`` call as the workflow -> ``odds/<ts>_Match_Odds_
   <League>.jsonl``. These DO include ``id``; the timestamp is the file's.
   ``data/llm_data`` is not used: its 2025/26 files come from the same local
   runs as ``data/api_result``.
4. ``data/reconstructed_odds.csv`` (built by ``utils/reconstruct_odds.py``,
   read only here) -> ``odds/reconstructed/<mtime>_Match_Odds_Reconstructed.csv``
   plus a README marking them as ESTIMATED. Never mixed into ``odds/``. Re-run
   the reconstruction BEFORE the single backfill, so the CSV covers every
   predicted match up to the first live run. A later re-run changes the mtime
   and archives a new version; with several CSVs, the newest one (highest
   timestamp, last in ``index.json`` ``reconstructed_odds``) is the current one.

Runs are rebuilt by clustering all timestamps (gap <= 30 min, no league or
provider twice per run); the earliest timestamp becomes the ``run_ts`` and each
run gets ``runs/<run_ts>.json`` with ``"source": "backfill"``.

Live runs: everything from the first run with ``"source": "workflow"`` in
``index.json`` on (minus a 30 min margin) is skipped, because the workflow
archived it already, under its own run timestamp. ``--until`` can only lower
that bound.

Idempotent: pathnames listed in ``index.json`` are skipped, and a real run
HEAD-checks (a cheap simple operation) every remaining pathname before the
upload; a failed HEAD falls back to the write-once upload. Manifests and the
index are written even if the run stops partway. Uploads are throttled to
<= 10/s (Hobby: 2,000 advanced ops/month).

Timing: the ``index.json`` update is a read-modify-write, so never run the
backfill or ``--rebuild-index`` near the cron (Tue/Fri 03:00 UTC).

Usage:
    uv run python utils/backfill_blob_archive.py --dry-run --data-dir /path/to/data
    uv run python utils/backfill_blob_archive.py --env-file ... --data-dir ...
    uv run python utils/backfill_blob_archive.py --rebuild-index --env-file ...

``--rebuild-index`` recreates ``index.json`` from all ``runs/*.json``
manifests (one ``list`` call per 1,000 files) if an index update failed.
"""
# * Author(s): Thomas Glanzer
# * Creation : Nov 2026
# * License: MIT license

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import requests
import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parents[1] / "src" / "tip_genius"))

from lib import blob_archive  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

logger = logging.getLogger(__name__)

# %% --------------------------------------------
# * Constants

REPO_ROOT = Path(__file__).parents[1]
API_CONFIG = REPO_ROOT / "src" / "tip_genius" / "cfg" / "api_config.yaml"
BERLIN = ZoneInfo("Europe/Berlin")
KV_KEY_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})_(Match_Predictions_.+)$"
)
API_FILE_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})_(.+)_odds_api\.json$"
)
SNAPSHOT_PATTERN = re.compile(r"ODDS_SNAPSHOT (\{.*\})\s*$")
DEBUG_MARKER = "Debug mode is enabled"
# First run that can contain ODDS_SNAPSHOT lines (PR #68)
LOG_ODDS_SINCE = datetime(2026, 10, 30, 7, 49, tzinfo=UTC)
WORKFLOW_FILE = "match-predictions.yaml"
RUN_GAP = timedelta(minutes=30)
# Same pairing with another kickoff within this window = rescheduled match
RESCHEDULE_WINDOW = timedelta(days=21)
MIN_UPLOAD_INTERVAL = 0.1  # <= 10 uploads/s
UPLOAD_WARN_THRESHOLD = 1500
RECONSTRUCTED_README = "odds/reconstructed/README.txt"
README_TEXT = (
    "ESTIMATED ODDS - NOT REAL BOOKMAKER DATA.\n"
    "Files in odds/reconstructed/ were rebuilt from the LLMs' reasoning texts by\n"
    "utils/reconstruct_odds.py (median across providers, ~97% of values within 5%\n"
    "of true odds). Use them only where no real odds/ file exists; never mix them\n"
    "into odds/. Column 'source' says how each row was obtained.\n"
)

# %% --------------------------------------------
# * Data model


@dataclass
class Item:
    """One file to archive."""

    pathname: str
    body: bytes
    content_type: str
    source: str
    stamp: datetime | None = None
    kind: str = ""  # "predictions" or "odds" for files that belong to a run
    key: str = ""  # provider key or league key


@dataclass
class Fixture:
    """A match identified by the odds <-> predictions join key."""

    league: str
    kickoff: datetime  # UTC
    home_team: str
    away_team: str
    last_stamp: datetime | None = None  # latest snapshot predicting this kickoff

    @property
    def join_key(self) -> tuple[str, str, datetime]:
        """Return ``(home_team, away_team, kickoff)``."""
        return (self.home_team, self.away_team, self.kickoff)


@dataclass
class Plan:
    """Everything the backfill would archive, and the coverage check."""

    items: list[Item] = field(default_factory=list)
    predicted: list[Fixture] = field(default_factory=list)
    odds_keys: set[tuple[str, str, datetime]] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)
    rescheduled: int = 0


# %% --------------------------------------------
# * Helpers


def parse_ts(text: str) -> datetime:
    """Parse a ``YYYY-MM-DD_HH-MM-SS`` file name timestamp (UTC)."""
    return datetime.strptime(text, blob_archive.RUN_TS_FORMAT).replace(tzinfo=UTC)


def parse_iso(text: str) -> datetime:
    """Parse an ISO timestamp such as ``2026-10-30T09:49:58Z`` (UTC)."""
    return datetime.fromisoformat(text).astimezone(UTC)


def berlin_to_utc(text: str) -> datetime:
    """Convert a prediction's ``dd.mm.YYYY HH:MM`` (Europe/Berlin) to UTC."""
    local = datetime.strptime(text, "%d.%m.%Y %H:%M").replace(tzinfo=BERLIN)
    return local.astimezone(UTC)


def sport_names() -> dict[str, str]:
    """Map league keys (``Bundesliga-Germany``) to sport names."""
    with API_CONFIG.open(encoding="utf-8") as f:
        mapping = yaml.safe_load(f)["odds_api"]["sports_mapping"]
    return {blob_archive.league_key(sport): sport for sport in mapping}


def odds_keys_from_rows(
    columns: list[str], matches: list[list[Any]]
) -> set[tuple[str, str, datetime]]:
    """Return the join keys of odds rows that carry real odds."""
    col = {name: i for i, name in enumerate(columns)}
    keys = set()
    for row in matches:
        if not row[col["odds_home"]]:
            continue
        keys.add(
            (
                row[col["home_team"]],
                row[col["away_team"]],
                parse_iso(row[col["commence_time"]]),
            )
        )
    return keys


# %% --------------------------------------------
# * Sources


def kv_command(url: str, token: str, *args: str) -> Any:
    """Run one read-only Upstash REST command."""
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}"},
        json=list(args),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["result"]


def load_kv_values(since: datetime) -> dict[str, list[dict[str, Any]]]:
    """Read all non-debug timestamped prediction keys since ``since`` from KV."""
    url = os.environ["KV_REST_API_URL"]
    token = os.environ.get("KV_REST_API_READ_ONLY_TOKEN") or os.environ.get(
        "KV_REST_API_TOKEN", ""
    )
    keys: list[str] = []
    cursor = "0"
    while True:
        cursor, batch = kv_command(
            url,
            token,
            "SCAN",
            cursor,
            "MATCH",
            "*_Match_Predictions_*",
            "COUNT",
            "1000",
        )
        keys.extend(batch)
        if cursor == "0":
            break
    selected = sorted(
        key
        for key in keys
        if (match := KV_KEY_PATTERN.match(key))
        and not key.endswith("_debug")
        and parse_ts(match[1]) >= since
    )
    values: dict[str, list[dict[str, Any]]] = {}
    for start in range(0, len(selected), 25):
        batch = selected[start : start + 25]
        for key, value in zip(
            batch, kv_command(url, token, "MGET", *batch), strict=True
        ):
            if value is not None:
                values[key] = json.loads(value)
    logger.info("Loaded %d prediction snapshots from KV", len(values))
    return values


def latest_fixtures(
    last_seen: dict[tuple[str, str, datetime], tuple[Fixture, datetime]],
) -> tuple[list[Fixture], int]:
    """Keep each match with the kickoff of its latest snapshot.

    A kickoff that later snapshots moved (same pairing, another kickoff within
    RESCHEDULE_WINDOW) is superseded, so the coverage check uses the kickoff
    the match was finally predicted with. Returns the fixtures and the number
    of superseded kickoffs.
    """
    by_pair: dict[tuple[str, str], list[tuple[Fixture, datetime]]] = defaultdict(list)
    for fixture, stamp in last_seen.values():
        by_pair[fixture.home_team, fixture.away_team].append((fixture, stamp))
    kept: list[Fixture] = []
    superseded = 0
    for entries in by_pair.values():
        for fixture, stamp in entries:
            if any(
                other is not fixture
                and abs(other.kickoff - fixture.kickoff) <= RESCHEDULE_WINDOW
                and other_stamp > stamp
                for other, other_stamp in entries
            ):
                superseded += 1
            else:
                kept.append(replace(fixture, last_stamp=stamp))
    return sorted(kept, key=lambda f: (f.kickoff, f.league)), superseded


def kv_items(
    values: dict[str, list[dict[str, Any]]], since: datetime
) -> tuple[list[Item], list[Fixture], int]:
    """Turn KV values into prediction items and the fixtures they predict.

    Returns the items, the fixtures (latest kickoff per match) and the number
    of superseded (rescheduled) kickoffs.
    """
    items: list[Item] = []
    last_seen: dict[tuple[str, str, datetime], tuple[Fixture, datetime]] = {}
    for key, payload in sorted(values.items()):
        match = KV_KEY_PATTERN.match(key)
        if not match:
            continue
        stamp_text, base_key = match[1], match[2]
        items.append(
            Item(
                pathname=blob_archive.predictions_pathname(stamp_text, base_key),
                body=blob_archive.to_jsonl(payload),
                content_type=blob_archive.JSONL_CONTENT_TYPE,
                source="kv",
                stamp=parse_ts(stamp_text),
                kind="predictions",
                key=blob_archive.provider_key(base_key),
            )
        )
        for league in payload:
            for m in league.get("matches", []):
                fixture = Fixture(
                    league=league["name"],
                    kickoff=berlin_to_utc(m["commence_time_str"]),
                    home_team=m["home_team"],
                    away_team=m["away_team"],
                )
                if fixture.kickoff >= since:
                    last_seen[fixture.join_key] = (fixture, parse_ts(stamp_text))
    fixtures, superseded = latest_fixtures(last_seen)
    return items, fixtures, superseded


def api_result_items(
    data_dir: Path,
    since: datetime,
    process: Callable[[Any], tuple[list[str], list[list[Any]]]],
) -> list[Item]:
    """Process local raw Odds API responses into odds items."""
    names = sport_names()
    items: list[Item] = []
    for path in sorted((data_dir / "api_result").glob("*_odds_api.json")):
        match = API_FILE_PATTERN.match(path.name)
        if not match or parse_ts(match[1]) < since:
            continue
        sport = names.get(match[2])
        if sport is None:
            logger.warning("Unknown league in %s, skipped", path.name)
            continue
        api_result = json.loads(path.read_text(encoding="utf-8"))
        if not api_result:
            continue
        columns, matches = process(api_result)
        payload = blob_archive.build_odds_payload(
            sport,
            blob_archive.format_payload_ts(parse_ts(match[1])),
            columns,
            matches,
        )
        items.append(
            Item(
                pathname=blob_archive.odds_pathname(match[1], sport),
                body=blob_archive.to_jsonl(payload),
                content_type=blob_archive.JSONL_CONTENT_TYPE,
                source="api_result",
                stamp=parse_ts(match[1]),
                kind="odds",
                key=blob_archive.league_key(sport),
            )
        )
    return items


def make_processor() -> Callable[[Any], tuple[list[str], list[list[Any]]]]:
    """Return a processor using the workflow's own ``process_api_data``."""
    from lib.api_data import OddsAPI  # noqa: PLC0415

    # The key is only needed to fetch, never to process
    os.environ.setdefault("ODDS_API_KEY", "unused-for-processing")
    api = OddsAPI()

    def process(api_result: Any) -> tuple[list[str], list[list[Any]]]:
        data = api.process_api_data(
            api_result=api_result, named_teams=True, additional_info=True
        )
        rows = data.select(blob_archive.ODDS_COLUMNS).iter_rows()
        return blob_archive.ODDS_COLUMNS, [list(row) for row in rows]

    return process


def gh(*args: str) -> str:
    """Run a read-only ``gh`` command and return stdout."""
    return subprocess.run(  # noqa: S603
        ["gh", *args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def parse_log_snapshots(log: str) -> list[dict[str, Any]]:
    """Extract the ``ODDS_SNAPSHOT`` payloads from one run log."""
    return [
        json.loads(match[1])
        for line in log.splitlines()
        if (match := SNAPSHOT_PATTERN.search(line))
    ]


def log_items(
    repo: str, run_fetcher: Callable[[str, str], str] | None = None
) -> tuple[list[Item], list[str]]:
    """Collect odds from ``ODDS_SNAPSHOT`` lines of recent workflow runs."""
    fetch = run_fetcher or (
        lambda run_id, repo_: gh("run", "view", run_id, "--repo", repo_, "--log")
    )
    notes: list[str] = []
    runs = json.loads(
        gh(
            "run",
            "list",
            "--repo",
            repo,
            "--workflow",
            WORKFLOW_FILE,
            "--limit",
            "100",
            "--json",
            "databaseId,startedAt,status,event",
        )
    )
    items: list[Item] = []
    for run in runs:
        started = parse_iso(run["startedAt"])
        if started < LOG_ODDS_SINCE or run["status"] != "completed":
            continue
        log = fetch(str(run["databaseId"]), repo)
        if DEBUG_MARKER in log:
            notes.append(f"Actions run {run['databaseId']}: debug mode, skipped")
            continue
        run_ts = blob_archive.format_run_ts(started)
        seen: set[str] = set()
        for snap in parse_log_snapshots(log):
            if snap["sport"] in seen:
                continue
            seen.add(snap["sport"])
            payload = blob_archive.build_odds_payload(
                snap["sport"],
                blob_archive.format_payload_ts(parse_iso(snap["fetched_at"])),
                snap["columns"],
                snap["matches"],
            )
            items.append(
                Item(
                    pathname=blob_archive.odds_pathname(run_ts, snap["sport"]),
                    body=blob_archive.to_jsonl(payload),
                    content_type=blob_archive.JSONL_CONTENT_TYPE,
                    source="actions_log",
                    stamp=started,
                    kind="odds",
                    key=blob_archive.league_key(snap["sport"]),
                )
            )
    return items, notes


def reconstructed_items(
    csv_path: Path,
) -> tuple[list[Item], set[tuple[str, str, datetime]]]:
    """Archive the reconstructed odds CSV (estimated) plus its README."""
    if not csv_path.is_file():
        return [], set()
    stamp = datetime.fromtimestamp(csv_path.stat().st_mtime, tz=UTC)
    keys = set()
    with csv_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("odds_home"):
                keys.add(
                    (
                        row["home_team"],
                        row["away_team"],
                        parse_iso(row["kickoff_utc"]),
                    )
                )
    items = [
        Item(
            pathname=blob_archive.reconstructed_odds_pathname(
                blob_archive.format_run_ts(stamp)
            ),
            body=csv_path.read_bytes(),
            content_type=blob_archive.CSV_CONTENT_TYPE,
            source="reconstructed",
        ),
        Item(
            pathname=RECONSTRUCTED_README,
            body=README_TEXT.encode("utf-8"),
            content_type="text/plain",
            source="reconstructed",
        ),
    ]
    return items, keys


# %% --------------------------------------------
# * Runs and manifests


def group_runs(items: Iterable[Item]) -> dict[str, list[Item]]:
    """Cluster run files into runs keyed by ``run_ts`` (earliest timestamp).

    A new run starts after a gap of more than RUN_GAP, or when a league or
    provider would appear twice in the same run.
    """
    runs: dict[str, list[Item]] = {}
    current: list[Item] = []
    previous: datetime | None = None
    dated = sorted(
        (i for i in items if i.kind and i.stamp is not None),
        key=lambda i: (i.stamp, i.pathname),
    )
    for item in dated:
        assert item.stamp is not None
        clash = any(i.kind == item.kind and i.key == item.key for i in current)
        if (
            current
            and previous is not None
            and (item.stamp - previous > RUN_GAP or clash)
        ):
            runs[blob_archive.format_run_ts(current[0].stamp)] = current  # type: ignore[arg-type]
            current = []
        current.append(item)
        previous = item.stamp
    if current:
        runs[blob_archive.format_run_ts(current[0].stamp)] = current  # type: ignore[arg-type]
    return runs


def build_manifests(
    runs: dict[str, list[Item]], archived: set[str]
) -> list[dict[str, Any]]:
    """Build backfill manifests listing only files that are archived."""
    manifests = []
    for run_ts, run_items in sorted(runs.items()):
        odds = {
            i.key: i.pathname
            for i in run_items
            if i.kind == "odds" and i.pathname in archived
        }
        preds = {
            i.key: i.pathname
            for i in run_items
            if i.kind == "predictions" and i.pathname in archived
        }
        if odds or preds:
            manifests.append(
                blob_archive.build_manifest(run_ts, odds, preds, source="backfill")
            )
    return manifests


# %% --------------------------------------------
# * Upload


class Uploader:
    """Throttled, idempotent uploader (or counter in dry-run mode)."""

    def __init__(self, existing: set[str], *, dry_run: bool, head_check: bool) -> None:
        """Initialize with the pathnames already known from the index."""
        self.existing = set(existing)
        self.dry_run = dry_run
        self.head_check = head_check
        self.archived: set[str] = set(existing)
        self.stats: dict[str, Counter[str]] = defaultdict(Counter)
        self._last_upload = 0.0

    def exists(self, pathname: str) -> bool:
        """Check the index, then HEAD (real runs, or dry-run with --head-check)."""
        if pathname in self.existing:
            return True
        if self.dry_run and not self.head_check:
            return False
        return blob_archive.blob_exists(pathname)

    def upload(self, item: Item) -> None:
        """Upload one item unless it already exists."""
        stats = self.stats[item.source]
        stats["candidates"] += 1
        if self.exists(item.pathname):
            stats["existing"] += 1
            self.archived.add(item.pathname)
            return
        stats["uploads"] += 1
        stats["bytes"] += len(item.body)
        if self.dry_run:
            self.archived.add(item.pathname)
            return
        wait = MIN_UPLOAD_INTERVAL - (time.monotonic() - self._last_upload)
        if wait > 0:
            time.sleep(wait)
        self._last_upload = time.monotonic()
        if blob_archive.put_with_retry(
            item.pathname, item.body, content_type=item.content_type
        ):
            self.archived.add(item.pathname)
        else:
            stats["failed"] += 1
            stats["uploads"] -= 1
            stats["bytes"] -= len(item.body)


def run_backfill(
    plan: Plan, index: dict[str, Any], *, dry_run: bool, head_check: bool
) -> Uploader:
    """Upload all files, then manifests, then merge the runs into the index.

    Manifests and index are written in ``finally``, so everything uploaded so
    far is indexed even if the run stops partway.
    """
    uploader = Uploader(
        blob_archive.index_pathnames(index), dry_run=dry_run, head_check=head_check
    )
    try:
        for item in plan.items:
            uploader.upload(item)
    finally:
        write_manifests_and_index(plan, index, uploader, dry_run=dry_run)
    return uploader


def write_manifests_and_index(
    plan: Plan, index: dict[str, Any], uploader: Uploader, *, dry_run: bool
) -> None:
    """Write manifests and merge the index for every archived file."""
    manifests = build_manifests(group_runs(plan.items), uploader.archived)
    entries = []
    for manifest in manifests:
        pathname = blob_archive.manifest_pathname(manifest["run_ts"])
        uploader.upload(
            Item(
                pathname=pathname,
                body=blob_archive.to_json(manifest),
                content_type=blob_archive.JSON_CONTENT_TYPE,
                source="manifest",
            )
        )
        entries.append(
            blob_archive.index_entry(
                manifest, manifest_written=pathname in uploader.archived
            )
        )
    reconstructed = [
        i.pathname
        for i in plan.items
        if i.source == "reconstructed"
        and i.pathname.endswith(".csv")
        and i.pathname in uploader.archived
    ]
    merged = blob_archive.merge_index(index, entries, reconstructed)
    index_changed = blob_archive.index_pathnames(
        merged
    ) != blob_archive.index_pathnames(index)
    stats = uploader.stats["index"]
    stats["candidates"] += 1
    if index_changed:
        stats["uploads"] += 1
        stats["bytes"] += len(blob_archive.to_json(merged))
        if not dry_run:
            blob_archive.update_index(entries, reconstructed)
    else:
        stats["existing"] += 1


def live_cutoff(index: dict[str, Any]) -> datetime | None:
    """Return where the backfill must stop: before the first live workflow run.

    Runs from then on are archived by the workflow itself. Re-reading them
    would duplicate them under another ``run_ts`` (the Actions start time,
    not the workflow's run timestamp), so the cutoff keeps a RUN_GAP margin.
    """
    first = blob_archive.first_workflow_run(index)
    return parse_ts(first) - RUN_GAP if first else None


def effective_until(until: datetime | None, cutoff: datetime | None) -> datetime | None:
    """Combine ``--until`` with the live-run cutoff (the earlier one wins)."""
    bounds = [b for b in (until, cutoff) if b is not None]
    return min(bounds) if bounds else None


def rebuild_index() -> None:
    """Recreate ``index.json`` from all run manifests (uses ``list``)."""
    from vercel.blob import sync as vercel_blob  # noqa: PLC0415

    reader = blob_archive.BlobArchiveReader()
    entries = [
        blob_archive.index_entry(reader.fetch_json(b.pathname), manifest_written=True)
        for b in vercel_blob.query_blobs(prefix="runs/", page_size=1000)
        if b.pathname.endswith(".json")
    ]
    reconstructed = [
        b.pathname
        for b in vercel_blob.query_blobs(prefix="odds/reconstructed/", page_size=1000)
        if b.pathname.endswith(".csv")
    ]
    merged = blob_archive.merge_index(None, entries, reconstructed)
    ok = blob_archive.put_with_retry(
        blob_archive.INDEX_PATHNAME,
        blob_archive.to_json(merged),
        content_type=blob_archive.JSON_CONTENT_TYPE,
        allow_overwrite=True,
        cache_control_max_age=blob_archive.INDEX_CACHE_MAX_AGE,
    )
    print(f"Rebuilt index from {len(entries)} manifests: {'ok' if ok else 'FAILED'}")


# %% --------------------------------------------
# * Report


def print_report(plan: Plan, uploader: Uploader, *, dry_run: bool) -> None:
    """Print counts and sizes per source plus the odds coverage check."""
    title = "DRY RUN - nothing uploaded" if dry_run else "Backfill finished"
    print(f"\n=== {title} ===")
    print(
        f"{'source':<14}{'candidates':>11}{'existing':>10}{'uploads':>9}{'MB':>9}{'failed':>8}"
    )
    total_uploads = total_bytes = 0
    for source in [
        "kv",
        "api_result",
        "actions_log",
        "reconstructed",
        "manifest",
        "index",
    ]:
        s = uploader.stats.get(source, Counter())
        total_uploads += s["uploads"]
        total_bytes += s["bytes"]
        print(
            f"{source:<14}{s['candidates']:>11}{s['existing']:>10}{s['uploads']:>9}"
            f"{s['bytes'] / 1e6:>9.2f}{s['failed']:>8}"
        )
    print(f"{'total':<14}{'':>11}{'':>10}{total_uploads:>9}{total_bytes / 1e6:>9.2f}")
    if total_uploads > UPLOAD_WARN_THRESHOLD:
        print(
            f"WARNING: {total_uploads} uploads exceed {UPLOAD_WARN_THRESHOLD}; "
            "split the backfill (e.g. by --since/--until) across two months."
        )
    for note in plan.notes:
        print(f"Note: {note}")

    missing = [f for f in plan.predicted if f.join_key not in plan.odds_keys]
    print(
        f"\nOdds coverage: {len(plan.predicted) - len(missing)}/{len(plan.predicted)} "
        "predicted matches have an odds row (real or reconstructed); "
        f"{plan.rescheduled} earlier kickoffs of rescheduled matches not counted"
    )
    for f in missing:
        print(
            f"  missing: {f.kickoff:%Y-%m-%d %H:%M} UTC  {f.league:<26} "
            f"{f.home_team} - {f.away_team}"
        )


# %% --------------------------------------------
# * Main


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--env-file", type=Path, default=REPO_ROOT / ".env.local")
    parser.add_argument("--data-dir", type=Path, default=REPO_ROOT / "data")
    parser.add_argument(
        "--since",
        type=lambda s: datetime.fromisoformat(s).replace(tzinfo=UTC),
        default=datetime(2025, 7, 1, tzinfo=UTC),
        help="first KV/api_result timestamp to archive (default 2025-07-01)",
    )
    parser.add_argument(
        "--until",
        type=lambda s: datetime.fromisoformat(s).replace(tzinfo=UTC),
        default=None,
        help=(
            "exclusive upper bound for run files, to split the backfill; "
            "always capped before the first live workflow run in index.json"
        ),
    )
    parser.add_argument("--repo", default="thg-muc/tip-genius")
    parser.add_argument("--dry-run", action="store_true", help="count, upload nothing")
    parser.add_argument(
        "--head-check",
        action="store_true",
        help="in dry-run, also HEAD-check pathnames missing from the index",
    )
    parser.add_argument("--skip-logs", action="store_true", help="skip Actions logs")
    parser.add_argument("--rebuild-index", action="store_true")
    return parser.parse_args()


def build_plan(args: argparse.Namespace) -> Plan:
    """Collect all sources into one plan."""
    plan = Plan()
    values = load_kv_values(args.since)
    preds, plan.predicted, plan.rescheduled = kv_items(values, args.since)

    odds = api_result_items(args.data_dir, args.since, make_processor())
    if args.skip_logs:
        plan.notes.append("Actions logs skipped (--skip-logs)")
    else:
        try:
            logged, notes = log_items(args.repo)
            odds += logged
            plan.notes += notes
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            plan.notes.append(f"Actions logs not read: {e}")
    for item in odds:
        payload = json.loads(item.body)
        plan.odds_keys |= odds_keys_from_rows(payload["columns"], payload["matches"])

    run_files = preds + odds
    if args.until is not None:
        kept = [i for i in run_files if i.stamp is None or i.stamp < args.until]
        plan.notes.append(
            f"{len(run_files) - len(kept)} run files at or after "
            f"{args.until:%Y-%m-%d %H:%M} UTC skipped (--until / live runs)"
        )
        run_files = kept
        plan.predicted = [
            f
            for f in plan.predicted
            if f.last_stamp is None or f.last_stamp < args.until
        ]
    recon, recon_keys = reconstructed_items(args.data_dir / "reconstructed_odds.csv")
    if not recon:
        plan.notes.append("reconstructed_odds.csv not found")
    plan.odds_keys |= recon_keys
    plan.items = run_files + recon
    return plan


def main() -> None:
    """Run the backfill."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args()
    load_dotenv(args.env_file)
    if args.rebuild_index:
        rebuild_index()
        return
    if not os.environ.get("BLOB_READ_WRITE_TOKEN"):
        sys.exit("BLOB_READ_WRITE_TOKEN is not set (check --env-file)")
    index = blob_archive.read_index(fresh=True)
    cutoff = live_cutoff(index)
    args.until = effective_until(args.until, cutoff)
    plan = build_plan(args)
    if cutoff is not None:
        plan.notes.append(
            f"First live workflow run in index: stopping at {cutoff:%Y-%m-%d %H:%M} UTC"
        )
    uploader = run_backfill(
        plan, index, dry_run=args.dry_run, head_check=args.head_check
    )
    print_report(plan, uploader, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
