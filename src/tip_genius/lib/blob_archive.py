"""Helpers for the long-term archive of predictions and odds in Vercel Blob.

The archive lives in a private Vercel Blob store and is laid out flat by type:

- ``predictions/<YYYY-MM-DD_HH-MM-SS>_<base_key>.jsonl``
- ``predictions/backfill/<run_ts>_<base_key>.jsonl`` (predictions generated
  later for a past run, each line marked with a ``backfill`` object)
- ``odds/<YYYY-MM-DD_HH-MM-SS>_Match_Odds_<League-Key>.jsonl``
- ``odds/reconstructed/<YYYY-MM-DD_HH-MM-SS>_Match_Odds_Reconstructed.csv``
- ``runs/<run_ts>.json`` (one manifest per workflow run)
- ``results/<season>_<League-Key>.csv`` (final 90-minute scores, see
  ``lib.results_archive``; rewritten in place when rows are added)
- ``stats/predictions_<season>.csv`` (one row per fixture and provider for
  the dashboard statistics, see ``lib.prediction_table``; rewritten in place)
- ``index.json`` (every archived run, so readers never need ``list``)

Backfilled predictions are listed in ``index.json`` under
``backfill_predictions`` (``{run_ts: {provider_key: pathname}}``), never in
``runs``, so readers cannot mistake them for live pre-match predictions.

Every file is write-once except ``index.json``, the results CSVs, listed under
``results`` (``{pathname: {"rows", "updated_at"}}``), and the prediction tables,
listed the same way under ``stats``. With
several reconstructed CSVs, the newest one (highest timestamp, last in
``reconstructed_odds``) is the current one. The ``index.json``
read-modify-write is not atomic, so never run the backfill or
``--rebuild-index`` near the cron (Tue/Fri 03:00 UTC). The SDK
reads the static ``BLOB_READ_WRITE_TOKEN`` from the environment.
"""
# * Author(s): Thomas Glanzer
# * Creation : Nov 2026
# * License: MIT license

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from vercel.blob import sync as vercel_blob

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

logger = logging.getLogger(__name__)

# %% --------------------------------------------
# * Constants

RUN_TS_FORMAT = "%Y-%m-%d_%H-%M-%S"
PAYLOAD_TS_FORMAT = "%Y-%m-%d %H:%M:%S"
INDEX_PATHNAME = "index.json"
PREDICTIONS_PREFIX = "Match_Predictions_"
BACKFILL_PREFIX = "predictions/backfill/"
BACKFILL_KEY = "backfill_predictions"
RESULTS_KEY = "results"
RESULTS_SCAN_KEY = "results_scan"
STATS_KEY = "stats"
STATS_SCAN_KEY = "stats_scan"
ODDS_SOURCES = ("logged", "reconstructed")
ODDS_COLUMNS = [
    "id",
    "commence_time",
    "home_team",
    "away_team",
    "odds_home",
    "odds_draw",
    "odds_away",
]
JSONL_CONTENT_TYPE = "application/x-ndjson"
JSON_CONTENT_TYPE = "application/json"
CSV_CONTENT_TYPE = "text/csv"
# Overwritten blobs are cached; keep the index cache short (60 s is the minimum)
INDEX_CACHE_MAX_AGE = 60
RETRY_BACKOFF_SECONDS = 2.0
MAX_RETRY_WAIT_SECONDS = 10.0
# Backfill index updates are read back; a fresh read can lag behind a write
INDEX_VERIFY_ATTEMPTS = 4
INDEX_VERIFY_WAIT_SECONDS = 30.0
# Pauses before each read-back after a put (~45 s in all). An overwritten blob
# can serve its old content for 15 s and more, even under the new etag
# (observed 10.11.2026); polling instead of re-putting lets it settle
READBACK_DELAYS = (3.0, 6.0, 12.0, 24.0)

# Tue/Fri, from shortly before the 03:00 UTC cron until delayed runs are done
CRON_WEEKDAYS = {1, 4}
CRON_WINDOW_START = 2
CRON_WINDOW_END = 17

# Errors where a retry cannot help
_NO_RETRY_ERRORS = (
    vercel_blob.BlobAccessError,
    vercel_blob.BlobCredentialsError,
    vercel_blob.BlobPreconditionFailedError,
    vercel_blob.BlobStoreNotFoundError,
    vercel_blob.BlobStoreSuspendedError,
)


