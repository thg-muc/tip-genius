"""Evaluation module for team logo matching."""

# * Author(s): Thomas Glanzer
# * Creation : Feb 2025
# * License: MIT license

# %% --------------------------------------------
# * Libraries

import logging
import sys
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from statistics import mean

import polars as pl

# Add src directory to path
src_path = str(Path(__file__).parents[1] / "src")
sys.path.insert(0, src_path)

from tip_genius.lib.team_matching import TeamLogoMatcher  # noqa

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# %% --------------------------------------------
# * Code Execution


def load_team_names(llm_data_folder: Path) -> pl.DataFrame:
    """Load and combine team names from LLM data files."""
    data_list = []
    for csv_file in llm_data_folder.glob("*.csv"):
        data = pl.read_csv(csv_file).select(["home_team", "away_team"])
        data_list.append(data)
    return pl.concat(data_list)


def eval_logo_matching() -> None:
    """Evaluate logo matching and generate statistics."""
    project_root = Path(__file__).parents[1]
    llm_data_folder = project_root / "data" / "llm_data_test"
    logo_folder = project_root / "public" / "images" / "teams"

    # Load team names
    data = load_team_names(llm_data_folder)

    # Get unique team names
    team_names = pl.concat(
        [
            data.select("home_team"),
            data.select("away_team").rename({"away_team": "home_team"}),
        ],
    ).unique()

    # Initialize matcher
    matcher = TeamLogoMatcher(
        logo_directory=logo_folder,
        match_cutoff=0.0,
    )  # Set cutoff to 0 to get all similarities

    # Match statistics
    matches = []
    similarities = []
    unmatched = []

    for team in team_names["home_team"]:
        processed_name = matcher.preprocess_name(team)
        name_mapping = {matcher.preprocess_name(f): f for f in matcher.logo_files}

        # Get similarity scores for all logo files
        scores = [
            (f, SequenceMatcher(None, processed_name, n).ratio())
            for n, f in name_mapping.items()
        ]

        if scores:
            best_match, best_score = max(scores, key=lambda x: x[1])
            similarities.append(best_score)

            if best_score >= 0.6:  # Original cutoff
                matches.append((team, best_match, best_score))
            else:
                unmatched.append((team, best_match, best_score))

    # Generate statistics
    stats = {
        "total_teams": len(team_names),
        "matched_teams": len(matches),
        "unmatched_teams": len(unmatched),
        "match_percentage": round(len(matches) / len(team_names) * 100, 2),
        "highest_similarity": round(max(similarities, default=0) * 100, 2),
        "lowest_similarity": round(min(similarities, default=0) * 100, 2),
        "average_similarity": round(mean(similarities) * 100, 2) if similarities else 0,
    }

    # Log results
    logger.info("\nMatching Statistics:")
    for key, value in stats.items():
        logger.info("%s: %s", key.replace("_", " ").title(), value)

    logger.info("\nUnmatched Teams (with top 5 matches):")
    name_mapping = {matcher.preprocess_name(f): f for f in matcher.logo_files}
    for team, _, _ in unmatched:
        processed_name = matcher.preprocess_name(team)
        scores = [
            (f, n, SequenceMatcher(None, processed_name, n).ratio())
            for n, f in name_mapping.items()
        ]
        top_5 = sorted(scores, key=lambda x: x[2], reverse=True)[:5]

        logger.info('"%s" -> Top matches:', team)
        for filename, _, score in top_5:
            logger.info('  - "%s" (%.2f%%)', filename, score * 100)

        # Value count for similarity scores
        bins = [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        labels = [f"{bins[i]}-{bins[i+1]}%" for i in range(len(bins) - 1)]

        counts = defaultdict(int)

        # Value count for similarity scores
        similarity_percentages = [score * 100 for _, _, score in matches + unmatched]
        bins = list(range(0, 101, 10))  # 0-10, 10-20, ..., 90-100

        # Value count for similarity scores
        bins = [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        labels = [f"{bins[i]}-{bins[i+1]}%" for i in range(len(bins) - 1)]

        counts = defaultdict(int)

        for score in similarity_percentages:
            for i in range(len(bins) - 1):
                if bins[i] <= score < bins[i + 1]:
                    counts[labels[i]] += 1
                    break

        logger.info("\nSimilarity Score Distribution:")
        for label in labels:
            count = counts[label]
            progress = "█" * int(count * 50 / len(similarity_percentages))
            logger.info(" %s: %s (%d)", label.ljust(8), progress, count)


if __name__ == "__main__":
    eval_logo_matching()
