"""Utility functions to standardize image filenames to be URL-friendly.

This module provides functions to purify filenames for team logos,
making them URL-friendly and consistent across the application.

Can be used as:
1. Standalone script: python rename_team_logos.py
2. Imported module: from rename_team_logos import purify_image_filename
"""

# * Author(s): Thomas Glanzer
# * Creation : Feb 2025
# * License: MIT license

# %% --------------------------------------------
# * Libraries

import logging
from pathlib import Path

from slugify import slugify

# Only set up logging if running as standalone script
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

logger = logging.getLogger(__name__)

# %% --------------------------------------------
# * Code Execution


def purify_image_filename(filename: str) -> str:
    """Convert a filename to a URL-friendly format.

    Parameters
    ----------
    filename : str
        Original filename to be purified

    Returns
    -------
    str
        URL-friendly filename with special characters handled:
        - Spaces converted to underscores
        - Accents removed
        - Special characters removed
        - Lowercase conversion

    """
    name, ext = Path(filename).stem, Path(filename).suffix
    purified_name = slugify(name, separator="_", lowercase=True)
    return f"{purified_name}{ext.lower()}"


def purify_image_directory(directory: str | Path = ".") -> None:
    """Purify all image filenames in a directory to make them URL-friendly.

    Parameters
    ----------
    directory : str or Path, optional
        Directory containing PNG files to process, by default "."

    """
    try:
        directory = Path(directory)
        logger.info("Processing PNG files in: %s", directory.absolute())

        for filepath in directory.glob("*.png"):
            original_name = filepath.name
            new_filename = purify_image_filename(original_name)

            if original_name != new_filename:
                try:
                    filepath.rename(directory / new_filename)
                    logger.info('Renamed: "%s" -> "%s"', original_name, new_filename)
                except OSError:
                    logger.exception('Failed to rename "%s"', original_name)
    except Exception:
        logger.exception("Error processing directory")


if __name__ == "__main__":
    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent
    target_dir = project_root / "public" / "images" / "teams"
    purify_image_directory(target_dir)