def is_already_exists(error: Exception) -> bool:
    """Tell whether a put was rejected because the pathname already exists.

    The SDK has no dedicated error class for this: the API answers with a 400
    (``bad_request``) whose message reads "This blob already exists, use
    allowOverwrite ...", which the SDK raises as a plain ``BlobError``.
    """
    return (
        isinstance(error, vercel_blob.BlobError)
        and getattr(error, "status_code", None) in (400, 409)
        and "already exists" in str(error).lower()
    )


def near_cron(moment: datetime) -> bool:
    """Whether ``moment`` falls into the workflow window (Tue/Fri, UTC).

    The cron fires at 03:00 UTC, but GitHub often starts it hours late (runs
    in the index start up to ~16:00 UTC), so manual scripts that write the
    index avoid the whole window.
    """
    utc = moment.astimezone(UTC)
    return utc.weekday() in CRON_WEEKDAYS and (
        CRON_WINDOW_START <= utc.hour < CRON_WINDOW_END
    )


# %% --------------------------------------------
# * Pathnames


def format_run_ts(moment: datetime) -> str:
    """Format a moment as the file name timestamp used throughout the archive."""
    return moment.astimezone(UTC).strftime(RUN_TS_FORMAT)


def format_payload_ts(moment: datetime) -> str:
    """Format a moment as the payload timestamp used inside the JSONL lines."""
    return moment.astimezone(UTC).strftime(PAYLOAD_TS_FORMAT)


def league_key(sport: str) -> str:
    """Return the league key used in file names, e.g. ``Bundesliga-Germany``."""
    return sport.replace(" ", "")


def provider_key(base_key: str) -> str:
    """Return the index/manifest key of a prediction base key."""
    return base_key.removeprefix(PREDICTIONS_PREFIX)


def predictions_pathname(timestamp: str, base_key: str) -> str:
    """Return the pathname of a prediction file (timestamp as in the KV key)."""
    return f"predictions/{timestamp}_{base_key}.jsonl"


def backfill_predictions_pathname(run_ts: str, base_key: str) -> str:
    """Return the pathname of a backfilled prediction file of a past run."""
    return f"{BACKFILL_PREFIX}{run_ts}_{base_key}.jsonl"


def parse_backfill_pathname(pathname: str) -> tuple[str, str] | None:
    """Return ``(run_ts, provider_key)`` of a backfill pathname, else None."""
    if not pathname.startswith(BACKFILL_PREFIX) or not pathname.endswith(".jsonl"):
        return None
    name = pathname.removeprefix(BACKFILL_PREFIX).removesuffix(".jsonl")
    run_ts, _, key = name.partition(f"_{PREDICTIONS_PREFIX}")
    try:
        datetime.strptime(run_ts, RUN_TS_FORMAT)  # noqa: DTZ007
    except ValueError:
        return None
    return (run_ts, key) if key else None


def odds_pathname(timestamp: str, sport: str) -> str:
    """Return the pathname of the odds file of one league and run."""
    return f"odds/{timestamp}_Match_Odds_{league_key(sport)}.jsonl"


def reconstructed_odds_pathname(timestamp: str) -> str:
    """Return the pathname of a reconstructed (estimated) odds CSV."""
    return f"odds/reconstructed/{timestamp}_Match_Odds_Reconstructed.csv"


def manifest_pathname(run_ts: str) -> str:
    """Return the pathname of the manifest of one run."""
    return f"runs/{run_ts}.json"


# %% --------------------------------------------
# * Payloads


def build_prediction_payload(
    prediction_data: dict[str, list[dict[str, Any]]],
    timestamp: str,
) -> list[dict[str, Any]]:
    """Build the prediction payload shared by KV, file and Blob.

    Parameters
    ----------
    prediction_data : dict[str, list[dict[str, Any]]]
        Matches per league.
    timestamp : str
        Payload timestamp in ``YYYY-MM-DD HH:MM:SS`` (UTC).

    Returns
    -------
    list[dict[str, Any]]
        One ``{"name", "timestamp", "matches"}`` entry per league.

    """
    return [
        {"name": sport, "timestamp": timestamp, "matches": matches}
        for sport, matches in prediction_data.items()
    ]


