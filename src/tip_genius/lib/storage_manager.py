"""This module handles storage operations for both file system and Vercel KV."""

# * Author(s): Thomas Glanzer
# * Creation : Nov 2024
# * License: MIT license

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests
import yaml

# Set up logging
logger = logging.getLogger(__name__)

# %% --------------------------------------------
# * Class Definitions


class StorageManager:
    """
    Handles storage operations for match predictions data.

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
        debug: bool = False,
        write_to_kv: bool = False,
        export_to_file: bool = False,
    ):

        self.match_predictions_folder = match_predictions_folder
        self.debug = debug
        self.write_to_kv = write_to_kv
        self.export_to_file = export_to_file
        self.kv_initialized = False
        self.kv_url: Optional[str] = None
        self.kv_token: Optional[str] = None

        # Initialize environment configuration
        self.kv_initialized = self._initialize_kv_config()

    def _initialize_kv_config(self) -> bool:
        """
        Initialize Vercel KV configuration from config file and environment variables.

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
            config_path = os.path.join("cfg", "vercel_config.yaml")
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)["tip_genius"]

            # Get environment variables from .env.local or system environment
            self.kv_token = os.environ[config["kv_token_env_name"]]
            self.kv_url = os.environ[config["kv_url_env_name"]]

            if self.kv_token and self.kv_url:
                self.kv_initialized = True
                logger.info("Vercel KV storage configured successfully")
                return True
            else:
                logger.warning(
                    "Vercel KV storage not configured: missing environment variables"
                )

        except (FileNotFoundError, KeyError, yaml.YAMLError) as e:
            logger.warning(("Vercel KV storage failed to initialize: %s", str(e)))

        return False

    def store_predictions(
        self,
        prediction_data: Dict[str, List[Dict[str, Any]]],
        base_key: str,
        full_export_path: Optional[str] = None,
    ) -> None:
        """
        Write predictions to Vercel KV storage.

        Parameters
        ----------
        prediction_data : Dict[str, Dict[str, List[Dict[str, Any]]]]
            The prediction data to store
        base_key : str
            Base key name for the output
        full_export_path : str, optional
            Full path to the export directory. Required if export_to_file is True
        """
        # Generate timestamp
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

        # Generate filenames
        timestamped_filename = f"{timestamp}_{base_key}.jsonl"
        non_timestamped_filename = f"{base_key}.jsonl"
        # Store files locally if enabled
        if self.export_to_file:
            if full_export_path:
                # Create directory if it doesn't exist
                os.makedirs(full_export_path, exist_ok=True)
                self._store_to_file(
                    prediction_data=prediction_data,
                    file_path=os.path.join(full_export_path, timestamped_filename),
                )
                self._store_to_file(
                    prediction_data=prediction_data,
                    file_path=os.path.join(full_export_path, non_timestamped_filename),
                )
            else:
                logger.warning(
                    "Full export path not provided, predictions not stored locally."
                )
        # Store in KV if enabled
        if self.write_to_kv:
            if self.kv_initialized:
                # Store both timestamped and non-timestamped versions
                self._store_to_kv(prediction_data, f"{timestamp}_{base_key}")
                self._store_to_kv(prediction_data, base_key)
            else:
                logger.warning("Vercel KV not configured, predictions not stored.")

    def _store_to_file(
        self,
        prediction_data: Dict[str, List[Dict[str, Any]]],
        file_path: str,
    ) -> None:
        """
        Store predictions to a JSONL file.

        Parameters
        ----------
        prediction_data : Dict[str, Dict[str, List[Dict[str, Any]]]]
            The prediction data to store
        file_path : str
            Full path to the export file
        """
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                for sport, matches in prediction_data.items():
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    json.dump(
                        {"name": sport, "timestamp": timestamp, "matches": matches},
                        f,
                        ensure_ascii=False,
                    )
                    f.write("\n")
            logger.info("Successfully exported predictions to: %s", file_path)
        except IOError as e:
            logger.error(
                "Failed to write predictions to file %s: %s", file_path, str(e)
            )

    def _store_to_kv(
        self,
        prediction_data: Dict[str, List[Dict[str, Any]]],
        key_name: str,
    ) -> None:
        """
        Store predictions in Vercel KV.

        Parameters
        ----------
        prediction_data : Dict[str, Dict[str, List[Dict[str, Any]]]]
            The prediction data to store
        key_name : str
            The key name to use in KV storage
        """
        if not self.kv_initialized or not self.kv_url or not self.kv_token:
            return

        headers = {"Authorization": f"Bearer {self.kv_token}"}

        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # Convert data to KV format (list of league data)
            kv_data = [
                {"name": sport, "timestamp": timestamp, "matches": matches}
                for sport, matches in prediction_data.items()
            ]

            # Store in KV using the provided key name
            response = requests.post(
                f"{self.kv_url}/set/{key_name}/{json.dumps(kv_data)}",
                headers=headers,
                timeout=10,
            )

            if response.status_code == 200:
                logger.info(
                    "Successfully stored predictions in Vercel KV with key: %s",
                    key_name,
                )
            else:
                logger.error(
                    "Failed to store predictions in KV: HTTP %d", response.status_code
                )

        except Exception as e:  # pylint: disable=broad-except
            logger.error("Error storing predictions in KV: %s", str(e))
