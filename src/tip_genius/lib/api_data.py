"""This module contains api information retrieval functionality."""

# * Author(s): Thomas Glanzer
# * Creation : Nov 2024
# * License: MIT license

# %% --------------------------------------------
# * Libraries

import json
import logging
import os
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, Tuple

import polars as pl
import requests
import yaml

# %% --------------------------------------------
# * Config

ODDS_CONFIG_FILE = os.path.join("cfg", "api_config.yaml")

# Setup basic configuration for logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# %% --------------------------------------------
# * Class Definitions


class BaseAPI(ABC):
    """Abstract Base class for different API classes.

    Parameters
    ----------
    api_name : str
        The name of the API.
    """

    def __init__(self, api_name: str):
        """Initialize the BaseAPI class."""
        self.api_name = api_name
        self.api_result_folder = os.path.join("data", "api_result")

        # Load Config
        try:
            with open(ODDS_CONFIG_FILE, "r", encoding="utf-8") as f:
                self.config = yaml.safe_load(f)[self.api_name]
        except FileNotFoundError as exc:
            logger.error("Config file not found: %s", ODDS_CONFIG_FILE)
            raise FileNotFoundError(
                f"Config file not found: {ODDS_CONFIG_FILE}"
            ) from exc
        except KeyError as exc:
            logger.error("Key not found in config: %s", exc)
            raise KeyError(f"Key not found in config: {exc}") from exc

    @abstractmethod
    def fetch_api_data(
        self, sport_key: str, store_api_result: bool = False
    ) -> Dict[str, Any]:
        """
        Fetch odds data from the API for a specific sport.

        Parameters
        ----------
        sport_key : str
            The key of the sport for which to retrieve odds.
        store_api_result : bool, optional, default False
            Whether to store the raw API result data.

        Returns
        -------
        Dict[str, Any]
            The raw data retrieved from the API.
        """

    @abstractmethod
    def process_api_data(
        self, api_result: Dict[str, Any], named_teams: bool, additional_info: bool
    ) -> pl.DataFrame:
        """
        Process API data into a Polars DataFrame and add necessary columns.

        Parameters
        ----------
        api_result : Dict[str, Any]
            The raw data retrieved from the API.
        named_teams : bool
            Whether to use named teams in the prediction or to anonymize them.
        additional_info : bool
            Whether to include additional information in the prediction.

        Returns
        -------
        pl.DataFrame
            A Polars DataFrame with processed data.
        """

    @staticmethod
    def store_api_result_as_json(
        api_result: Dict[str, Any], export_folder: str, suffix: str = ""
    ) -> str:
        """
        Store the raw API result data in a JSON file in the specified data folder.

        Parameters
        ----------
        api_result : Dict[str, Any]
            The raw data returned from the API.
        export_folder : str
            The folder where the JSON file will be saved.
        suffix : str, optional, default ''
            The suffix to add to the filename

        Returns
        -------
        str
            The full path to the JSON file.
        """
        try:
            # Determine the project root and create the full export path
            project_root = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            )
            full_export_path = os.path.join(project_root, export_folder)
            os.makedirs(full_export_path, exist_ok=True)

            # Generate a filename with a timestamp
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            filename = f"{timestamp}{suffix}.json"
            file_path = os.path.join(full_export_path, filename)

            # Write the raw API data to the JSON file
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(api_result, f, ensure_ascii=False, indent=4)

            logger.debug("API result stored successfully at: %s", file_path)
            return file_path

        except Exception as exc:  # pylint: disable=broad-except
            logger.error("A problem occurred while storing API result: %s", exc)
            return ""


