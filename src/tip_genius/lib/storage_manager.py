"""This module handles storage operations for both file system and Vercel KV."""

# * Author(s): Thomas Glanzer
# * Creation : Nov 2024
# * License: MIT license

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import yaml

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# %% --------------------------------------------
# * Class Definitions


class EnvConfig:
    """
    Handle environment configuration from .env files and system environment.

    Parameters
    ----------
    project_root : str or Path
        Path to the project root directory
    env_file : str, optional
        Name of the environment file, by default '.env.local'

    Attributes
    ----------
    env_vars : Dict[str, str]
        Dictionary containing loaded environment variables
    """

    def __init__(self, project_root: str | Path, env_file: str = ".env.local") -> None:
        self.project_root = Path(project_root)
        self.env_file = env_file
        self.env_vars: Dict[str, str] = {}
        self._load_env_file()

    def _load_env_file(self) -> None:
        """Load environment variables from the specified .env file."""
        env_path = self.project_root / self.env_file

        try:
            if env_path.exists():
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            key, value = line.split("=", 1)
                            self.env_vars[key.strip()] = (
                                value.strip().strip("'").strip('"')
                            )
                logger.info("Successfully loaded environment from: %s", self.env_file)
            else:
                logger.warning("Environment file not found: %s", env_path)
        except Exception as e:  # pylint: disable=broad-except
            logger.error("Failed to load environment file: %s", str(e))

    def get(self, key: str, default: Any = None) -> Optional[str]:
        """
        Get an environment variable, checking both .env file and system environment.

        Parameters
        ----------
        key : str
            The environment variable key
        default : Any, optional
            Default value if key is not found, by default None

        Returns
        -------
        Optional[str]
            The value of the environment variable or default if not found
        """
        # First check system environment
        value = os.environ.get(key)

        # If not in system environment, check loaded .env vars
        if value is None:
            value = self.env_vars.get(key)

        # Return value or default
        return value if value is not None else default


# %% --------------------------------------------
# * Class Definitions


class StorageManager:
    """
    Handles storage operations for match predictions data.

    This class provides functionality to store prediction data both in the local
    file system and in Vercel KV storage.

    Parameters
    ----------
    prediction_json_folder : str
        Path to the folder where JSON files will be stored
    debug : bool, optional
        Flag to indicate debug mode, by default False
    write_to_kv : bool, optional
        Flag to enable Vercel KV storage, by default False

    Attributes
    ----------
    kv_enabled : bool
        Indicates whether KV storage is properly configured and available
    """

    def __init__(
        self,
        prediction_json_folder: str,
        debug: bool = False,
        write_to_kv: bool = False,
    ):
        # Get project root directory
        self.project_root = Path(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        )
        self.prediction_json_folder = prediction_json_folder
        self.debug = debug
        self.write_to_kv = write_to_kv
        self.kv_enabled = False
        self.kv_url: Optional[str] = None
        self.kv_token: Optional[str] = None

        # Initialize environment configuration
        self.env_config = EnvConfig(self.project_root)

        if self.write_to_kv:
            self._initialize_kv_config()

    def _initialize_kv_config(self) -> None:
        """
        Initialize Vercel KV configuration from config file and environment variables.

        The method will first try to load configuration from .env.local file,
        then fall back to system environment variables if needed.

        Sets kv_enabled to True if configuration is successful, False otherwise.
        """
        try:
            # Load KV config
            config_path = os.path.join("cfg", "vercel_config.yaml")
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)["tip_genius"]

            # Get environment variables from .env.local or system environment
            self.kv_token = self.env_config.get(config["kv_token_env_name"])
            self.kv_url = self.env_config.get(config["kv_url_env_name"])

            if self.kv_token and self.kv_url:
                self.kv_enabled = True
                logger.info("Vercel KV storage configured successfully")
            else:
                logger.warning(
                    "Vercel KV storage not configured: missing environment variables"
                )

        except (FileNotFoundError, KeyError, yaml.YAMLError) as e:
            logger.error("Failed to initialize KV config: %s", str(e))
            self.kv_enabled = False

    def store_predictions(
        self,
        prediction_data: Dict[str, List[Dict[str, Any]]],
        base_filename: str,
    ) -> None:
        """
        Store predictions in both file system and optionally in Vercel KV.

        Parameters
        ----------
        prediction_data : Dict[str, Dict[str, List[Dict[str, Any]]]]
            The prediction data to store
        base_filename : str
            Base name for the output files
        """
        # Create full export path using project root
        full_export_path = os.path.join(self.project_root, self.prediction_json_folder)
        os.makedirs(full_export_path, exist_ok=True)

        # Generate timestamp
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

        # Generate filenames
        timestamped_filename = f"{timestamp}_{base_filename}.jsonl"
        non_timestamped_filename = f"{base_filename}.jsonl"

        # Store files locally
        self._store_to_file(prediction_data, timestamped_filename, full_export_path)
        self._store_to_file(prediction_data, non_timestamped_filename, full_export_path)

        # Store in KV if enabled
        if self.write_to_kv and self.kv_enabled:
            # Store both timestamped and non-timestamped versions
            self._store_to_kv(prediction_data, f"{timestamp}_{base_filename}")
            self._store_to_kv(prediction_data, base_filename)

    def _store_to_file(
        self,
        prediction_data: Dict[str, List[Dict[str, Any]]],
        filename: str,
        export_path: str,
    ) -> None:
        """
        Store predictions to a JSONL file.

        Parameters
        ----------
        prediction_data : Dict[str, Dict[str, List[Dict[str, Any]]]]
            The prediction data to store
        filename : str
            Name of the output file
        export_path : str
            Full path to the export directory
        """
        file_path = os.path.join(export_path, filename)
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                for sport, matches in prediction_data.items():
                    json.dump(
                        {"name": sport, "matches": matches},
                        f,
                        ensure_ascii=False,
                    )
                    f.write("\n")
            logger.info("Successfully exported predictions to: %s", filename)
        except IOError as e:
            logger.error("Failed to write predictions to file %s: %s", filename, str(e))

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
        if not self.kv_enabled or not self.kv_url or not self.kv_token:
            return

        headers = {"Authorization": f"Bearer {self.kv_token}"}

        try:
            # Convert data to KV format (list of league data)
            kv_data = [
                {"name": sport, "matches": matches}
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
