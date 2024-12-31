"""This module implements the Tip Genius workflow as a class."""

# * Author(s): Thomas Glanzer
# * Creation : Nov 2024
# * License: MIT license

# %% --------------------------------------------
# * Libraries

import logging
import os
from ast import literal_eval
from collections import defaultdict
from datetime import datetime
from itertools import product
from typing import Any, Dict, List

import polars as pl
import yaml
from tqdm import tqdm

from lib.api_data import BaseAPI, OddsAPI
from lib.llm_manager import LLMManager
from lib.storage_manager import StorageManager

# %% --------------------------------------------
# * Configuration and Logging

DEBUG = False  # Limit number of API calls and other actions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

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
    write_to_kv : bool, optional
        Flag to enable writing of result to Vercel KV storage, by default False.
    write_to_file : bool, optional
        Flag to enable writing of result to file system, by default False.

    Attributes
    ----------
    store_api_results : bool, default False
        Flag to store intermediate API results to file system.
    store_llm_results : bool, default False
        Flag to store intermediate LLM results to file system.
    llm_attempts : int, default 5
        Number of attempts for LLM predictions before giving up.
    llm_data_folder : str, default 'data/llm_data'
        The folder path for storing LLM data.
    prediction_json_folder : str, default 'data/match_predictions'
        The folder path for storing prediction JSON and JSONL files.
    prediction_data : Dict[str, Dict[str, List[Dict[str, Any]]]]
        Nested dictionary to store prediction data for summary export.
    """

    # Initialize class parameters
    store_api_results = False
    store_llm_results = False
    llm_attempts = 5

    llm_data_folder = os.path.join("data", "llm_data")
    prediction_json_folder = os.path.join("data", "match_predictions")

    def __init__(
        self,
        api_pipeline: BaseAPI,
        debug: bool = False,
        write_to_kv: bool = False,
        write_to_file: bool = False,
    ):
        """Initialize the TipGenius class."""

        # Initialize class paremeters
        self.api_pipeline = api_pipeline
        self.debug = debug
        self.prediction_data: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(
            dict
        )

        if self.debug:
            self.store_api_results = True
            self.store_llm_results = True

        # Initialize storage manager
        self.storage_manager = StorageManager(
            prediction_json_folder=self.prediction_json_folder,
            debug=self.debug,
            write_to_kv=write_to_kv,
            write_to_file=write_to_file,
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
            # Determine the project root directory by navigating up from current file
            project_root = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            )

            # Construct the full export path using the configured LLM data folder
            llm_export_path = os.path.join(project_root, self.llm_data_folder)
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

        Raises
        ------
        ValueError
            If the LLM prediction is inconsistent with the odds.
        """
        llm = LLMManager(provider=llm_provider, prediction_type=prediction_type)

        for i in range(df.shape[0]):
            odds_summary = df[i, "odds_summary"]
            odds_home = df[i, "odds_home"]
            odds_away = df[i, "odds_away"]
            odds_draw = df[i, "odds_draw"]

            if (odds_home == 0) or (odds_away == 0) or (odds_draw == 0):
                logger.debug("Odds are invalid for row %d, skipping...", i)
                continue

            success = False
            reasoning = prediction_home = prediction_away = outlook = None
            for attempt in range(self.llm_attempts):
                try:
                    llm_response = llm.get_prediction(
                        user_prompt=odds_summary,
                        temperature=llm.kwargs.get("temperature", 0.0) + 0.2 * attempt,
                    )

                    response_dict = literal_eval(llm_response)
                    reasoning = response_dict["reasoning"]
                    prediction_home = response_dict["prediction"]["home"]
                    prediction_away = response_dict["prediction"]["away"]
                    outlook = response_dict["outlook"]

                    if (
                        prediction_home > prediction_away and odds_home >= odds_away
                    ) or (prediction_home < prediction_away and odds_home <= odds_away):
                        logger.warning(
                            "Prediction inconsistent with odds for row %d, retrying...",
                            i,
                        )
                        continue

                    success = True
                    break

                except Exception as e:  # pylint: disable=broad-except
                    logger.warning(
                        "LLM prediction attempt %d failed for row %d: %s",
                        attempt + 1,
                        i,
                        str(e),
                    )

            if not success or any(
                var is None
                for var in (reasoning, prediction_home, prediction_away, outlook)
            ):
                logger.warning("No valid LLM response for row %d, skipping...", i)
                continue

            df[i, "reasoning"] = reasoning
            df[i, "prediction_home"] = prediction_home
            df[i, "prediction_away"] = prediction_away
            df[i, "outlook"] = outlook

            validity = (
                reasoning != ""
                and outlook != ""
                and isinstance(prediction_home, (int))
                and isinstance(prediction_away, (int))
            )
            df[i, "validity"] = validity

        return df

    def store_predictions(
        self,
        sport: str,
        llm_provider: str,
        prediction_type: str,
        named_teams: bool,
        additional_info: bool,
        matches: List[Dict[str, Any]],
    ) -> None:
        """
        Store predictions in the nested dictionary structure.

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
        for key, sport_data in self.prediction_data.items():
            base_filename = f"Match_Predictions_{key}"
            if self.debug:
                base_filename += "_debug"

            self.storage_manager.store_predictions(sport_data, base_filename)

        logger.info("All predictions exported successfully")

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

        with tqdm(total=nr_total_combinations, desc="Processing combinations") as pbar:
            for sport in sports_list:
                logger.debug("Retrieving odds for sport: %s", sport)

                api_result = self.api_pipeline.fetch_api_data(
                    sport_key=sport,
                    store_api_result=self.store_api_results,
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
                    logger.debug(
                        "Processing: %s, %s, Named Teams: %s, Additional Info: %s",
                        llm_provider,
                        prediction_type,
                        named_teams,
                        additional_info,
                    )

                    # If API Data is not available, skip this combination
                    if api_result is None or len(api_result) == 0:
                        logger.warning("No valid API data, skipping combination...")
                        pbar.update(1)
                        continue

                    # Process the API data into a DataFrame
                    df = self.api_pipeline.process_api_data(
                        api_result,
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

                    self.store_predictions(
                        sport,
                        llm_provider,
                        prediction_type,
                        named_teams,
                        additional_info,
                        matches,
                    )

                    logger.debug(
                        "Completed: %s, %s, Named Teams: %s, Additional Info: %s",
                        llm_provider,
                        prediction_type,
                        named_teams,
                        additional_info,
                    )

                    pbar.update(1)

        self.export_results()

        logger.info("All workflows completed.")


# %% --------------------------------------------
# * Default Workflow

if __name__ == "__main__":

    # Read configuration into a dictionary
    config_path = os.path.join("cfg", "tip_genius_config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        tip_genius_config = yaml.safe_load(f)

    # Create an instance of TipGenius with an API class
    tip_genius = TipGenius(
        api_pipeline=OddsAPI(), debug=False, write_to_kv=True, write_to_file=True
    )

    # Intermediate Result Storage (by default False)
    tip_genius.store_api_results = True
    tip_genius.store_llm_results = True

    # Execute Workflow
    tip_genius.execute_workflow(tip_genius_config)