class OddsAPI(BaseAPI):
    """Class for interacting with 'The Odds API'."""

    def __init__(self):
        """Initialize the OddsAPI class."""
        super().__init__(api_name="odds_api")

        # Get Params from config
        self.api_key = os.environ[self.config["api_key_env_name"]]
        self.base_url = self.config["base_url"]
        self.parameters = self.config["parameters"]
        self.sports_mapping = self.config["sports_mapping"]
        self.bookmaker_priority = self.config["bookmaker_priority"]

    def fetch_api_data(
        self, sport_key: str, store_api_result: bool = False
    ) -> Dict[str, Any]:
        """
        Fetch odds data from the Odds API for a specific sport.

        Parameters
        ----------
        sport_key : str
            The key of the sport for which to retrieve odds.
            E.g., "Bundesliga - Germany", "Premier League - England", etc.
        store_api_result : bool, optional, default False
            Whether to store the raw API result data.

        Returns
        -------
        Dict[str, Any]
            The raw data retrieved from the Odds API.

        Raises
        ------
        KeyError
            If the sport key is not found in the sports mapping.
        Exception
            If the API request fails for any reason.
        """
        try:
            # Get the value of the 'sport_key' parameter from the sports mapping
            sports_value = self.sports_mapping[sport_key]
        except KeyError as exc:
            logger.error("Sport key not found in sports mapping: %s", sport_key)
            raise KeyError(
                f"Sport key not found in sports mapping: {sport_key}"
            ) from exc

        # Construct the URL for the API request
        url = f"{self.base_url}{sports_value}/{self.parameters}&apiKey={self.api_key}"

        logger.debug("Fetching odds data for sport: %s", sport_key)
        response = requests.get(url, timeout=30)

        # Check if the response was successful
        if response.status_code != 200:
            logger.error(
                "Failed to fetch odds data: %s %s", response.status_code, response.text
            )
            raise requests.exceptions.HTTPError(
                f"Failed to fetch odds data: {response.status_code} {response.text}"
            )

        api_result = response.json()
        logger.debug("Successfully fetched odds data for sport: %s", sport_key)

        if store_api_result:
            suffix = f"_{sport_key}_{self.api_name}".replace(" ", "")
            file_export_location = self.store_api_result_as_json(
                api_result=api_result,
                export_folder=self.api_result_folder,
                suffix=suffix,
            )
            logger.debug("Raw API result data stored in: %s", file_export_location)

        return api_result

    def process_api_data(
        self,
        api_result: Dict[str, Any],
        named_teams: bool,
        additional_info: bool,
        target_timezone: str = "Europe/Berlin",
    ) -> pl.DataFrame:
        """
        Process API data into a Polars DataFrame and add necessary columns.

        Parameters
        ----------
        api_result : Dict[str, Any]
            The raw data retrieved from the API.
        named_teams : bool
            Whether to use named teams in the prediction or to anonymize them.
        additional_info : bool
            Whether to include additional information in the prediction.
        target_timezone : str, optional
            The target timezone for datetime conversion, by default "Europe/Berlin"

        Returns
        -------
        pl.DataFrame
            A Polars DataFrame with processed data.
        """
        logger.debug("Processing API data...")
        df = pl.DataFrame(api_result)

        # Create a new 'commence_time_str' as str, with correct time zone
        df = df.with_columns(
            pl.col("commence_time")
            .str.strptime(pl.Datetime, "%Y-%m-%dT%H:%M:%SZ")  # Naive DT
            .dt.replace_time_zone("UTC")  # Reset time zone to UTC
            .dt.convert_time_zone(target_timezone)  # Set to Target Zone
            .dt.strftime("%d.%m.%Y %H:%M")  # Format as string
            .alias("commence_time_str")  # Rename to commence_time_str
        )

        # Add necessary columns for odds, reasoning, and predictions
        df = df.with_columns(
            pl.lit("").alias("odds_summary"),
            pl.lit(0.0).alias("odds_home"),
            pl.lit(0.0).alias("odds_away"),
            pl.lit(0.0).alias("odds_draw"),
            pl.lit("").alias("reasoning"),
            pl.lit(0).alias("prediction_home"),
            pl.lit(0).alias("prediction_away"),
            pl.lit("").alias("outlook"),
            pl.lit(0).alias("validity"),
        )

        # Iterate over each row to process odds and generate predictions
        for i in range(df.shape[0]):
            bookmakers_tuple = df.select("bookmakers").row(i)[0]
            bookmakers_dict: Dict[str, Tuple[float, float, float]] = {}
            home_team = df[i, "home_team"]
            away_team = df[i, "away_team"]

            # Extract the odds for home, away, and draw outcomes
            for bookmaker in bookmakers_tuple:
                outcomes = bookmaker["markets"][0]["outcomes"]
                cur_home = next(
                    (
                        outcome["price"]
                        for outcome in outcomes
                        if outcome["name"] == home_team
                    ),
                    None,
                )
                cur_away = next(
                    (
                        outcome["price"]
                        for outcome in outcomes
                        if outcome["name"] == away_team
                    ),
                    None,
                )
                cur_draw = next(
                    (
                        outcome["price"]
                        for outcome in outcomes
                        if outcome["name"] == "Draw"
                    ),
                    None,
                )
                if cur_home and cur_away and cur_draw:
                    bookmakers_dict[bookmaker["key"]] = (
                        cur_home,
                        cur_away,
                        cur_draw,
                    )

            # Initialize variables for home, away, and draw odds
            odds_home, odds_away, odds_draw = 0.0, 0.0, 0.0
            # Iterate over the prioritized bookmakers
            for bookmaker in self.bookmaker_priority:
                # Try to get the odds for the current bookmaker
                odds_tuple = bookmakers_dict.get(bookmaker)
                if odds_tuple is not None:
                    # Update home, away, and draw with the valid odds
                    odds_home, odds_away, odds_draw = odds_tuple
                    break

            # Add the processed odds to the DataFrame
            if named_teams:
                home_team = df[i, "home_team"]
                away_team = df[i, "away_team"]
                odds_summary = (
                    f"{home_team}: {odds_home}, {away_team}: {odds_away}, "
                    f"draw: {odds_draw}"
                )
            else:
                odds_summary = (
                    f"home: {odds_home}, away: {odds_away}, draw: {odds_draw}"
                )

            # Add additional information if specified
            if additional_info:
                odds_summary = f"{odds_summary}, {df[i, 'sport_title']}"

            df[i, "odds_summary"] = odds_summary
            df[i, "odds_home"] = odds_home
            df[i, "odds_away"] = odds_away
            df[i, "odds_draw"] = odds_draw

        # Remove nested columns
        df = df.drop("bookmakers")

        logger.debug("Data processing completed successfully.")
        return df
