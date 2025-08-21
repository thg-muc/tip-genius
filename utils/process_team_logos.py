"""Team logo processing utility for bulk downloading and standardization.

Downloads ALL team logos from football-logos repository and converts them
to the flat naming structure expected by Tip Genius.

Usage:
    python utils/process_team_logos.py

This will download all available logos and store them in public/images/teams/
Run manually once per season to update logos.
"""

# * Author(s): Thomas Glanzer
# * Creation : Aug 2025
# * License: MIT license

# %% --------------------------------------------
# * Libraries

import logging
import re
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

# Countries/leagues to download
COUNTRIES = [
    "england",
    "germany",
    "spain",
    "italy",
    "france",
    "portugal",
    "netherlands",
    "belgium",
    "austria",
    "switzerland",
    "scotland",
    "turkey",
    "greece",
    "russia",
    "ukraine",
    "poland",
    "czech-republic",
    "denmark",
    "sweden",
    "norway",
    "croatia",
    "serbia",
    "bulgaria",
    "romania",
    "hungary",
    "slovakia",
    "slovenia",
]

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
    1. Downloading logos from the football-logos repository
    2. Applying custom name mappings
    3. Removing common prefixes
    4. Standardizing filenames to be URL-friendly

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

    def __init__(self, output_dir: Path = OUTPUT_DIR) -> None:
        """Initialize the processor."""
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.stats = {"downloaded": 0, "updated": 0, "skipped": 0, "errors": 0}

    def get_team_files_for_country(self, country: str) -> list[str]:
        """Get list of PNG files for a country by scraping GitHub."""
        try:
            github_url = f"https://github.com/luukhopman/football-logos/tree/master/logos/{country}"

            with urlopen(github_url) as response:  # noqa: S310
                html = response.read().decode("utf-8")

            # Extract PNG filenames from GitHub HTML
            png_pattern = (
                r'href="/luukhopman/football-logos/blob/master/logos/'
                + country
                + r'/([^"]+\.png)"'
            )
            matches = re.findall(png_pattern, html)

            logger.info("Found %d logos for %s", len(matches), country)
            return matches

        except Exception as e:
            logger.warning("Failed to get file list for %s: %s", country, e)
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

    def download_logo(self, country: str, filename: str) -> bool:
        """Download a single logo and save with standardized naming."""
        try:
            # Download URL
            url = f"{GITHUB_RAW_URL}/{country}/{filename}"

            # Apply custom mappings first
            mapped_filename = self.apply_custom_mappings(filename)

            # Remove common prefixes
            clean_filename = self.clean_filename_prefixes(mapped_filename)

            # Apply the standardized purification from rename_team_logos
            final_filename = purify_image_filename(clean_filename)

            output_path = self.output_dir / final_filename

            # Check if file exists
            if output_path.exists():
                logger.debug("Skipping existing: %s", final_filename)
                self.stats["skipped"] += 1
                return True

            # Download the file
            urlretrieve(url, output_path)  # noqa: S310

            if output_path.exists():
                self.stats["downloaded"] += 1
                logger.info(
                    "Downloaded: %s/%s -> %s", country, filename, final_filename
                )
                return True
            else:
                self.stats["errors"] += 1
                return False

        except Exception as e:
            logger.warning("Failed to download %s/%s: %s", country, filename, e)
            self.stats["errors"] += 1
            return False

    def process_all_logos(self) -> None:
        """Process all logos from all countries (download, rename, and standardize)."""
        logger.info("Starting bulk logo processing to %s", self.output_dir)

        total_files = 0

        for country in COUNTRIES:
            logger.info("Processing country: %s", country)

            # Get list of PNG files for this country
            filenames = self.get_team_files_for_country(country)
            total_files += len(filenames)

            # Process each file
            for filename in filenames:
                self.download_logo(country, filename)

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
    processor = TeamLogoProcessor()
    processor.process_all_logos()


if __name__ == "__main__":
    main()
