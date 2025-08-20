"""Module to implement the Tip Genius workflow as a class."""

# * Author(s): Thomas Glanzer
# * Creation : Nov 2024
# * License: MIT license

# %% --------------------------------------------
# * Libraries

import json
import logging
import os
import sys
import time
from ast import literal_eval
from collections import defaultdict
from datetime import UTC, datetime
from itertools import product
from pathlib import Path
from typing import Any

import polars as pl
import yaml
from lib.api_data import BaseAPI, OddsAPI
from lib.llm_manager import LLMManager
from lib.storage_manager import StorageManager
from lib.team_matching import TeamLogoMatcher

# Set up logging
logger = logging.getLogger(__name__)

# %% --------------------------------------------
# * Support Functions


def is_cloud_environment() -> bool:
    """Check if the code is running in a cloud/CI environment.

    The function checks for common environment variables used by different
    cloud providers and CI/CD platforms to determine the execution context.

    Returns
    -------
    bool
        True if running in a cloud environment (AWS, GitHub Actions, Vercel, etc.),
        False if running locally.

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
    """A class to execute the Tip Genius workflow for sports predictions.

    Parameters
    ----------
    api_pipeline : BaseAPI
        The API pipeline to use for fetching odds data.
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
    llm_attempts : int, default 4
        Number of attempts for LLM predictions before giving up.
    api_data_folder : str, default 'data/api_result'
        The folder path for storing API data.
    llm_data_folder : str, default 'data/llm_data'
        The folder path for storing LLM data.
    match_predictions_folder : str, default 'data/match_predictions'
        The folder path for storing prediction JSON and JSONL files.
    prediction_data : dict[str, dict[str, list[dict[str, Any]]]]
        Nested dictionary to store prediction data for summary export.
    storage_manager : StorageManager
        An instance of the StorageManager class for handling storage operations.

    """

    # Set project root
    project_root = Path(__file__).parent.parent.parent

    # Initialize all attributes with default values
    export_to_kv = False
    export_to_file = False
    store_api_results = False
    store_llm_results = False
    llm_attempts = 4

    api_data_folder = Path("data") / "api_result"
    llm_data_folder = Path("data") / "llm_data"
    match_predictions_folder = Path("data") / "match_predictions"

    prediction_data: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(dict)

    def __init__(
        self,
        api_pipeline: BaseAPI,
    ) -> None:
        """Initialize the TipGenius class."""
        # Initialize parameters
        self.api_pipeline = api_pipeline

        # Read debug settings from environment
        self.debug = os.environ.get("DEBUG_MODE", "FALSE").upper() == "TRUE"
        self.debug_limit = int(os.environ.get("DEBUG_PROCESSING_LIMIT", "0"))

        # Initialize class attributes
        self.storage_manager = None
        self.logo_matcher = None

        # Initialize logging
        log_level = logging.DEBUG if self.debug else logging.INFO
        self._setup_logging(log_level)

        # Log initialization
        if self.debug:
            logger.info("Debug mode is enabled...")

        logger.info(
            "TipGenius initialized with API: %s",
            api_pipeline.__class__.__name__,
        )
        logger.debug("Project root directory: %s", self.project_root)

    def _setup_logging(self, log_level: int) -> None:
        """Set up logging configuration with optional file output in debug mode."""
        # Create custom formatter
        formatter = logging.Formatter(
            fmt="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        # Get root logger and clear existing handlers
        root_logger = logging.getLogger()
        root_logger.setLevel(log_level)
        root_logger.handlers.clear()

        # Always add console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(log_level)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

        # Add file handler when debug mode is enabled
        if self.debug:
            # Check if file logging is enabled (defaults to True in debug mode)
            enable_file_logging = (
                os.environ.get("DEBUG_LOG_FILE", "TRUE").upper() == "TRUE"
            )

            if enable_file_logging:
                # Create logs directory if it doesn't exist
                log_dir = Path(os.environ.get("DEBUG_LOG_DIR", "logs"))
                log_dir.mkdir(exist_ok=True)

                # Generate timestamp-based filename
                timestamp = datetime.now(UTC).strftime("%Y-%m-%d_%H-%M-%S")
                log_filename = log_dir / f"tip_genius_{timestamp}.log"

                # Create file handler with more detailed formatting
                file_handler = logging.FileHandler(log_filename, encoding="utf-8")
                file_handler.setLevel(logging.DEBUG)  # Always DEBUG level for file

                # More detailed formatter for files
                file_formatter = logging.Formatter(
                    fmt=(
                        "%(asctime)s - %(name)s:%(lineno)d - %(levelname)s - "
                        "%(funcName)s - %(message)s"
                    ),
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
                file_handler.setFormatter(file_formatter)
                root_logger.addHandler(file_handler)

                # Log the file location
                logger.info("Debug logging enabled - log file: %s", log_filename)

    def store_llm_data(
        self,
        data: pl.DataFrame,
        sport: str,
        llm_provider: str,
        prediction_type: str,
        *,
        named_teams: bool,
        additional_info: bool,
    ) -> None:
        """Store LLM prediction data in CSV format.

        Parameters
        ----------
        data : pl.DataFrame
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
            llm_export_path = self.project_root / self.llm_data_folder
            llm_export_path.mkdir(parents=True, exist_ok=True)

            # Generate current timestamp for unique filename
            timestamp = datetime.now(tz=UTC).strftime("%Y-%m-%d_%H-%M-%S")

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
            csv_path = llm_export_path / f"{filename}.csv"

            # Write the dataframe to CSV using Polars' native write_csv method
            data.write_csv(csv_path)

            logger.debug("LLM data stored in CSV: %s", csv_path)

        except Exception:
            logger.exception("Failed to store LLM data: %s")
            raise

    def store_api_data(
        self,
        api_result: dict[str, Any],
        sport: str,
        api_name: str,
    ) -> None:
        """Store the raw API result data in a JSON file in the specified data folder.

        Parameters
        ----------
        api_result : dict[str, Any]
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
            api_export_path = self.project_root / self.api_data_folder
            api_export_path.mkdir(parents=True, exist_ok=True)

            # Generate a filename with a timestamp
            timestamp = datetime.now(tz=UTC).strftime("%Y-%m-%d_%H-%M-%S")
            filename = f"{timestamp}{suffix}.json"
            file_path = api_export_path / filename

            # Write the raw API data to the JSON file
            with file_path.open("w", encoding="utf-8") as json_file:
                json.dump(api_result, json_file, ensure_ascii=False, indent=4)

            logger.debug("API result stored successfully at: %s", file_path)

        except Exception:
            logger.exception("A problem occurred while storing API result: %s")

    def predict_results(
        self,
        data: pl.DataFrame,
        llm_provider: str,
        prediction_type: str,
    ) -> pl.DataFrame:
        """Process the dataframe using the given LLM provider and prediction type.

        Parameters
        ----------
        data : pl.DataFrame
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
        try:
            llm = LLMManager(provider=llm_provider, prediction_type=prediction_type)
        except Exception:
            logger.exception(
                "Failed to initialize LLM provider %s",
                llm_provider,
            )
            return data  # Return unmodified dataframe if LLM initialization fails

        def is_prediction_consistent(
            p_home: int,
            p_away: int,
            o_home: float,
            o_away: float,
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

        for i in range(data.shape[0]):
            if any(data[i, f"odds_{key}"] == 0 for key in ["home", "away", "draw"]):
                logger.debug("Odds are invalid for row %d, skipping...", i + 1)
                continue

            try:
                last_response = None
                attempt = 0
                start_time = time.time()  # Track start time for rate limiting

                for attempt in range(self.llm_attempts):
                    try:
                        # Calculate time since last request for rate limiting
                        if attempt > 0 and llm.rate_limit > 0:
                            elapsed = time.time() - start_time
                            llm.wait_for_rate_limit(elapsed)

                        response = literal_eval(
                            llm.get_prediction(
                                user_prompt=data[i, "odds_summary"],
                                temperature=llm.kwargs.get("temperature", 0.0)
                                + 0.2 * attempt,
                            ),
                        )
                        last_response = response

                        if is_prediction_consistent(
                            response["prediction"]["home"],
                            response["prediction"]["away"],
                            data[i, "odds_home"],
                            data[i, "odds_away"],
                        ):
                            break
                        logger.debug(
                            "Prediction inconsistent with odds for row %d, attempt %d",
                            i + 1,
                            attempt + 1,
                        )
                    except Exception as e:
                        logger.warning(
                            "LLM prediction attempt %d failed for row %d: %s",
                            attempt + 1,
                            i + 1,
                            str(e),
                        )
                        continue  # Try next attempt even if this one fails

                if not last_response:
                    logger.warning(
                        "No valid LLM response for row %d, skipping...",
                        i + 1,
                    )
                    continue

                if attempt == self.llm_attempts - 1:
                    logger.warning(
                        "Using inconsistent prediction for row %d after %d failed "
                        "attempts: %s",
                        i + 1,
                        self.llm_attempts,
                        last_response,
                    )

                # Update DataFrame with prediction results
                data[i, "reasoning"] = last_response["reasoning"]
                data[i, "prediction_home"] = last_response["prediction"]["home"]
                data[i, "prediction_away"] = last_response["prediction"]["away"]
                data[i, "outlook"] = last_response["outlook"]
                data[i, "validity"] = validate_prediction(last_response)

            except Exception as e:
                logger.warning("Failed to process row %d: %s", i + 1, str(e))
                continue  # Skip this row but continue processing others

        return data

    def save_results(
        self,
        sport: str,
        llm_provider: str,
        prediction_type: str,
        *,
        named_teams: bool,
        additional_info: bool,
        matches: list[dict[str, Any]],
    ) -> None:
        """Save predictions in the nested dict structure and (optionally) add logos.

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
        matches : list[dict[str, Any]]
            The list of match predictions to store.

        """
        # Add logo matching for each match
        for match in matches:
            if self.logo_matcher is not None:
                match["home_logo"] = self.logo_matcher.find_logo(match["home_team"])
                match["away_logo"] = self.logo_matcher.find_logo(match["away_team"])
            else:
                match["home_logo"] = None
                match["away_logo"] = None

        key = (
            f"{llm_provider}_{prediction_type}_"
            f"{'named' if named_teams else 'anonymized'}_"
            f"{'with-info' if additional_info else 'no-info'}"
        )
        self.prediction_data[key][sport] = matches

    def export_results(self) -> bool:
        """Export the summary of predictions to JSONL files and optionally to Vercel KV.

        Returns
        -------
        bool
            True if all export operations were successful, False otherwise

        """
        if not self.storage_manager:
            logger.warning(
                "No storage manager configured. Skipping export of predictions.",
            )
            return False

        all_successful = True
        for key, sport_data in self.prediction_data.items():
            base_key = f"Match_Predictions_{key}"
            if self.debug:
                base_key += "_debug"

            if self.export_to_file:
                full_export_path = str(
                    self.project_root / self.match_predictions_folder
                )
            else:
                full_export_path = None

            # Export predictions and track success
            success = self.storage_manager.store_predictions(
                sport_data,
                base_key,
                full_export_path,
            )
            all_successful = all_successful and success

        if all_successful:
            logger.info("All predictions exported successfully.")
        else:
            logger.warning("Some predictions could not be exported successfully.")

        return all_successful

    def execute_workflow(self, config: dict[str, Any]) -> None:  # noqa: C901
        """Execute the Tip Genius workflow for a given configuration.

        The workflow continues execution in case individual combinations fail.

        Parameters
        ----------
        config : dict[str, Any]
            The configuration dictionary with workflow parameters.

        """
        try:
            # Initialize storage manager
            self.storage_manager = StorageManager(
                match_predictions_folder=str(self.match_predictions_folder),
                debug=self.debug,
                write_to_kv=self.export_to_kv,
                export_to_file=self.export_to_file,
            )
            self.prediction_data.clear()

            # Initialize logo matcher if folder is configured and exists
            if team_logos_path := config.get("team_logos_folder"):
                full_path = self.project_root / team_logos_path
                if full_path.exists():
                    try:
                        self.logo_matcher = TeamLogoMatcher(logo_directory=full_path)
                        logger.debug("TeamLogoMatcher successfully initialized.")
                    except Exception:
                        logger.warning("Failed to initialize logo matcher: %s")
                else:
                    logger.warning(
                        "Not performing Logo matching, logo dir does not exist: %s",
                        full_path,
                    )

            sports_list = config["sports_list"]
            llm_provider_options = config["llm_provider_options"]
            prediction_type_options = config["prediction_type_options"]
            named_teams_options = config["named_teams_options"]
            additional_info_options = config["additional_info_options"]

            if not sports_list:
                logger.warning("No sports defined in the configuration, aborting.")
                return
            if not llm_provider_options:
                logger.warning(
                    "No LLM providers defined in the configuration, aborting.",
                )
                return

            nr_total_combinations = (
                len(llm_provider_options)
                * len(prediction_type_options)
                * len(named_teams_options)
                * len(additional_info_options)
                * len(sports_list)
            )

            logger.info(
                "Starting workflow with %d total combinations",
                nr_total_combinations,
            )
            processed_count = 0
            failed_combinations = []

            for sport in sports_list:
                logger.info("Retrieving odds for sport: %s", sport)

                try:
                    # Fetch the odds data
                    api_result = self.api_pipeline.fetch_api_data(sport_key=sport)
                    if self.store_api_results:
                        self.store_api_data(
                            api_result=api_result,
                            sport=sport,
                            api_name=self.api_pipeline.api_name,
                        )
                except Exception:
                    logger.exception("Failed to fetch odds for sport %s", sport)
                    continue  # Skip this sport but continue with others

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
                    try:
                        logger.info(
                            "Processing combination %d/%d: %s, %s, Named Teams: %s, "
                            "Additional Info: %s",
                            processed_count,
                            nr_total_combinations,
                            llm_provider,
                            prediction_type,
                            named_teams,
                            additional_info,
                        )

                        if not api_result:
                            logger.warning("No valid API data, skipping combination...")
                            failed_combinations.append(
                                (sport, llm_provider, "No valid API data"),
                            )
                            continue

                        data = self.api_pipeline.process_api_data(
                            api_result=api_result,
                            named_teams=named_teams,
                            additional_info=additional_info,
                        )

                        if self.debug and self.debug_limit > 0:
                            data = data.limit(self.debug_limit)

                        data_processed = self.predict_results(
                            data=data,
                            llm_provider=llm_provider,
                            prediction_type=prediction_type,
                        )

                        if self.store_llm_results:
                            self.store_llm_data(
                                data=data_processed,
                                sport=sport,
                                llm_provider=llm_provider,
                                prediction_type=prediction_type,
                                named_teams=named_teams,
                                additional_info=additional_info,
                            )

                        # Keep valid predictions only
                        valid_matches = (
                            data_processed.filter(pl.col("validity").cast(pl.Boolean))
                            .select(
                                [
                                    "commence_time_str",
                                    "home_team",
                                    "away_team",
                                    "prediction_home",
                                    "prediction_away",
                                    "outlook",
                                    "reasoning",
                                ],
                            )
                            .to_dicts()
                        )

                        # Save valid predictions
                        if valid_matches:
                            self.save_results(
                                sport=sport,
                                llm_provider=llm_provider,
                                prediction_type=prediction_type,
                                named_teams=named_teams,
                                additional_info=additional_info,
                                matches=valid_matches,
                            )

                    except Exception as e:
                        logger.exception(
                            "Failed to process combination: %s, %s",
                            sport,
                            llm_provider,
                        )
                        failed_combinations.append((sport, llm_provider, str(e)))
                        continue  # Skip this combination but continue with others

            # Export results even if some combinations failed
            if self.prediction_data and (self.export_to_kv or self.export_to_file):
                try:
                    export_success = self.export_results()
                    if not export_success and is_cloud_environment():
                        # Only exit with failure in cloud environments
                        logger.exception(
                            "Critical error during export. Exiting with status code 1.",
                        )
                        sys.exit(1)
                except Exception:
                    logger.exception("Failed to export results: %s")
                    if is_cloud_environment():
                        sys.exit(1)

            # Log summary of failures
            if failed_combinations:
                logger.warning(
                    "Workflow completed with %d failed combinations:",
                    len(failed_combinations),
                )
                for sport, provider, error in failed_combinations:
                    logger.warning("- %s / %s: %s", sport, provider, error)
            else:
                logger.info("All workflows completed successfully.")

        except Exception:
            logger.exception("Critical workflow error: %s")
            # Try to save any collected data even if workflow fails
            if self.prediction_data and (self.export_to_kv or self.export_to_file):
                try:
                    export_success = self.export_results()
                    if not export_success and is_cloud_environment():
                        sys.exit(1)
                except Exception:
                    logger.exception("Failed to export results after error: %s")
                    if is_cloud_environment():
                        sys.exit(1)
            raise  # Re-raise the exception after attempting to save data


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
        # Read env.local (local development)
        load_dotenv(dotenv_path="../../.env.local")

    # Create an instance of TipGenius with an API class
    tip_genius = TipGenius(api_pipeline=OddsAPI())

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
    config_path = Path("cfg") / "tip_genius_config.yaml"
    with config_path.open(encoding="utf-8") as f:
        tip_genius_config = yaml.safe_load(f)
    # Execute Workflow
    tip_genius.execute_workflow(tip_genius_config)
