"""Utility module for matching team names."""

# * Author(s): Thomas Glanzer
# * Creation : Jan 2025
# * License: MIT license

# %% --------------------------------------------
# * Libraries

import logging
from difflib import get_close_matches
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from slugify import slugify

# Set up logging
logger = logging.getLogger(__name__)

# %% --------------------------------------------
# * Class Definitions


class TeamLogoMatcher:
    """
    A class for matching team names with their logo files using difflib.

    This class provides functionality to match team names from various sources
    (like API responses) with logo files using Python's standard library difflib.

    Parameters
    ----------
    logo_directory : str or Path
        Path to the directory containing team logo files
    match_cutoff : float, optional
        Minimum similarity ratio (0.0-1.0) to consider a match valid, by default 0.6

    Attributes
    ----------
    logo_directory : Path
        Pathlib Path object pointing to the logo directory
    match_cutoff : float
        Threshold for considering a match valid
    logo_files : List[str]
        Cached list of available logo filenames (without extension)
    """

    def __init__(self, logo_directory: str | Path, match_cutoff: float = 0.6) -> None:
        """Initialize the TeamLogoMatcher with a directory path and cutoff."""
        self.logo_directory = Path(logo_directory)
        self.match_cutoff = match_cutoff
        self.logo_files: List[str] = []
        self._loadlogo_files()
        logger.info(
            "TeamLogoMatcher initialized with %d logo files", len(self.logo_files)
        )

    def _loadlogo_files(self) -> None:
        """
        Load and cache the list of logo files from the directory.

        This method reads all PNG files from the logo directory and stores their
        names without extensions for matching.

        Raises
        ------
        FileNotFoundError
            If the logo directory doesn't exist
        """
        try:
            self.logo_files = [f.stem for f in self.logo_directory.glob("*.png")]
            logger.debug("Loading logo files from directory: %s", self.logo_directory)

            if not self.logo_files:
                logger.warning(
                    "No PNG files found in logo directory: %s", self.logo_directory
                )

        except FileNotFoundError:
            logger.error("Logo directory not found: %s", self.logo_directory)
            raise

    def preprocess_name(self, name: str) -> str:
        """
        Preprocess a team name for better matching.

        Parameters
        ----------
        name : str
            Team name to preprocess

        Returns
        -------
        str
            Preprocessed team name using slugify for consistent processing
        """
        return slugify(name, separator="_")

    @lru_cache(maxsize=1024)
    def find_logo(self, team_name: str) -> Optional[str]:
        """
        Find the most likely matching logo file for a team name.

        Parameters
        ----------
        team_name : str
            Search term for the team name.

        Returns
        -------
        str or None
            Name of the matching logo file or None if no match was found
        """
        if not team_name or not self.logo_files:
            logger.debug(
                "Invalid input: team_name=%s, logo_files=%s",
                bool(team_name),
                bool(self.logo_files),
            )
            return None

        # Create a mapping of processed names to original filenames
        name_mapping = {self.preprocess_name(f): f for f in self.logo_files}

        # Find best match using processed names
        processed_name = self.preprocess_name(team_name)
        matches = get_close_matches(
            processed_name, list(name_mapping.keys()), n=5, cutoff=self.match_cutoff
        )

        if not matches:
            logger.warning(
                "No logo match found for team '%s' (processed: '%s')",
                team_name,
                processed_name,
            )
            return None

        # Get original filename directly from mapping
        best_match = name_mapping[matches[0]]
        best_match_logo = f"{best_match}.png"
        logger.debug(
            "Found logo match for '%s': %s (similarity matches: %s)",
            team_name,
            best_match_logo,
            matches,
        )

        return best_match_logo
