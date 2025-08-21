"""Team logo processing utility for bulk downloading and standardization.

Dynamically discovers ALL available leagues from football-logos repository,
downloads team logos, and converts them to the flat naming structure
expected by Tip Genius.

Usage:
    python utils/process_team_logos.py [--overwrite] [--output-dir PATH]

Options:
    --overwrite     Overwrite existing files instead of skipping them
    --output-dir    Specify custom output directory (default: public/images/teams)

This will automatically discover all leagues, download all available logos,
and store them in the specified directory. No maintenance needed when leagues
change - the script adapts automatically each season.
"""

# * Author(s): Thomas Glanzer
# * Creation : Aug 2025
# * License: MIT license

# %% --------------------------------------------
# * Libraries

import argparse
import logging
from pathlib import Path
from urllib.request import urlopen, urlretrieve

from rename_team_logos import purify_image_filename

# Only set up logging if running as standalone script
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

logger = logging.getLogger(__name__)

# %% --------------------------------------------
# * Configuration

# GitHub repository URLs
GITHUB_RAW_URL = (
    "https://raw.githubusercontent.com/luukhopman/football-logos/master/logos"
)

# Path configuration
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
OUTPUT_DIR = PROJECT_ROOT / "public" / "images" / "teams"

# No hardcoded leagues - discovered dynamically from repository

# Custom team name mappings before applying purification
TEAM_NAME_MAPPINGS = {
    # Common variations that need special handling
    "bor_m_gladbach.png": "borussia_monchengladbach.png",
    "e_frankfurt.png": "eintracht_frankfurt.png",
    "bayern_munchen.png": "bayern_munich.png",
    "psg.png": "paris_saint_germain.png",
    "man_city.png": "manchester_city.png",
    "man_united.png": "manchester_united.png",
}

# %% --------------------------------------------
# * Class Definitions