def combine_odds_sources(sources: list[str]) -> str:
    """Summarize per-match odds sources as ``logged``, ``reconstructed`` or ``mixed``.

    Raises
    ------
    ValueError
        If ``sources`` is empty or contains an unknown source.

    """
    unknown = set(sources) - set(ODDS_SOURCES)
    if not sources or unknown:
        msg = f"Expected odds sources from {ODDS_SOURCES}, got {sorted(set(sources))}"
        raise ValueError(msg)
    distinct = set(sources)
    return distinct.pop() if len(distinct) == 1 else "mixed"


def build_backfill_payload(
    prediction_data: dict[str, list[dict[str, Any]]],
    timestamp: str,
    *,
    odds_sources: dict[str, str],
    generated_at: str,
    script: str,
) -> list[dict[str, Any]]:
    """Build a backfill prediction payload: live lines plus a ``backfill`` marker.

    Parameters
    ----------
    prediction_data : dict[str, list[dict[str, Any]]]
        Matches per league, with the same fields as live prediction files.
    timestamp : str
        Payload timestamp of the original run (``YYYY-MM-DD HH:MM:SS``, UTC).
    odds_sources : dict[str, str]
        ``logged``, ``reconstructed`` or ``mixed`` per league.
    generated_at : str
        When the predictions were actually generated (UTC, payload format).
    script : str
        Name of the script that generated them.

    Returns
    -------
    list[dict[str, Any]]
        One ``{"name", "timestamp", "matches", "backfill"}`` entry per league.

    """
    payload = build_prediction_payload(prediction_data, timestamp)
    for entry in payload:
        entry["backfill"] = {
            "generated_at": generated_at,
            "odds_source": odds_sources[entry["name"]],
            "script": script,
        }
    return payload


def build_odds_payload(
    sport: str,
    timestamp: str,
    columns: list[str],
    matches: list[list[Any]],
) -> list[dict[str, Any]]:
    """Build the odds payload of one league (a single JSONL line)."""
    return [
        {"name": sport, "timestamp": timestamp, "columns": columns, "matches": matches}
    ]


def to_jsonl(payload: list[dict[str, Any]]) -> bytes:
    """Serialize a payload as JSONL, one line per entry (as ``_store_to_file``)."""
    return "".join(
        json.dumps(entry, ensure_ascii=False) + "\n" for entry in payload
    ).encode("utf-8")


def to_json(data: dict[str, Any]) -> bytes:
    """Serialize a manifest or index as indented JSON."""
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


def build_manifest(
    run_ts: str,
    odds: dict[str, str],
    predictions: dict[str, str],
    source: str = "workflow",
) -> dict[str, Any]:
    """Build the manifest linking a run's predictions to the odds they used."""
    return {
        "run_ts": run_ts,
        "source": source,
        "odds": dict(sorted(odds.items())),
        "predictions": dict(sorted(predictions.items())),
    }


def index_entry(manifest: dict[str, Any], *, manifest_written: bool) -> dict[str, Any]:
    """Turn a manifest into an index entry."""
    run_ts = manifest["run_ts"]
    return {
        "run_ts": run_ts,
        "source": manifest.get("source", "workflow"),
        "manifest": manifest_pathname(run_ts) if manifest_written else None,
        "odds": manifest["odds"],
        "predictions": manifest["predictions"],
    }


def empty_index() -> dict[str, Any]:
    """Return an index without runs."""
    return {"updated_at": None, "runs": [], "reconstructed_odds": []}


