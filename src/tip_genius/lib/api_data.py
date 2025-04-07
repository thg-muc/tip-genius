"""Module for API information retrieval functionality."""

# * Author(s): Thomas Glanzer
# * Creation : Nov 2024
# * License: MIT license

# %% --------------------------------------------
# * Libraries

import logging
import os
from abc import ABC, abstractmethod
from typing import Any

import polars as pl
import requests
import yaml

# %% --------------------------------------------
# * Config

ODDS_CONFIG_FILE = os.path.join("cfg", "api_config.yaml")

# Set up logging
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

    def __init__(self, api_name: str) -> None:
        """Initialize the BaseAPI class."""
        self.api_name = api_name
        self.api_result_folder = os.path.join("data", "api_result")

        # Load Config
        try:
            with open(ODDS_CONFIG_FILE, encoding="utf-8") as f:
                self.config = yaml.safe_load(f)[self.api_name]
        except FileNotFoundError as exc:
            logger.exception("Config file not found: %s", ODDS_CONFIG_FILE)
            error_msg = f"Config file not found: {ODDS_CONFIG_FILE}"
            raise FileNotFoundError(error_msg) from exc
        except KeyError as exc:
            logger.exception("Key not found in config: %s")
            error_msg = f"Key not found in config: {exc}"
            raise KeyError(error_msg) from exc

    @abstractmethod
    def fetch_api_data(
        self,
        sport_key: str,
    ) -> dict[str, Any]:
        """Fetch odds data from the API for a specific sport.

        Parameters
        ----------
        sport_key : str
            The key of the sport for which to retrieve odds.

        Returns
        -------
        dict[str, Any]
            The raw data retrieved from the API.

        """

    @abstractmethod
    def process_api_data(
        self,
        api_result: dict[str, Any],
        named_teams: bool,
        additional_info: bool,
    ) -> pl.DataFrame:
        """Process API data into a Polars DataFrame and add necessary columns.

        Parameters
        ----------
        api_result : dict[str, Any]
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


class OddsAPI(BaseAPI):
    """Class for interacting with 'The Odds API'."""

    def __init__(self) -> None:
        """Initialize the OddsAPI class."""
        super().__init__(api_name="odds_api")

        # Get Params from config
        self.api_key = os.environ[self.config["api_key_env_name"]]
        self.base_url = self.config["base_url"]
        self.parameters = self.config["parameters"]
        self.sports_mapping = self.config["sports_mapping"]
        self.bookmaker_priority = self.config["bookmaker_priority"]

    def fetch_api_data(self, sport_key: str) -> dict[str, Any]:
        """Fetch odds data from the Odds API for a specific sport.

        Parameters
        ----------
        sport_key : str
            The key of the sport for which to retrieve odds.
            E.g., "Bundesliga - Germany", "Premier League - England", etc.

        Returns
        -------
        dict[str, Any]
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
            logger.exception("Sport key not found in sports mapping: %s", sport_key)
            error_msg = f"Sport key not found in sports mapping: {sport_key}"
            raise KeyError(error_msg) from exc

        # Construct the URL for the API request
        url = f"{self.base_url}{sports_value}/{self.parameters}&apiKey={self.api_key}"

        # Fetch data from the API
        logger.debug("Fetching odds data for sport: %s", sport_key)
        response = requests.get(url, timeout=10)

        # Check if the response was successful
        if response.status_code != 200:
            logger.error(
                "Failed to fetch odds data: %s %s",
                response.status_code,
                response.text,
            )
            error_message = (
                f"Failed to fetch odds data: {response.status_code} {response.text}"
            )
            raise requests.exceptions.HTTPError(error_message)

        api_result = response.json()
        logger.info(
            "Successfully fetched odds data for sport: %s | API credits remaining: %s",
            sport_key,
            response.headers.get("x-requests-remaining", "unknown"),
        )

        return api_result

    def process_api_data(
        self,
        api_result: dict[str, Any],
        named_teams: bool,
        additional_info: bool,
        target_timezone: str = "Europe/Berlin",
    ) -> pl.DataFrame:
        """Process API data into a Polars DataFrame and add necessary columns.

        Parameters
        ----------
        api_result : dict[str, Any]
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
        data = pl.DataFrame(api_result)

        # Create a new 'commence_time_str' as str, with correct time zone
        data = data.with_columns(
            pl.col("commence_time")
            .str.strptime(pl.Datetime, "%Y-%m-%dT%H:%M:%SZ")  # Naive DT
            .dt.replace_time_zone("UTC")  # Reset time zone to UTC
            .dt.convert_time_zone(target_timezone)  # Set to Target Zone
            .dt.strftime("%d.%m.%Y %H:%M")  # Format as string
            .alias("commence_time_str"),  # Rename to commence_time_str
        )

        # Add necessary columns for odds, reasoning, and predictions
        data = data.with_columns(
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
        for i in range(data.shape[0]):
            bookmakers_tuple = data.select("bookmakers").row(i)[0]
            bookmakers_dict: dict[str, tuple[float, float, float]] = {}
            home_team = data[i, "home_team"]
            away_team = data[i, "away_team"]

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
                home_team = data[i, "home_team"]
                away_team = data[i, "away_team"]
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
                odds_summary = f"{odds_summary}, {data[i, 'sport_title']}"

            data[i, "odds_summary"] = odds_summary
            data[i, "odds_home"] = odds_home
            data[i, "odds_away"] = odds_away
            data[i, "odds_draw"] = odds_draw

        # Remove nested columns
        data = data.drop("bookmakers")

        logger.debug("Data processing completed successfully.")
        return data
