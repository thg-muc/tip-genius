"""Module for LLM-related functions."""

# * Author(s): Thomas Glanzer
# * Creation : Nov 2024
# * License: MIT license

# %% --------------------------------------------
# * Libs and Config

import logging
import os
import time
from pathlib import Path
from typing import Any

import requests
import yaml
from lib.llm_prompts import Prompt

LLM_CONFIG_FILE = Path("cfg") / "llm_config.yaml"

# Set up logging
logger = logging.getLogger(__name__)

# %% --------------------------------------------
# * Class Definitions


class LLMManager:
    """Class for interacting with various LLM providers to retrieve predictions.

    Parameters
    ----------
    provider : str
        The provider to use for the Language Model.
        A config with required parameters will be loaded from the config file.
    prediction_type : str, optional
        The type of prediction to use, by default "Default".

    Attributes
    ----------
    provider : str
        The LLM provider name.
    prediction_type : str
        The type of prediction.
    config : dict
        Configuration loaded from the YAML file.
    api_key : str
        API key for the LLM provider.
    system_prompt : str
        System prompt for the LLM.
    base_url : str
        Base URL for the API.
    model : str
        Model name to use.
    kwargs : dict
        Additional keyword arguments for the LLM.
    full_response_list : list
        A list to store full responses from the LLM.

    Raises
    ------
    FileNotFoundError
        If the config file is not found.
    KeyError
        If a required key is not found in the config.
    ValueError
        If the API key is not found in environment variables.

    Examples
    --------
    >>> from llm_manager import LLMManager
    >>> llm = LLMManager(provider="mistral", prediction_type="Default")
    >>> llm.get_prediction("Liverpool FC : 2.1, Manchester United : 2.0, draw: 1.9")

    """

    def __init__(self, provider: str, prediction_type: str = "Default") -> None:
        """Initialize the LLMManager."""
        self.provider = provider.lower()
        self.prediction_type = prediction_type
        self.full_response_list: list[Any] = []

        logger.debug("Initializing LLMManager with provider: %s", self.provider)

        # Load Config
        class ConfigError(Exception):
            """Exception raised for config-related errors."""

            def __init__(self, message: str) -> None:
                super().__init__(message)

        try:
            with LLM_CONFIG_FILE.open(encoding="utf-8") as f:
                self.config = yaml.safe_load(f)[self.provider]
        except FileNotFoundError as exc:
            logger.exception("Config file not found: %s", LLM_CONFIG_FILE)
            error_message = f"Config file not found: {LLM_CONFIG_FILE}"
            raise ConfigError(error_message) from exc
        except KeyError as exc:
            error_message = f"Key not found in config: {exc}"
            logger.exception(error_message)
            raise ConfigError(error_message) from exc

        # Get API key from environment variables
        try:
            self.api_key = os.environ[self.config["api_key_env_name"]]
        except KeyError as exc:
            error_message = f"Key not found in environment: {exc}"
            logger.exception(error_message)
            raise KeyError(error_message) from exc

        # Get other parameters
        self.system_prompt = Prompt.get(self.prediction_type)
        self.base_url = self.config["base_url"]
        self.operation = self.config["operation"]
        self.model = self.config["model"]
        self.kwargs: dict[str, Any] = self.config.get("kwargs", {})
        self.rate_limit: float = self.config.get(
            "api_rate_limit",
            0,
        )  # 0 or negative value means no rate limit

    def wait_for_rate_limit(self, request_duration: float) -> None:
        """Calculate and wait for the appropriate time to respect rate limits.

        Parameters
        ----------
        request_duration : float
            The duration of the last request in seconds.

        """
        # Calculate minimum time between requests (in s) with a small safety buffer
        min_interval = (60.0 / self.rate_limit) * 1.1

        # If request took less time than minimum interval, sleep for the difference
        if request_duration < min_interval:
            sleep_duration = min_interval - request_duration
            logger.debug(
                "Rate limiting: request took %s seconds, sleeping for %s seconds",
                round(request_duration, 2),
                round(sleep_duration, 2),
            )
            time.sleep(sleep_duration)

    def get_prediction(self, user_prompt: str, timeout: int = 60, **kwargs) -> str:
        """Get the prediction from the LLM.

        Parameters
        ----------
        user_prompt : str
            The prompt to send to the LLM.
        timeout : int, optional
            Timeout for in seconds, by default 60 (sufficient for most LLMs).
        **kwargs : dict, optional
            Additional keyword arguments to pass to the LLM (override defaults).

        Returns
        -------
        str
            The prediction from the LLM.

        Raises
        ------
        requests.RequestException
            If there's an error in getting the prediction from the LLM.

        """
        logger.debug("Getting prediction for prompt: %.50s...", user_prompt)

        # Record start time
        start_time = time.time()

        # Get kwargs, give priority to kwargs passed to the function
        llm_kwargs = {**self.kwargs, **kwargs}

        headers = {
            "content-type": "application/json",
        }

        # For Anthropic Claude, we need to structure the request differently
        if self.provider.startswith("anthropic"):
            url = f"{self.base_url}/{self.operation}"
            headers["x-api-key"] = self.api_key
            headers["anthropic-version"] = "2023-06-01"

            data = {
                "model": self.model,
                "system": self.system_prompt,
                "messages": [
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
                **llm_kwargs,
            }
        # For Google Gemini, we need to structure the request differently
        elif self.provider.startswith("google"):
            url = f"{self.base_url}/models/{self.model}:{self.operation}"
            headers["x-goog-api-key"] = self.api_key

            data = {
                "contents": [{"parts": [{"text": user_prompt}]}],
                "generationConfig": {**llm_kwargs},
                "system_instruction": {"parts": {"text": self.system_prompt}},
            }

        # Other providers (OpenAI and compatible: Mistral, etc.)
        else:
            url = f"{self.base_url}/{self.operation}"
            headers["authorization"] = f"Bearer {self.api_key}"

            data = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                **llm_kwargs,
            }

        try:
            # Send the prompt to the LLM to receive the full response
            response = requests.post(
                url=url,
                headers=headers,
                json=data,
                timeout=timeout,
            )
            # Check the response and raise an exception if it's not successful
            response.raise_for_status()

            # Parse the response
            full_response = response.json()
            self.full_response_list.append(full_response)

            # Get the prediction
            if self.provider.startswith("anthropic"):
                prediction = full_response["content"][0]["text"]
            elif self.provider.startswith("google"):
                prediction = full_response["candidates"][0]["content"]["parts"][0][
                    "text"
                ]
            else:
                prediction = full_response["choices"][0]["message"]["content"]

            # Observe a rate limit if specified
            if self.rate_limit > 0:  # Check for rate limit
                self.wait_for_rate_limit(request_duration=time.time() - start_time)

        except requests.RequestException:
            logger.exception("Failed to get prediction: %s")
            raise

        else:
            logger.debug("Received prediction: %.50s...", prediction)
            return prediction