def merge_index(
    index: dict[str, Any] | None,
    entries: list[dict[str, Any]],
    reconstructed: list[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Merge run entries into an index, sorted by ``run_ts`` (newest last).

    Entries with the same ``run_ts`` are combined, so re-running a backfill or
    retrying an index update never duplicates a run. ``reconstructed_odds`` is
    sorted too: with several CSVs, the last one (newest timestamp) is current.
    """
    merged = empty_index() if index is None else dict(index)
    runs: dict[str, dict[str, Any]] = {}
    for entry in [*merged.get("runs", []), *entries]:
        run = runs.setdefault(
            entry["run_ts"],
            {
                "run_ts": entry["run_ts"],
                "source": entry.get("source", "workflow"),
                "manifest": None,
                "odds": {},
                "predictions": {},
            },
        )
        run["manifest"] = entry.get("manifest") or run["manifest"]
        run["odds"] = dict(sorted({**run["odds"], **entry.get("odds", {})}.items()))
        run["predictions"] = dict(
            sorted({**run["predictions"], **entry.get("predictions", {})}.items())
        )
    merged["runs"] = [runs[run_ts] for run_ts in sorted(runs)]
    merged["reconstructed_odds"] = sorted(
        {*merged.get("reconstructed_odds", []), *(reconstructed or [])}
    )
    merged["updated_at"] = format_payload_ts(now or datetime.now(tz=UTC))
    return merged


def merge_backfill_index(
    index: dict[str, Any] | None,
    entries: dict[str, dict[str, str]],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Merge backfill files into ``backfill_predictions`` (``runs`` untouched).

    Parameters
    ----------
    index : dict[str, Any] | None
        The current index (None for a new one).
    entries : dict[str, dict[str, str]]
        ``{run_ts: {provider_key: pathname}}``.
    now : datetime | None, optional
        Time for ``updated_at``, by default the current time.

    Returns
    -------
    dict[str, Any]
        The merged index; runs and provider keys are sorted.

    """
    merged = empty_index() if index is None else dict(index)
    backfill: dict[str, dict[str, str]] = {
        run_ts: dict(files) for run_ts, files in merged.get(BACKFILL_KEY, {}).items()
    }
    for run_ts, files in entries.items():
        backfill.setdefault(run_ts, {}).update(files)
    merged[BACKFILL_KEY] = {
        run_ts: dict(sorted(files.items()))
        for run_ts, files in sorted(backfill.items())
    }
    merged["updated_at"] = format_payload_ts(now or datetime.now(tz=UTC))
    return merged


def merge_results_index(
    index: dict[str, Any] | None,
    entries: dict[str, dict[str, Any]],
    now: datetime | None = None,
    *,
    key: str = RESULTS_KEY,
) -> dict[str, Any]:
    """Merge results files into ``results`` (``{pathname: {rows, updated_at}}``).

    Per pathname the entry with the newer ``updated_at`` wins (ties go to
    ``entries``), so carrying entries over into an index read that lags
    behind never replaces a newer row count with an older one. ``key``
    selects the section (``stats`` for the prediction tables).
    """
    merged = empty_index() if index is None else dict(index)
    results: dict[str, dict[str, Any]] = {
        path: dict(entry) for path, entry in merged.get(key, {}).items()
    }
    for path, entry in entries.items():
        current = results.get(path)
        if current is None or str(entry.get("updated_at") or "") >= str(
            current.get("updated_at") or ""
        ):
            results[path] = dict(entry)
    merged[key] = dict(sorted(results.items()))
    merged["updated_at"] = format_payload_ts(now or datetime.now(tz=UTC))
    return merged


def merge_results_scan(
    index: dict[str, Any] | None,
    scan: dict[str, dict[str, Any] | None],
    now: datetime | None = None,
    *,
    key: str = RESULTS_SCAN_KEY,
) -> dict[str, Any]:
    """Set the results step's scan state per season (``results_scan``).

    ``scan`` maps a season (``2026/27``) to its state, which replaces the
    stored one; None removes the season's state (the next run scans fully).
    See ``results_archive.plan_scan`` for the fields. ``key`` selects the
    section (``stats_scan`` for the prediction tables).
    """
    merged = empty_index() if index is None else dict(index)
    if not scan:
        return merged
    current = merged.get(key)
    states = dict(current) if isinstance(current, dict) else {}
    for season, state in scan.items():
        if state is None:
            states.pop(season, None)
        else:
            states[season] = state
    merged[key] = dict(sorted(states.items()))
    merged["updated_at"] = format_payload_ts(now or datetime.now(tz=UTC))
    return merged


def index_pathnames(index: dict[str, Any]) -> set[str]:
    """Return every pathname an index refers to."""
    pathnames = set(index.get("reconstructed_odds", []))
    pathnames.update(index.get(RESULTS_KEY, {}))
    pathnames.update(index.get(STATS_KEY, {}))
    for files in index.get(BACKFILL_KEY, {}).values():
        pathnames.update(files.values())
    for run in index.get("runs", []):
        if run.get("manifest"):
            pathnames.add(run["manifest"])
        pathnames.update(run.get("odds", {}).values())
        pathnames.update(run.get("predictions", {}).values())
    return pathnames


# %% --------------------------------------------
# * Blob I/O


def put_with_retry(
    pathname: str,
    body: bytes,
    *,
    content_type: str,
    allow_overwrite: bool = False,
    cache_control_max_age: int | None = None,
    backoff: float = RETRY_BACKOFF_SECONDS,
) -> bool:
    """Upload one file to the private store, with at most one retry.

    Never raises: a failure is logged as a WARNING with pathname and HTTP status.
    A write-once put rejected because the pathname already exists counts as
    success (the file is present) and is neither retried nor logged as WARNING.

    Returns
    -------
    bool
        True if the file was written or already exists.

    """
    for attempt in (1, 2):
        try:
            vercel_blob.put_blob(
                pathname,
                body,
                access="private",
                content_type=content_type,
                add_random_suffix=False,
                allow_overwrite=allow_overwrite,
                cache_control_max_age=cache_control_max_age,
            )
        except Exception as e:
            status = getattr(e, "status_code", None)
            if not allow_overwrite and is_already_exists(e):
                logger.debug("Blob %s already exists, not overwritten", pathname)
                return True
            if attempt == 1 and not isinstance(e, _NO_RETRY_ERRORS):
                wait = max(backoff, float(getattr(e, "retry_after", 0) or 0))
                logger.info(
                    "Blob write failed for %s (HTTP %s), retrying once",
                    pathname,
                    status,
                )
                time.sleep(min(wait, MAX_RETRY_WAIT_SECONDS))
                continue
            logger.warning(
                "Blob write failed for %s (HTTP %s): %s", pathname, status, e
            )
            return False
        else:
            logger.debug("Archived %s in Vercel Blob", pathname)
            return True
    return False


def blob_exists(pathname: str) -> bool:
    """Check whether a pathname exists, using a cheap HEAD (simple operation).

    A transient failure (rate limit, network) returns False, so the caller
    attempts the write-once upload, which reports an existing file as success.
    """
    try:
        vercel_blob.head_blob(pathname)
    except vercel_blob.BlobNotFoundError:
        return False
    except Exception as e:
        logger.info(
            "HEAD failed for %s (HTTP %s), trying the upload: %s",
            pathname,
            getattr(e, "status_code", None),
            e,
        )
        return False
    return True


def read_fresh(pathname: str) -> bytes | None:
    """Download a file bypassing the delivery cache; None if it does not exist.

    HEAD (uncached catalog API) gives the current etag, and the download uses
    it as a query parameter, i.e. a new cache key. Use this for files that are
    overwritten in place (``index.json``, the results CSVs), which the CDN can
    otherwise serve stale for up to a month.

    Raises
    ------
    vercel_blob.BlobError
        For any failure other than a missing file.

    """
    try:
        head = vercel_blob.head_blob(pathname)
        target = f"{head.url}?etag={quote(head.etag, safe='')}"
        return vercel_blob.get_blob(target, access="private").body
    except vercel_blob.BlobNotFoundError:
        return None


def read_index(*, fresh: bool = False) -> dict[str, Any]:
    """Read ``index.json``; return an empty index if it does not exist yet.

    Parameters
    ----------
    fresh : bool, optional
        Bypass the delivery cache (see ``read_fresh``). Use this right before
        a read-modify-write of the index.

    Raises
    ------
    vercel_blob.BlobError
        For any failure other than a missing index, so callers never overwrite
        an index they could not read.

    """
    if fresh:
        body = read_fresh(INDEX_PATHNAME)
    else:
        try:
            body = vercel_blob.get_blob(INDEX_PATHNAME, access="private").body
        except vercel_blob.BlobNotFoundError:
            body = None
    return empty_index() if body is None else json.loads(body)


def update_index(  # noqa: PLR0913
    entries: list[dict[str, Any]],
    reconstructed: list[str] | None = None,
    results: dict[str, dict[str, Any]] | None = None,
    results_scan: dict[str, dict[str, Any] | None] | None = None,
    stats: dict[str, dict[str, Any]] | None = None,
    stats_scan: dict[str, dict[str, Any] | None] | None = None,
    *,
    expected: dict[str, Any] | None = None,
    attempts: int = INDEX_VERIFY_ATTEMPTS,
    wait: float = INDEX_VERIFY_WAIT_SECONDS,
) -> bool:
    """Append run entries to ``index.json``, verified.

    ``results`` (``{pathname: {rows, updated_at}}``, e.g. what this run's
    results step verified) is merged in as well, so an index read that lags
    behind that step does not drop its entries. ``results_scan`` (the results
    step's scan state per season) is set the same way, and so are ``stats``
    and ``stats_scan`` (the prediction tables and their scan state).

    ``expected`` is an index the caller read earlier (the workflow: the one
    read at the start of the run). A fresh read that lacks any of its
    pathnames is stale and never overwritten. Each attempt puts the merge and
    polls the read-back (``_verified_index_update``); success needs every run,
    pathname, row count and scan state of this call in the index.

    Never raises; a failure is logged as a WARNING. Runs can be re-added later
    from their manifests with ``utils/backfill_blob_archive.py --rebuild-index``.
    The read-modify-write is not atomic: never run the backfill or
    ``--rebuild-index`` near the cron (Tue/Fri 03:00 UTC).
    """
    run_ts = {entry["run_ts"] for entry in entries}
    wanted = index_pathnames(merge_index(None, entries, reconstructed))
    known = index_pathnames(expected) if expected else set()
    sections = [
        (RESULTS_KEY, results or {}, RESULTS_SCAN_KEY, results_scan or {}),
        (STATS_KEY, stats or {}, STATS_SCAN_KEY, stats_scan or {}),
    ]

    def merge(index: dict[str, Any]) -> dict[str, Any]:
        merged = merge_index(index, entries, reconstructed)
        for key, files, scan_key, scan in sections:
            if files:
                merged = merge_results_index(merged, files, key=key)
            if scan:
                merged = merge_results_scan(merged, scan, key=scan_key)
        return merged

    def missing(index: dict[str, Any]) -> int:
        count = len(run_ts - {run.get("run_ts") for run in index.get("runs", [])})
        count += len(wanted - index_pathnames(index))
        for key, files, scan_key, scan in sections:
            current = index.get(key, {})
            for path, entry in files.items():
                found = current.get(path)
                # A newer entry (another writer) counts as present
                count += found is None or (
                    found.get("rows") != entry.get("rows")
                    and str(found.get("updated_at") or "")
                    <= str(entry.get("updated_at") or "")
                )
            states = index.get(scan_key)
            states = states if isinstance(states, dict) else {}
            count += sum(states.get(season) != state for season, state in scan.items())
        return count

    ok = _verified_index_update(
        merge,
        missing,
        lambda index: len(known - index_pathnames(index)),
        label=f"run {', '.join(sorted(run_ts)) or '(no run)'}",
        attempts=attempts,
        wait=wait,
    )
    if not ok:
        logger.warning(
            "Run index update NOT verified after %d attempts (%s may lack these "
            "entries); re-add runs with utils/backfill_blob_archive.py "
            "--rebuild-index",
            attempts,
            INDEX_PATHNAME,
        )
    return ok


def await_readback(
    check: Callable[[], bool],
    pathname: str,
    delays: tuple[float, ...] = READBACK_DELAYS,
) -> bool:
    """Poll ``check`` (a fresh read-back) after each pause in ``delays``.

    Returns True as soon as ``check`` does (logged at INFO when it took more
    than one read, to keep an eye on the lag). Never raises.
    """
    waited = 0.0
    for number, delay in enumerate(delays, start=1):
        time.sleep(delay)
        waited += delay
        if check():
            if number > 1:
                logger.info(
                    "%s read-back current after %.0f s (%d reads)",
                    pathname,
                    waited,
                    number,
                )
            return True
    return False


def _read_index_quietly() -> dict[str, Any] | None:
    """Read ``index.json`` fresh; log and return None on failure."""
    try:
        return read_index(fresh=True)
    except Exception as e:
        logger.warning(
            "Could not read %s (HTTP %s): %s",
            INDEX_PATHNAME,
            getattr(e, "status_code", None),
            e,
        )
        return None


def _verified_index_update(
    merge: Callable[[dict[str, Any]], dict[str, Any]],
    missing: Callable[[dict[str, Any]], int],
    stale: Callable[[dict[str, Any]], int],
    *,
    label: str,
    attempts: int,
    wait: float,
) -> bool:
    """Read-modify-write ``index.json`` with a stale guard and a read-back check.

    Each attempt reads the index fresh; ``stale(index)`` counts what the caller
    saw earlier but the read lacks (then nothing is written, as a stale read
    would overwrite newer entries). Otherwise it puts ``merge(index)`` and
    polls the read-back (``await_readback``); success needs
    ``missing(read_back) == 0``. Between attempts it waits ``wait`` seconds.
    Never raises.
    """
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            time.sleep(wait)
        index = _read_index_quietly()
        if index is None:
            continue
        if lagging := stale(index):
            logger.warning(
                "%s looks stale (attempt %d/%d): %d known files missing, not "
                "overwriting",
                INDEX_PATHNAME,
                attempt,
                attempts,
                lagging,
            )
            continue
        if not missing(index):
            return True  # already indexed
        if not put_with_retry(
            INDEX_PATHNAME,
            to_json(merge(index)),
            content_type=JSON_CONTENT_TYPE,
            allow_overwrite=True,
            cache_control_max_age=INDEX_CACHE_MAX_AGE,
        ):
            continue

        def current() -> bool:
            check = _read_index_quietly()
            return check is not None and not missing(check)

        if await_readback(current, INDEX_PATHNAME):
            return True
        logger.warning(
            "%s read-back misses %s (attempt %d/%d), re-merging",
            INDEX_PATHNAME,
            label,
            attempt,
            attempts,
        )
    return False


def update_backfill_index(
    entries: dict[str, dict[str, str]],
    *,
    expected: set[str] | None = None,
    attempts: int = INDEX_VERIFY_ATTEMPTS,
    wait: float = INDEX_VERIFY_WAIT_SECONDS,
) -> bool:
    """Add backfill files to ``index.json`` under ``backfill_predictions``, verified.

    A fresh read can return a stale index shortly after another write
    (observed 04.11.2026, cause unconfirmed). So each attempt reads the index,
    treats it as stale if it lacks any pathname in ``expected`` (what the
    caller saw earlier, so a stale read never overwrites newer entries), puts
    the merge and reads it back; only an index containing ``entries`` and
    ``expected`` counts as success. Between attempts it waits ``wait`` seconds.

    Never raises; a failure is logged as a WARNING. Lost entries can be re-added
    with ``utils/backfill_blob_archive.py --rebuild-index``. Same timing rule as
    ``update_index``: never run near the cron (Tue/Fri 03:00 UTC).

    Returns
    -------
    bool
        True once a read-back shows every entry.

    """
    wanted = {path for files in entries.values() for path in files.values()}
    known = expected or set()
    ok = _verified_index_update(
        lambda index: merge_backfill_index(index, entries),
        lambda index: len((wanted | known) - index_pathnames(index)),
        lambda index: len(known - index_pathnames(index)),
        label=f"{len(wanted)} backfill files",
        attempts=attempts,
        wait=wait,
    )
    if ok:
        logger.info("Indexed %d backfill files (verified)", len(wanted))
    else:
        logger.warning(
            "Backfill index update NOT verified after %d attempts; re-run the "
            "script or backfill_blob_archive.py --rebuild-index",
            attempts,
        )
    return ok


def update_results_index(
    entries: dict[str, dict[str, Any]],
    *,
    expected: dict[str, Any] | None = None,
    scan: dict[str, dict[str, Any] | None] | None = None,
    attempts: int = INDEX_VERIFY_ATTEMPTS,
    wait: float = INDEX_VERIFY_WAIT_SECONDS,
    entries_key: str = RESULTS_KEY,
    scan_key: str = RESULTS_SCAN_KEY,
) -> bool:
    """Set ``results`` entries in ``index.json`` (``{path: {rows, updated_at}}``).

    ``scan`` (scan state per season, see ``merge_results_scan``) is written
    along with the entries and is part of the read-back check, so a reset
    (None) is written even when every row count is already indexed.

    ``expected`` is the index the caller read earlier: a read that lacks any of
    its pathnames, or lists fewer rows for a results file than it did, is
    stale and never overwritten. Success needs a read-back showing every entry
    with its row count. Never raises; a failure is logged as a WARNING (the
    next run re-adds the entries, since it compares the CSVs to the index).
    ``entries_key``/``scan_key`` select the sections (``update_stats_index``).
    """
    known = index_pathnames(expected) if expected else set()
    known_rows = {
        path: int(entry.get("rows", 0))
        for path, entry in (expected or {}).get(entries_key, {}).items()
    }

    def stale(index: dict[str, Any]) -> int:
        current = index.get(entries_key, {})
        lower = {
            path
            for path, rows in known_rows.items()
            if path not in entries and int(current.get(path, {}).get("rows", 0)) < rows
        }
        return len((known - index_pathnames(index)) | lower)

    def missing(index: dict[str, Any]) -> int:
        current = index.get(entries_key, {})
        states = index.get(scan_key)
        states = states if isinstance(states, dict) else {}
        return sum(
            current.get(path, {}).get("rows") != entry["rows"]
            for path, entry in entries.items()
        ) + sum(states.get(season) != state for season, state in (scan or {}).items())

    ok = _verified_index_update(
        lambda index: merge_results_scan(
            merge_results_index(index, entries, key=entries_key),
            scan or {},
            key=scan_key,
        ),
        missing,
        stale,
        label=f"{len(entries)} {entries_key} files",
        attempts=attempts,
        wait=wait,
    )
    if not ok:
        logger.warning(
            "Index update of %s NOT verified after %d attempts; the next run "
            "re-adds the entries",
            entries_key,
            attempts,
        )
    return ok


def update_stats_index(
    entries: dict[str, dict[str, Any]],
    *,
    expected: dict[str, Any] | None = None,
    scan: dict[str, dict[str, Any] | None] | None = None,
    attempts: int = INDEX_VERIFY_ATTEMPTS,
    wait: float = INDEX_VERIFY_WAIT_SECONDS,
) -> bool:
    """Set ``stats`` entries and ``stats_scan`` state in ``index.json``, verified.

    Same guards as ``update_results_index`` (stale reads are never
    overwritten, success needs a read-back showing every entry and state).
    Used by ``utils/backfill_stats.py``; the workflow carries its entries
    into ``update_index`` instead. Never raises.
    """
    return update_results_index(
        entries,
        expected=expected,
        scan=scan,
        attempts=attempts,
        wait=wait,
        entries_key=STATS_KEY,
        scan_key=STATS_SCAN_KEY,
    )


class BlobArchiveReader:
    """Read the archive through ``index.json``, without calling ``list``.

    Examples
    --------
    >>> reader = BlobArchiveReader()
    >>> for run in reader.runs():
    ...     odds = {k: reader.fetch_jsonl(p) for k, p in run["odds"].items()}

    """

    def __init__(self) -> None:
        """Initialize the reader; the index is loaded lazily."""
        self._index: dict[str, Any] | None = None

    def load_index(self, *, refresh: bool = False) -> dict[str, Any]:
        """Load (and cache) ``index.json``."""
        if self._index is None or refresh:
            self._index = read_index()
        return self._index

    def runs(self) -> list[dict[str, Any]]:
        """Return all archived runs, oldest first."""
        return self.load_index()["runs"]

    def backfill_predictions(self) -> dict[str, dict[str, str]]:
        """Return backfilled prediction files, ``{run_ts: {provider_key: path}}``.

        These were generated after the fact for past runs; never treat them as
        live pre-match predictions.
        """
        return self.load_index().get(BACKFILL_KEY, {})

    def fetch_bytes(self, pathname: str) -> bytes:
        """Download one archived file."""
        return vercel_blob.get_blob(pathname, access="private").body

    def fetch_json(self, pathname: str) -> dict[str, Any]:
        """Download a JSON file such as a manifest."""
        return json.loads(self.fetch_bytes(pathname))

    def fetch_jsonl(self, pathname: str) -> list[dict[str, Any]]:
        """Download a prediction or odds file, one dict per league."""
        lines = self.fetch_bytes(pathname).decode("utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]


class MemoryArchiveReader(BlobArchiveReader):
    """``BlobArchiveReader`` that keeps downloaded files in memory.

    Lets two workflow steps share reads of write-once files (prediction and
    odds files) within one run. Never use it for ``index.json``, the results
    CSVs or the prediction tables, which are overwritten in place.
    """

    def __init__(self) -> None:
        """Initialize with an empty cache."""
        super().__init__()
        self.cache: dict[str, bytes] = {}
        self.downloads = 0

    def fetch_bytes(self, pathname: str) -> bytes:
        """Download one file once; later calls return the cached body."""
        if pathname not in self.cache:
            self.cache[pathname] = super().fetch_bytes(pathname)
            self.downloads += 1
        return self.cache[pathname]


class CachedArchiveReader(BlobArchiveReader):
    """``BlobArchiveReader`` with a disk cache for downloaded files.

    Only for write-once files (run files, predictions, odds): a cached copy
    never goes stale. Never use it for ``index.json`` or the results CSVs.
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
            # Write then rename, so an interrupted run never leaves a partial file
            tmp = cached.with_name(f"{cached.name}.tmp")
            tmp.write_bytes(body)
            tmp.replace(cached)
        return body
