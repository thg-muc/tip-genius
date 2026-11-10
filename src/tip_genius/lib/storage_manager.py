"""Module to handle storage operations for file system, Vercel KV and Vercel Blob."""
# * Author(s): Thomas Glanzer
# * Creation : Nov 2024
# * License: MIT license

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
import yaml
from lib import blob_archive, dashboard, results_archive, stats

# Set up logging
logger = logging.getLogger(__name__)

# %% --------------------------------------------
# * Class Definitions


class StorageManager:
    """Handles storage operations for match predictions data.

    This class provides functionality to store prediction data in the local
    file system and in Vercel KV storage, and to archive predictions and odds
    in a private Vercel Blob store (an additional, write-once copy; KV stays the
    live store).

    Parameters
    ----------
    match_predictions_folder : str
        Path to the folder where JSON files will be stored
    debug : bool, optional
        Flag to indicate debug mode, by default False
    write_to_kv : bool, optional
        Flag to enable Vercel KV storage, by default False
    export_to_file : bool, optional
        Flag to enable writing of result to file system, by default False.
    write_to_blob : bool, optional
        Flag to enable the Vercel Blob archive, by default False. Skipped in
        debug mode and when the Blob token is missing.
    run_started_at : datetime, optional
        Start of the workflow run, used for the run timestamp of odds files,
        the run manifest and the index entry. Defaults to now.

    Attributes
    ----------
    kv_initialized : bool
        Indicates whether KV storage is properly configured and available
    blob_initialized : bool
        Indicates whether Blob archiving is enabled and configured
    run_ts : str
        Run timestamp (``YYYY-MM-DD_HH-MM-SS``, UTC), set once per run

    """

    def __init__(
        self,
        match_predictions_folder: str,
        *,
        debug: bool = False,
        write_to_kv: bool = False,
        export_to_file: bool = False,
        write_to_blob: bool = False,
        run_started_at: datetime | None = None,
    ) -> None:
        """Initialize the StorageManager with the given parameters."""
        self.match_predictions_folder = match_predictions_folder
        self.debug = debug
        self.write_to_kv = write_to_kv
        self.export_to_file = export_to_file
        self.write_to_blob = write_to_blob
        self.kv_initialized = False
        self.kv_url: str | None = None
        self.kv_token: str | None = None

        # One timestamp per run for odds files, manifest and index entry
        self.run_ts = blob_archive.format_run_ts(run_started_at or datetime.now(tz=UTC))
        # Pathnames actually written to Blob during this run, for the manifest
        self.blob_odds: dict[str, str] = {}
        self.blob_predictions: dict[str, str] = {}
        # Results index entries verified by this run's results step
        self.blob_results: dict[str, dict[str, Any]] = {}
        # Results scan state per season, stored once the step went through
        self.blob_results_scan: dict[str, dict[str, Any] | None] = {}
        # Prediction table index entries and scan state of the stats step
        self.blob_stats: dict[str, dict[str, Any]] = {}
        self.blob_stats_scan: dict[str, dict[str, Any] | None] = {}
        # Shared by the results and stats steps: each write-once file once
        self.blob_reader = blob_archive.MemoryArchiveReader()
        self.results_report: results_archive.ArchiveReport | None = None
        self.stats_kv_key: str | None = None

        # Initialize environment configuration
        self.kv_initialized = self._initialize_kv_config()
        self.blob_initialized = self._initialize_blob_config()

    @staticmethod
    def _load_config() -> dict[str, Any]:
        """Load the Vercel config file."""
        config_path = Path(__file__).parents[1] / "cfg" / "vercel_config.yaml"
        with config_path.open(encoding="utf-8") as f:
            return yaml.safe_load(f)["tip_genius"]

    def _initialize_blob_config(self) -> bool:
        """Check whether Blob archiving is enabled and the Blob token is set.

        Returns
        -------
        bool
            True if Blob writes should be attempted, False otherwise

        """
        if not self.write_to_blob:
            return False
        if self.debug:
            logger.info(
                "Debug mode: skipping Vercel Blob archive (predictions, odds, manifest)"
            )
            return False
        try:
            token_env_name = self._load_config()["blob_token_env_name"]
        except (FileNotFoundError, KeyError, yaml.YAMLError) as e:
            logger.warning("Vercel Blob archive failed to initialize: %s", str(e))
            return False
        if not os.environ.get(token_env_name):
            logger.warning(
                "Vercel Blob archive not configured: %s is not set, "
                "skipping all Blob writes",
                token_env_name,
            )
            return False
        logger.info("Vercel Blob archive configured successfully")
        return True

    def _initialize_kv_config(self) -> bool:
        """Initialize Vercel KV configuration from config file and env variables.

        The method will first try to load configuration from .env.local file,
        then fall back to system environment variables if needed.

        Sets kv_initialized to True if configuration is successful, False otherwise.

        Returns
        -------
        bool
            True if KV configuration is successful, False otherwise

        """
        try:
            # Load KV config
            config = self._load_config()

            # Get environment variables from .env.local or system environment
            self.kv_token = os.environ[config["kv_token_env_name"]]
            self.kv_url = os.environ[config["kv_url_env_name"]]

            if self.kv_token and self.kv_url:
                self.kv_initialized = True
                logger.info("Vercel KV storage configured successfully")
                return True
            logger.warning(
                "Vercel KV storage not configured: missing environment variables",
            )

        except (FileNotFoundError, KeyError, yaml.YAMLError) as e:
            logger.warning("Vercel KV storage failed to initialize: %s", str(e))

        return False

    def store_predictions(
        self,
        prediction_data: dict[str, list[dict[str, Any]]],
        base_key: str,
        full_export_path: str | None = None,
    ) -> bool:
        """Write predictions to file, Vercel KV and the Vercel Blob archive.

        The payload is built once and the same object goes to every target.
        Blob results do not affect the return value: a failed archive write
        must never fail the workflow.

        Parameters
        ----------
        prediction_data : dict[str, dict[str, list[dict[str, Any]]]]
            The prediction data to store
        base_key : str
            Base key name for the output
        full_export_path : str, optional
            Full path to the export directory. Required if export_to_file is True

        Returns
        -------
        bool
            True if all file and KV operations were successful, False if any failed

        """
        # Generate timestamps (key/file name and payload) from one moment
        now = datetime.now(tz=UTC)
        timestamp = blob_archive.format_run_ts(now)
        payload = blob_archive.build_prediction_payload(
            prediction_data, blob_archive.format_payload_ts(now)
        )
        all_successful = True

        # Generate filenames
        timestamped_filename = f"{timestamp}_{base_key}.jsonl"
        non_timestamped_filename = f"{base_key}.jsonl"
        # Store files locally if enabled
        if self.export_to_file:
            if full_export_path:
                # Create directory if it doesn't exist
                export_path = Path(full_export_path)
                export_path.mkdir(parents=True, exist_ok=True)
                file_result1 = self._store_to_file(
                    payload=payload,
                    file_path=export_path / timestamped_filename,
                )
                file_result2 = self._store_to_file(
                    payload=payload,
                    file_path=export_path / non_timestamped_filename,
                )
                all_successful = all_successful and file_result1 and file_result2
            else:
                logger.warning(
                    "Full export path not provided, predictions not stored locally.",
                )
                all_successful = False
        # Store in KV if enabled
        if self.write_to_kv:
            if self.kv_initialized:
                # Store both timestamped and non-timestamped versions
                kv_result1 = self._store_to_kv(payload, f"{timestamp}_{base_key}")
                kv_result2 = self._store_to_kv(payload, base_key)
                all_successful = all_successful and kv_result1 and kv_result2
            else:
                logger.warning("Vercel KV not configured, predictions not stored.")
                all_successful = False
        # Archive in Blob (timestamped only); deliberately not part of the result
        if self.blob_initialized:
            pathname = blob_archive.predictions_pathname(timestamp, base_key)
            if self._store_to_blob(payload, pathname):
                self.blob_predictions[blob_archive.provider_key(base_key)] = pathname

        return all_successful

    def _store_to_blob(
        self,
        payload: list[dict[str, Any]],
        pathname: str,
    ) -> bool:
        """Archive a JSONL payload in Vercel Blob (write-once).

        Parameters
        ----------
        payload : list[dict[str, Any]]
            One entry per league, written as one JSONL line each
        pathname : str
            Pathname in the Blob store

        Returns
        -------
        bool
            True if the file was written, False otherwise (logged as WARNING)

        """
        if not self.blob_initialized:
            return False
        return blob_archive.put_with_retry(
            pathname,
            blob_archive.to_jsonl(payload),
            content_type=blob_archive.JSONL_CONTENT_TYPE,
        )

    def store_odds(
        self,
        sport: str,
        columns: list[str],
        matches: list[list[Any]],
        fetched_at: datetime,
    ) -> bool:
        """Archive the processed odds of one league in Vercel Blob.

        Called once per league and run. The pathname uses the run timestamp,
        the payload timestamp is the fetch time.

        Parameters
        ----------
        sport : str
            The name of the sport/league
        columns : list[str]
            Column names of the match rows
        matches : list[list[Any]]
            One row per fixture
        fetched_at : datetime
            When the odds were fetched

        Returns
        -------
        bool
            True if the file was written, False otherwise

        """
        if not self.blob_initialized:
            return False
        pathname = blob_archive.odds_pathname(self.run_ts, sport)
        payload = blob_archive.build_odds_payload(
            sport, blob_archive.format_payload_ts(fetched_at), columns, matches
        )
        if self._store_to_blob(payload, pathname):
            self.blob_odds[blob_archive.league_key(sport)] = pathname
            return True
        return False

    def update_results_archive(self, now: datetime | None = None) -> bool:
        """Add the current season's missing match results to the Blob archive.

        Runs before the new odds and predictions are fetched. Reads only the
        prediction files added since the last complete run plus the fixtures
        still open then (``results_archive.plan_scan``); the new scan state is
        written with the results index entries and again by
        ``finalize_blob_archive``. Never raises and never affects the exit
        code: a failure only logs a WARNING, and the next run repeats the
        scan from the old state (gap detection).

        Returns
        -------
        bool
            True if the update went through completely.

        """
        if not self.blob_initialized:
            return False
        moment = now or datetime.now(tz=UTC)
        try:
            report = results_archive.update_results_archive(
                {results_archive.season_of(moment)},
                now=moment,
                fetch_jsonl=self.blob_reader.fetch_jsonl,
                incremental=True,
                attempts=results_archive.CRON_ATTEMPTS,
                wait=results_archive.CRON_WAIT_SECONDS,
            )
        except Exception:
            logger.warning("Could not update the Blob results archive", exc_info=True)
            return False
        self.results_report = report
        # Entries of verified CSVs and the state go into the run's index
        # update too, also when this step's own index update was not verified
        # (newer entries win the merge, so carrying them over is safe)
        self.blob_results = dict(report.index_entries)
        if report.scan_state:
            # None after an unreadable prediction file (see _next_scan_state)
            self.blob_results_scan = report.scan_state
        return report.ok

    def update_dashboard_stats(
        self, active_models: list[str], now: datetime | None = None
    ) -> bool:
        """Update the prediction table and write the dashboard statistics to KV.

        Runs right after ``update_results_archive`` and before the new odds
        are fetched. Brings the current season's prediction table in Blob up
        to date (``lib.prediction_table``, incremental), reads the other
        seasons' tables and the results CSVs, computes the statistics JSON
        (``lib.stats``) from Blob data only and writes it to KV under the
        configured key. Reuses the index, results CSVs and prediction files
        the results step already read. The table's index entry and scan state
        are written by ``finalize_blob_archive``. Never raises and never
        affects the exit code: a failure only logs a WARNING and the previous
        JSON stays in KV.

        Parameters
        ----------
        active_models : list[str]
            Models the workflow currently runs (``llm_provider_options``).
        now : datetime | None, optional
            Current time.

        Returns
        -------
        bool
            True if the table update went through and the JSON was written.

        """
        if not self.blob_initialized:
            return False
        moment = now or datetime.now(tz=UTC)
        season = results_archive.season_of(moment)
        try:
            report = self.results_report
            index = report.index if report is not None else None
            if index is None:
                index = blob_archive.read_index(fresh=True)
            elif report is not None and report.index_entries:
                index = blob_archive.merge_results_index(index, report.index_entries)
            full = report is not None and report.scan_modes.get(season) == "full"
            inputs = dashboard.collect_inputs(
                index,
                now=moment,
                seasons=dashboard.seasons_since(stats.FROM_SEASON, moment),
                update={season},
                fetch_jsonl=self.blob_reader.fetch_jsonl,
                fetch_bytes=self.blob_reader.fetch_bytes,
                results_contents=dashboard.verified_results(report),
                force_full="results step scanned fully" if full else None,
                attempts=results_archive.CRON_ATTEMPTS,
                wait=results_archive.CRON_WAIT_SECONDS,
            )
            table = inputs.tables[season]
            if table.index_entry:
                self.blob_stats[table.pathname] = table.index_entry
            if table.scan_state:
                # Set only after the table is in Blob as computed; files it
                # could not read are listed for a retry
                self.blob_stats_scan[season] = table.scan_state
            logger.info(
                "Dashboard stats: table %s %s, %d prediction files downloaded this "
                "run (shared), %d odds files, ~%d reads of tables/results",
                season,
                table.status,
                self.blob_reader.downloads,
                table.odds_read,
                inputs.reads,
            )
            if table.unread:
                logger.warning(
                    "Prediction table %s: %d files unread, retried next run",
                    season,
                    len(table.unread),
                )
            if inputs.table_rows.get(season) is None or inputs.unread:
                logger.warning(
                    "Dashboard stats not updated: unreadable %s",
                    ", ".join(inputs.unread) or table.pathname,
                )
                return False
            _, body = dashboard.build_json(inputs, active=active_models, now=moment)
        except Exception:
            logger.warning("Could not compute the dashboard stats", exc_info=True)
            return False
        key = self.stats_kv_key or self._stats_kv_key()
        if not self.write_to_kv or not self.kv_initialized or not key:
            logger.info(
                "KV not enabled: dashboard stats (%d bytes) not stored", len(body)
            )
            return False
        if not self._store_to_kv(body, key):
            logger.warning("Could not write the dashboard stats to KV key %s", key)
            return False
        logger.info("Dashboard stats written to KV key %s (%d bytes)", key, len(body))
        return table.ok

    def _stats_kv_key(self) -> str | None:
        """Return the KV key of the dashboard statistics from the config."""
        try:
            self.stats_kv_key = self._load_config()["stats_kv_key"]
        except (FileNotFoundError, KeyError, yaml.YAMLError) as e:
            logger.warning("No dashboard stats KV key configured: %s", e)
            return None
        return self.stats_kv_key

    def finalize_blob_archive(self) -> None:
        """Write the run manifest and add the run to ``index.json``.

        Lists only the files actually written in this run, plus the index
        entries and scan states the results and stats steps verified. Without
        odds or prediction files no manifest is written, but pending stats
        entries still go to the index. Never raises.
        """
        if not self.blob_initialized:
            return
        has_files = bool(self.blob_odds or self.blob_predictions)
        if not (has_files or self.blob_stats or self.blob_stats_scan):
            return
        try:
            entries = []
            if has_files:
                manifest = blob_archive.build_manifest(
                    self.run_ts, self.blob_odds, self.blob_predictions
                )
                manifest_written = blob_archive.put_with_retry(
                    blob_archive.manifest_pathname(self.run_ts),
                    blob_archive.to_json(manifest),
                    content_type=blob_archive.JSON_CONTENT_TYPE,
                )
                entries.append(
                    blob_archive.index_entry(
                        manifest, manifest_written=manifest_written
                    )
                )
            report = self.results_report
            if blob_archive.update_index(
                entries,
                results=self.blob_results,
                results_scan=self.blob_results_scan,
                stats=self.blob_stats,
                stats_scan=self.blob_stats_scan,
                # The index read at the start of the run: a later read that
                # lacks any of its files is stale and never overwritten
                expected=report.index if report is not None else None,
            ):
                logger.info(
                    "Archived run %s in Vercel Blob: %d odds, %d prediction files, "
                    "%d stats tables",
                    self.run_ts,
                    len(self.blob_odds),
                    len(self.blob_predictions),
                    len(self.blob_stats),
                )
        except Exception:
            logger.warning("Could not finalize the Blob archive", exc_info=True)

    def _store_to_file(
        self,
        payload: list[dict[str, Any]],
        file_path: str | Path,
    ) -> bool:
        """Store predictions to a JSONL file.

        Parameters
        ----------
        payload : list[dict[str, Any]]
            The prediction payload, one entry per league
        file_path : str
            Full path to the export file

        Returns
        -------
        bool
            True if storage was successful, False otherwise

        """
        try:
            file_path = Path(file_path)
            with file_path.open("w", encoding="utf-8") as f:
                for entry in payload:
                    json.dump(entry, f, ensure_ascii=False)
                    f.write("\n")

        except OSError:
            logger.exception(
                "Failed to write predictions to file %s",
                file_path,
            )
            return False

        else:
            logger.debug("Successfully exported predictions to: %s", file_path)
            return True

    def _store_to_kv(
        self,
        payload: list[dict[str, Any]] | bytes,
        key_name: str,
    ) -> bool:
        """Store predictions (or an already serialized JSON value) in Vercel KV.

        Parameters
        ----------
        payload : list[dict[str, Any]] | bytes
            The prediction payload, one entry per league, or JSON bytes
            (sent as they are, e.g. the compact dashboard statistics)
        key_name : str
            The key name to use in KV storage

        Returns
        -------
        bool
            True if storage was successful, False otherwise

        """
        if not self.kv_initialized or not self.kv_url or not self.kv_token:
            return False

        headers = {
            "Authorization": f"Bearer {self.kv_token}",
            "Content-Type": "application/json",
        }

        try:
            # Store in KV using the provided key - use request body instead of URL path
            if isinstance(payload, bytes):
                response = requests.post(
                    f"{self.kv_url}/set/{key_name}",
                    headers=headers,
                    data=payload,
                    timeout=10,
                )
            else:
                response = requests.post(
                    f"{self.kv_url}/set/{key_name}",
                    headers=headers,
                    json=payload,  # Use json parameter instead of including in URL
                    timeout=10,
                )

            if response.status_code == 200:
                logger.debug(
                    "Successfully stored predictions in Vercel KV with key: %s",
                    key_name,
                )
                return True
            logger.error(
                "Failed to write predictions in Vercel KV: HTTP %d, Response: %s",
                response.status_code,
                # Truncate the response text to avoid excessive logging
                response.text[:200],
            )

        except Exception:
            logger.exception("Failed to write predictions to KV")
            return False

        else:
            return False
