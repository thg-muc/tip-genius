"""Helpers for the long-term archive of predictions and odds in Vercel Blob.

The archive lives in a private Vercel Blob store and is laid out flat by type:

- ``predictions/<YYYY-MM-DD_HH-MM-SS>_<base_key>.jsonl``
- ``odds/<YYYY-MM-DD_HH-MM-SS>_Match_Odds_<League-Key>.jsonl``
- ``odds/reconstructed/<YYYY-MM-DD_HH-MM-SS>_Match_Odds_Reconstructed.csv``
- ``runs/<run_ts>.json`` (one manifest per workflow run)
- ``index.json`` (every archived run, so readers never need ``list``)

Every file is write-once except ``index.json``. With several reconstructed
CSVs, the newest one (highest timestamp, last in ``reconstructed_odds``) is the
current one. The ``index.json`` read-modify-write is not atomic, so never run
the backfill or ``--rebuild-index`` near the cron (Tue/Fri 03:00 UTC). The SDK
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
from typing import Any
from urllib.parse import quote

from vercel.blob import sync as vercel_blob

logger = logging.getLogger(__name__)

# %% --------------------------------------------
# * Constants

RUN_TS_FORMAT = "%Y-%m-%d_%H-%M-%S"
PAYLOAD_TS_FORMAT = "%Y-%m-%d %H:%M:%S"
INDEX_PATHNAME = "index.json"
PREDICTIONS_PREFIX = "Match_Predictions_"
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


def index_pathnames(index: dict[str, Any]) -> set[str]:
    """Return every pathname an index refers to."""
    pathnames = set(index.get("reconstructed_odds", []))
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


def read_index(*, fresh: bool = False) -> dict[str, Any]:
    """Read ``index.json``; return an empty index if it does not exist yet.

    Parameters
    ----------
    fresh : bool, optional
        Bypass the delivery cache: HEAD (uncached catalog API) gives the current
        etag, and the download uses it as a query parameter, i.e. a new cache
        key. Use this right before a read-modify-write of the index.

    Raises
    ------
    vercel_blob.BlobError
        For any failure other than a missing index, so callers never overwrite
        an index they could not read.

    """
    try:
        if fresh:
            head = vercel_blob.head_blob(INDEX_PATHNAME)
            target = f"{head.url}?etag={quote(head.etag, safe='')}"
        else:
            target = INDEX_PATHNAME
        result = vercel_blob.get_blob(target, access="private")
    except vercel_blob.BlobNotFoundError:
        return empty_index()
    return json.loads(result.body)


def update_index(
    entries: list[dict[str, Any]],
    reconstructed: list[str] | None = None,
) -> bool:
    """Append run entries to ``index.json`` (the only overwritten file).

    Never raises; a failure is logged as a WARNING. Runs can be re-added later
    from their manifests with ``utils/backfill_blob_archive.py --rebuild-index``.
    The read-modify-write is not atomic: never run the backfill or
    ``--rebuild-index`` near the cron (Tue/Fri 03:00 UTC).
    """
    try:
        index = read_index(fresh=True)
    except Exception as e:
        logger.warning(
            "Could not read %s (HTTP %s), index not updated: %s",
            INDEX_PATHNAME,
            getattr(e, "status_code", None),
            e,
        )
        return False
    merged = merge_index(index, entries, reconstructed)
    return put_with_retry(
        INDEX_PATHNAME,
        to_json(merged),
        content_type=JSON_CONTENT_TYPE,
        allow_overwrite=True,
        cache_control_max_age=INDEX_CACHE_MAX_AGE,
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
