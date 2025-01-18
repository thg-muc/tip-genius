"""This module implements the Tip Genius workflow as a class."""

# * Author(s): Thomas Glanzer
# * Creation : Nov 2024
# * License: MIT license

# %% --------------------------------------------
# * Libraries

import json
import logging
import os
from ast import literal_eval
from collections import defaultdict
from datetime import datetime
from itertools import product
from pathlib import Path
from typing import Any, Dict, List

import polars as pl
import yaml

from lib.api_data import BaseAPI, OddsAPI
from lib.llm_manager import LLMManager
from lib.storage_manager import StorageManager

# Set up logging
logger = logging.getLogger(__name__)

# %% --------------------------------------------
# * Support Functions


def is_cloud_environment() -> bool:
    """
    Check if the code is running in a cloud/CI environment.

    The function checks for common environment variables used by different
    cloud providers and CI/CD platforms to determine the execution context.

    Returns
    -------
    bool
        True if running in a cloud environment (AWS, GitHub Actions, Vercel, etc.),
        False if running locally.

    Notes
    -----
    Currently detects:
    - GitHub Actions
    - Vercel
    - AWS Lambda
    - AWS CodeBuild
    - CircleCI
    - GitLab CI
    - Jenkins
    - Heroku
    - Render
    """
    # List of environment variables that indicate cloud/CI environments
    cloud_indicators = [
        "GITHUB_ACTIONS",  # GitHub Actions
        "VERCEL",  # Vercel
        "AWS_LAMBDA_FUNCTION_NAME",  # AWS Lambda
        "CODEBUILD_BUILD_ID",  # AWS CodeBuild
        "CIRCLECI",  # CircleCI
        "GITLAB_CI",  # GitLab CI
        "JENKINS_URL",  # Jenkins
        "DYNO",  # Heroku
        "RENDER",  # Render
    ]

    return any(indicator in os.environ for indicator in cloud_indicators)


# %% --------------------------------------------
# * TipGenius Class Definition


