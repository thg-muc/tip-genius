"""Module to handle storage operations for both file system and Vercel KV."""
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

# Set up logging
logger = logging.getLogger(__name__)

# %% --------------------------------------------
# * Class Definitions


class StorageManager:
    """Handles storage operations for match predictions data.

    This class provides functionality to store prediction data both in the local
    file system and in Vercel KV storage.

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

    Attributes
    ----------
    kv_initialized : bool
        Indicates whether KV storage is properly configured and available

    """

    def __init__(
        self,
        match_predictions_folder: str,
        *,
        debug: bool = False,
        write_to_kv: bool = False,
        export_to_file: bool = False,
    ) -> None:
        """Initialize the StorageManager with the given parameters."""
        self.match_predictions_folder = match_predictions_folder
        self.debug = debug
        self.write_to_kv = write_to_kv
        self.export_to_file = export_to_file
        self.kv_initialized = False
        self.kv_url: str | None = None
        self.kv_token: str | None = None

        # Initialize environment configuration
        self.kv_initialized = self._initialize_kv_config()

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
            config_path = Path("cfg") / "vercel_config.yaml"
            with config_path.open(encoding="utf-8") as f:
                config = yaml.safe_load(f)["tip_genius"]

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
            logger.warning(("Vercel KV storage failed to initialize: %s", str(e)))

        return False

    def store_predictions(
        self,
        prediction_data: dict[str, list[dict[str, Any]]],
        base_key: str,
        full_export_path: str | None = None,
    ) -> bool:
        """Write predictions to Vercel KV storage.

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
            True if all operations were successful, False if any failed

        """
        # Generate timestamp
        timestamp = datetime.now(tz=UTC).strftime("%Y-%m-%d_%H-%M-%S")
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
                    prediction_data=prediction_data,
                    file_path=export_path / timestamped_filename,
                )
                file_result2 = self._store_to_file(
                    prediction_data=prediction_data,
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
                kv_result1 = self._store_to_kv(
                    prediction_data,
                    f"{timestamp}_{base_key}",
                )
                kv_result2 = self._store_to_kv(prediction_data, base_key)
                all_successful = all_successful and kv_result1 and kv_result2
            else:
                logger.warning("Vercel KV not configured, predictions not stored.")
                all_successful = False

        return all_successful

    def _store_to_file(
        self,
        prediction_data: dict[str, list[dict[str, Any]]],
        file_path: str | Path,
    ) -> bool:
        """Store predictions to a JSONL file.

        Parameters
        ----------
        prediction_data : dict[str, dict[str, list[dict[str, Any]]]]
            The prediction data to store
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
                for sport, matches in prediction_data.items():
                    timestamp = datetime.now(tz=UTC).strftime(
                        "%Y-%m-%d %H:%M:%S",
                    )
                    json.dump(
                        {"name": sport, "timestamp": timestamp, "matches": matches},
                        f,
                        ensure_ascii=False,
                    )
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
        prediction_data: dict[str, list[dict[str, Any]]],
        key_name: str,
    ) -> bool:
        """Store predictions in Vercel KV.

        Parameters
        ----------
        prediction_data : dict[str, dict[str, list[dict[str, Any]]]]
            The prediction data to store
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
            timestamp = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%S")
            # Convert data to KV format (list of league data)
            kv_data = [
                {"name": sport, "timestamp": timestamp, "matches": matches}
                for sport, matches in prediction_data.items()
            ]

            # Store in KV using the provided key - use request body instead of URL path
            response = requests.post(
                f"{self.kv_url}/set/{key_name}",
                headers=headers,
                json=kv_data,  # Use json parameter instead of including in URL
                timeout=10,
            )

            if response.status_code == 200:
                logger.debug(
                    "Successfully stored predictions in Vercel KV with key: %s",
                    key_name,
                )
                return True
            logger.exception(
                "Failed to write predictions in Vercel KV: HTTP %d, Response: %s",
                response.status_code,
                # Truncate the response text to avoid excessive logging
                response.text[:200],
            )

        except Exception:
            logger.exception("Failed to write predictions to KV: %s")
            return False

        else:
            return False