class TeamLogoProcessor:
    """Process team logos by downloading, renaming, and standardizing them.

    This class handles the complete workflow of:
    1. Dynamically discovering all available leagues from the repository
    2. Downloading logos from all leagues in the football-logos repository
    3. Applying custom name mappings
    4. Removing common prefixes
    5. Standardizing filenames to be URL-friendly

    Parameters
    ----------
    output_dir : Path, optional
        Directory to store processed logos, by default OUTPUT_DIR

    Attributes
    ----------
    output_dir : Path
        Path to the output directory for processed logos
    stats : dict
        Dictionary tracking processing statistics
    """

    def __init__(
        self, output_dir: Path = OUTPUT_DIR, *, overwrite: bool = False
    ) -> None:
        """Initialize the processor."""
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.overwrite = overwrite
        self.stats = {"downloaded": 0, "updated": 0, "skipped": 0, "errors": 0}

    def get_all_leagues(self) -> list[str]:
        """Dynamically discover all available leagues from the repository."""
        try:
            import json

            # Use GitHub API to get all directories in the logos folder
            api_url = (
                "https://api.github.com/repos/luukhopman/football-logos/contents/logos"
            )

            with urlopen(api_url) as response:  # noqa: S310
                data = json.loads(response.read().decode("utf-8"))

            # Filter for directories (leagues)
            leagues = [
                item["name"]
                for item in data
                if isinstance(item, dict) and item.get("type") == "dir"
            ]

            logger.info(
                "Discovered %d leagues: %s",
                len(leagues),
                ", ".join(leagues[:3]) + "..."
                if len(leagues) > 3
                else ", ".join(leagues),
            )
            return leagues

        except Exception as e:
            logger.warning("Failed to discover leagues: %s", e)
            # Fallback to a minimal list if API fails
            return [
                "England - Premier League",
                "Germany - Bundesliga",
                "Spain - LaLiga",
            ]

    def get_team_files_for_league(self, league: str) -> list[str]:
        """Get list of PNG files for a league using GitHub API."""
        try:
            import json
            from urllib.parse import quote

            # Use GitHub API instead of scraping HTML
            encoded_league = quote(league)
            api_url = f"https://api.github.com/repos/luukhopman/football-logos/contents/logos/{encoded_league}"

            with urlopen(api_url) as response:  # noqa: S310
                data = json.loads(response.read().decode("utf-8"))

            # Filter for PNG files
            png_files = [
                item["name"]
                for item in data
                if isinstance(item, dict) and item.get("name", "").endswith(".png")
            ]

            logger.info("Found %d logos for %s", len(png_files), league)
            return png_files

        except Exception as e:
            logger.warning("Failed to get file list for %s: %s", league, e)
            return []

    def apply_custom_mappings(self, filename: str) -> str:
        """Apply custom team name mappings."""
        return TEAM_NAME_MAPPINGS.get(filename, filename)

    def clean_filename_prefixes(self, filename: str) -> str:
        """Remove common prefixes from filename."""
        name = filename.replace(".png", "")

        # Common prefixes to remove (but keep some like AFC)
        prefixes_to_remove = [
            "fc_",
            "ac_",
            "sc_",
            "rsc_",
            "kv_",
            "sv_",
            "bsc_",
            "vfb_",
            "vfl_",
            "tsv_",
            "tsg_",
        ]

        for prefix in prefixes_to_remove:
            if name.startswith(prefix):
                name = name[len(prefix) :]
                break

        return f"{name}.png"

    def download_logo(self, league: str, filename: str) -> bool:
        """Download a single logo and save with standardized naming."""
        try:
            # Download URL with URL encoding for both league and filename
            from urllib.parse import quote

            encoded_league = quote(league)
            encoded_filename = quote(filename)
            url = f"{GITHUB_RAW_URL}/{encoded_league}/{encoded_filename}"

            # Apply custom mappings first
            mapped_filename = self.apply_custom_mappings(filename)

            # Remove common prefixes
            clean_filename = self.clean_filename_prefixes(mapped_filename)

            # Apply the standardized purification from rename_team_logos
            final_filename = purify_image_filename(clean_filename)

            output_path = self.output_dir / final_filename

            # Track if file already exists before download
            file_existed = output_path.exists()

            # Check if file exists (unless overwrite is enabled)
            if file_existed and not self.overwrite:
                logger.debug("Skipping existing: %s", final_filename)
                self.stats["skipped"] += 1
                return True
            elif file_existed and self.overwrite:
                logger.debug("Overwriting existing: %s", final_filename)

            # Download the file
            urlretrieve(url, output_path)  # noqa: S310

            if output_path.exists():
                if file_existed and self.overwrite:
                    self.stats["updated"] += 1
                    logger.info(
                        "Updated: %s/%s -> %s", league, filename, final_filename
                    )
                else:
                    self.stats["downloaded"] += 1
                    logger.info(
                        "Downloaded: %s/%s -> %s", league, filename, final_filename
                    )
                return True
            else:
                self.stats["errors"] += 1
                return False

        except Exception as e:
            logger.warning("Failed to download %s/%s: %s", league, filename, e)
            self.stats["errors"] += 1
            return False

    def process_all_logos(self) -> None:
        """Process all logos from all leagues (download, rename, and standardize)."""
        mode = "with overwrite enabled" if self.overwrite else "skipping existing files"
        logger.info("Starting bulk logo processing to %s (%s)", self.output_dir, mode)

        # Dynamically discover all available leagues
        leagues = self.get_all_leagues()
        if not leagues:
            logger.error("No leagues discovered, aborting.")
            return

        total_files = 0

        for league in leagues:
            logger.info("Processing league: %s", league)

            # Get list of PNG files for this league
            filenames = self.get_team_files_for_league(league)
            total_files += len(filenames)

            # Process each file
            for filename in filenames:
                self.download_logo(league, filename)

        logger.info("Bulk processing complete. Processed %d total files.", total_files)
        self.print_stats()

    def print_stats(self) -> None:
        """Print processing statistics."""
        logger.info("Processing Statistics:")
        logger.info("  Downloaded: %d", self.stats["downloaded"])
        logger.info("  Updated:    %d", self.stats["updated"])
        logger.info("  Skipped:    %d", self.stats["skipped"])
        logger.info("  Errors:     %d", self.stats["errors"])
        logger.info("  Total logos: %d", len(list(self.output_dir.glob("*.png"))))


# %% --------------------------------------------
# * Main Execution


def main() -> None:
    """Process all team logos."""
    parser = argparse.ArgumentParser(
        description=(
            "Process team logos by downloading and standardizing "
            "from football-logos repository"
        )
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing files instead of skipping them",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=OUTPUT_DIR,
        help=f"Output directory for processed logos (default: {OUTPUT_DIR})",
    )

    args = parser.parse_args()

    processor = TeamLogoProcessor(output_dir=args.output_dir, overwrite=args.overwrite)
    processor.process_all_logos()


if __name__ == "__main__":
    main()