class TipGenius:
    """
    A class to execute the Tip Genius workflow for sports predictions.

    Parameters
    ----------
    api_pipeline : BaseAPI
        The API pipeline to use for fetching odds data.
    debug : bool, optional
        Flag to enable debug mode, by default False.
        Will limit the number of LLM requests and increase logging verbosity,

    Attributes
    ----------
    project_root : str or Path
        Path to the project root directory
    export_to_kv : bool, optional
        Flag to enable writing of result to Vercel KV storage, by default False.
    export_to_file : bool, optional
        Flag to enable writing of result to file system, by default False.
    store_api_results : bool, default False
        Flag to store intermediate API results to file system.
    store_llm_results : bool, default False
        Flag to store intermediate LLM results to file system.
    llm_attempts : int, default 5
        Number of attempts for LLM predictions before giving up.
    api_data_folder : str, default 'data/api_result'
        The folder path for storing API data.
    llm_data_folder : str, default 'data/llm_data'
        The folder path for storing LLM data.
    match_predictions_folder : str, default 'data/match_predictions'
        The folder path for storing prediction JSON and JSONL files.
    prediction_data : Dict[str, Dict[str, List[Dict[str, Any]]]]
        Nested dictionary to store prediction data for summary export.
    storage_manager : StorageManager
        An instance of the StorageManager class for handling storage operations.
    """

    # Set project root
    project_root = Path(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

    # Initialize all attributes with default values
    export_to_kv = False
    export_to_file = False
    store_api_results = False
    store_llm_results = False
    llm_attempts = 5

    api_data_folder = os.path.join("data", "api_result")
    llm_data_folder = os.path.join("data", "llm_data")
    match_predictions_folder = os.path.join("data", "match_predictions")

    prediction_data: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(dict)

    def __init__(
        self,
        api_pipeline: BaseAPI,
        debug: bool = False,
    ):
        """Initialize the TipGenius class."""

        # Initialize parameters
        self.api_pipeline = api_pipeline
        self.debug = debug
        self.storage_manager = None

        # Initialize logging
        log_level = logging.DEBUG if self.debug else logging.INFO
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        logger.info(
            "TipGenius initialized with API: %s", api_pipeline.__class__.__name__
        )

    def store_llm_data(
        self,
        df: pl.DataFrame,
        sport: str,
        llm_provider: str,
        prediction_type: str,
        named_teams: bool,
        additional_info: bool,
    ) -> None:
        """
        Store LLM prediction data in CSV format.

        Parameters
        ----------
        df : pl.DataFrame
            The dataframe containing the LLM prediction results.
        sport : str
            The name of the sport/league.
        llm_provider : str
            The name of the LLM provider used.
        prediction_type : str
            The type of prediction used.
        named_teams : bool
            Whether named teams were used or anonymized.
        additional_info : bool
            Whether additional information was included.
        """
        try:
            # Construct the full export path using the configured LLM data folder
            llm_export_path = os.path.join(self.project_root, self.llm_data_folder)
            os.makedirs(llm_export_path, exist_ok=True)

            # Generate current timestamp for unique filename
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

            # Construct filename with all relevant parameters
            filename = (
                f"{timestamp}_{sport}_{llm_provider}_{prediction_type}"
                f"_{'named' if named_teams else 'anonymized'}"
                f"_{'with-info' if additional_info else 'no-info'}"
            )

            # Add debug indicator to filename if in debug mode
            if self.debug:
                filename += "_debug"

            # Remove spaces from filename for compatibility
            filename = filename.replace(" ", "")

            # Construct the full file path with .csv extension
            csv_path = os.path.join(llm_export_path, f"{filename}.csv")

            # Write the dataframe to CSV using Polars' native write_csv method
            df.write_csv(csv_path)

            logger.debug("LLM data stored in CSV: %s", csv_path)

        except Exception as e:
            logger.error("Error storing LLM data: %s", str(e))
            raise

    def store_api_data(
        self,
        api_result: Dict[str, Any],
        sport: str,
        api_name: str,
    ) -> None:
        """
        Store the raw API result data in a JSON file in the specified data folder.

        Parameters
        ----------
        api_result : Dict[str, Any]
            The raw data returned from the API.
        sport : str
            The name of the sport/league.
        api_name : str
            The name of the API used.

        Returns
        -------
        None
        """
        suffix = f"_{sport}_{api_name}".replace(" ", "")
        try:
            # Construct the api_export_path using the configured data folder
            api_export_path = os.path.join(self.project_root, self.api_data_folder)
            os.makedirs(api_export_path, exist_ok=True)

            # Generate a filename with a timestamp
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            filename = f"{timestamp}{suffix}.json"
            file_path = os.path.join(api_export_path, filename)

            # Write the raw API data to the JSON file
            with open(file_path, "w", encoding="utf-8") as json_file:
                json.dump(api_result, json_file, ensure_ascii=False, indent=4)

            logger.debug("API result stored successfully at: %s", file_path)

        except Exception as exc:  # pylint: disable=broad-except
            logger.error("A problem occurred while storing API result: %s", exc)

    def predict_results(
        self, df: pl.DataFrame, llm_provider: str, prediction_type: str
    ) -> pl.DataFrame:
        """
        Process the dataframe using the given LLM provider and prediction type.

        Parameters
        ----------
        df : pl.DataFrame
            The dataframe to process.
        llm_provider : str
            The LLM provider to use.
        prediction_type : str
            The prediction type to use.

        Returns
        -------
        pl.DataFrame
            The processed dataframe with predictions.
        """
        llm = LLMManager(provider=llm_provider, prediction_type=prediction_type)

        def is_prediction_consistent(
            p_home: int, p_away: int, o_home: float, o_away: float
        ) -> bool:
            return not (
                (p_home > p_away and o_home >= o_away)
                or (p_home < p_away and o_home <= o_away)
            )

        def validate_prediction(pred: dict) -> bool:
            return (
                pred["reasoning"]
                and pred["outlook"]
                and isinstance(pred["prediction"]["home"], int)
                and isinstance(pred["prediction"]["away"], int)
            )

        for i in range(df.shape[0]):
            if any(df[i, f"odds_{key}"] == 0 for key in ["home", "away", "draw"]):
                logger.debug("Odds are invalid for row %d, skipping...", i + 1)
                continue

            last_response = None
            attempt = 0
            for attempt in range(self.llm_attempts):
                try:
                    response = literal_eval(
                        llm.get_prediction(
                            user_prompt=df[i, "odds_summary"],
                            temperature=llm.kwargs.get("temperature", 0.0)
                            + 0.2 * attempt,
                        )
                    )
                    last_response = response

                    if is_prediction_consistent(
                        response["prediction"]["home"],
                        response["prediction"]["away"],
                        df[i, "odds_home"],
                        df[i, "odds_away"],
                    ):
                        break
                    logger.warning(
                        "Prediction inconsistent with odds for row %d, attempt %d",
                        i + 1,
                        attempt + 1,
                    )
                except Exception as e:  # pylint: disable=broad-except
                    logger.warning(
                        "LLM prediction attempt %d failed for row %d: %s",
                        attempt + 1,
                        i + 1,
                        str(e),
                    )

            if not last_response:
                logger.warning("No valid LLM response for row %d, skipping...", i + 1)
                continue

            if attempt == self.llm_attempts - 1:
                logger.warning(
                    "Using inconsistent prediction for row %d after %d attempts: %s",
                    i + 1,
                    self.llm_attempts,
                    last_response,
                )

            df[i, "reasoning"] = last_response["reasoning"]
            df[i, "prediction_home"] = last_response["prediction"]["home"]
            df[i, "prediction_away"] = last_response["prediction"]["away"]
            df[i, "outlook"] = last_response["outlook"]
            df[i, "validity"] = validate_prediction(last_response)

        return df

    def save_results(
        self,
        sport: str,
        llm_provider: str,
        prediction_type: str,
        named_teams: bool,
        additional_info: bool,
        matches: List[Dict[str, Any]],
    ) -> None:
        """
        Save predictions in the nested dictionary structure.

        Parameters
        ----------
        sport : str
            The sport for which predictions are being stored.
        llm_provider : str
            The LLM provider used for predictions.
        prediction_type : str
            The type of prediction used.
        named_teams : bool
            Whether named teams were used or anonymized.
        additional_info : bool
            Whether additional information was included.
        matches : List[Dict[str, Any]]
            The list of match predictions to store.
        """
        key = (
            f"{llm_provider}_{prediction_type}_"
            f"{'named' if named_teams else 'anonymized'}_"
            f"{'with-info' if additional_info else 'no-info'}"
        )
        self.prediction_data[key][sport] = matches

    def export_results(self) -> None:
        """
        Export the summary of predictions to JSONL files and optionally to Vercel KV.
        """
        if not self.storage_manager:
            logger.warning(
                "No storage manager configured. Skipping export of predictions."
            )
            return

        for key, sport_data in self.prediction_data.items():
            base_key = f"Match_Predictions_{key}"
            if self.debug:
                base_key += "_debug"

            if self.export_to_file:
                full_export_path = os.path.join(
                    self.project_root, self.match_predictions_folder
                )
            else:
                full_export_path = None

            # Export predictions
            self.storage_manager.store_predictions(
                sport_data, base_key, full_export_path
            )

        logger.info("All predictions exported successfully.")

    def execute_workflow(self, config: Dict[str, Any]) -> None:
        """
        Execute the Tip Genius workflow for a given configuration.

        Parameters
        ----------
        config : Dict[str, Any]
            The configuration dictionary with the following keys:
            - sports_list : list
                The list of sports to process.
            - llm_provider_options : list
                The list of LLM providers to use.
            - prediction_type_options : list
                The list of prediction types to use.
            - named_teams_options : list
                The list of named teams options to use.
            - additional_info_options : list
                The list of additional info options to use.
        """
        # Initialize storage manager
        self.storage_manager = StorageManager(
            match_predictions_folder=self.match_predictions_folder,
            debug=self.debug,
            write_to_kv=self.export_to_kv,
            export_to_file=self.export_to_file,
        )
        self.prediction_data.clear()

        sports_list = config["sports_list"]
        llm_provider_options = config["llm_provider_options"]
        prediction_type_options = config["prediction_type_options"]
        named_teams_options = config["named_teams_options"]
        additional_info_options = config["additional_info_options"]

        nr_total_combinations = (
            len(llm_provider_options)
            * len(prediction_type_options)
            * len(named_teams_options)
            * len(additional_info_options)
            * len(sports_list)
        )

        if self.debug:
            logger.info("Debug mode is enabled, only processing first few rows...")

        logger.info(
            "Starting workflow with %d total combinations", nr_total_combinations
        )
        processed_count = 0

        for sport in sports_list:
            logger.debug("Retrieving odds for sport: %s", sport)

            # Fetch the odds data
            api_result = self.api_pipeline.fetch_api_data(
                sport_key=sport,
            )
            # Store the API data to json file
            if self.store_api_results:
                self.store_api_data(
                    api_result=api_result,
                    sport=sport,
                    api_name=self.api_pipeline.api_name,
                )

            combinations = product(
                llm_provider_options,
                prediction_type_options,
                named_teams_options,
                additional_info_options,
            )

            for (
                llm_provider,
                prediction_type,
                named_teams,
                additional_info,
            ) in combinations:
                processed_count += 1
                logger.info(
                    "Processing combination %d/%d: %s, %s, Named Teams: %s, Additional Info: %s",  # pylint: disable=line-too-long
                    processed_count,
                    nr_total_combinations,
                    llm_provider,
                    prediction_type,
                    named_teams,
                    additional_info,
                )

                # If API Data is not available, skip this combination
                if api_result is None or len(api_result) == 0:
                    logger.warning("No valid API data, skipping combination...")
                    continue

                # Process the API data into a DataFrame
                df = self.api_pipeline.process_api_data(
                    api_result=api_result,
                    named_teams=named_teams,
                    additional_info=additional_info,
                )

                # If debug mode is enabled, only process the first few rows
                if self.debug:
                    df = df.limit(2)

                df_processed = self.predict_results(
                    df=df,
                    llm_provider=llm_provider,
                    prediction_type=prediction_type,
                )

                # Store LLM results in CSV format
                if self.store_llm_results:
                    self.store_llm_data(
                        df=df_processed,
                        sport=sport,
                        llm_provider=llm_provider,
                        prediction_type=prediction_type,
                        named_teams=named_teams,
                        additional_info=additional_info,
                    )

                matches = df_processed.select(
                    [
                        "commence_time_str",
                        "home_team",
                        "away_team",
                        "prediction_home",
                        "prediction_away",
                        "outlook",
                    ]
                ).to_dicts()

                self.save_results(
                    sport=sport,
                    llm_provider=llm_provider,
                    prediction_type=prediction_type,
                    named_teams=named_teams,
                    additional_info=additional_info,
                    matches=matches,
                )

                logger.debug(
                    "Completed: %s, %s, Named Teams: %s, Additional Info: %s",
                    llm_provider,
                    prediction_type,
                    named_teams,
                    additional_info,
                )

        # Export the final results to KV / File
        if self.export_to_kv or self.export_to_file:
            self.export_results()

        logger.info("All workflows completed.")


# %% --------------------------------------------
# * Default Workflow

if __name__ == "__main__":

    # Check if running in cloud environment
    in_cloud = is_cloud_environment()

    if in_cloud:
        logger.info("Running in cloud environment.")
    else:
        from dotenv import load_dotenv

        logger.info("Running in local environment.")
        # Read env.local (if available) for local development
        load_dotenv(dotenv_path="../../.env.local")

    # Create an instance of TipGenius with an API class
    tip_genius = TipGenius(api_pipeline=OddsAPI(), debug=False)

    # Set attributes for the TipGenius instance
    options = {
        "store_api_results": not in_cloud,
        "store_llm_results": not in_cloud,
        "export_to_file": not in_cloud,
        "export_to_kv": True,
    }

    for option, value in options.items():
        setattr(tip_genius, option, value)

    # Load Config
    config_path = os.path.join("cfg", "tip_genius_config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        tip_genius_config = yaml.safe_load(f)
    # Execute Workflow
    tip_genius.execute_workflow(tip_genius_config)
