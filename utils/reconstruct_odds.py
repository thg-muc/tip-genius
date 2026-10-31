r"""Reconstruct missing pre-match 1X2 odds from the LLM reasoning texts.

The workflow did not keep the odds it fed to the LLMs, but most providers
quote them in their ``reasoning`` ("odds of 1.57", "45.5% for Espanyol",
"draw sits at 3.8"). This script extracts those numbers per provider from the
last snapshot before kickoff, converts implied probabilities with 1/p, and
combines providers per match with the median.

True odds from local files (``data/llm_data`` CSVs and ``data/api_result``)
are used where they exist and to validate the reconstruction. The result is a
CSV with one row per backtest fixture that ``utils/eval_providers.py`` can
read with ``--odds-csv``. KV is only read.

Usage::

    uv run python utils/reconstruct_odds.py --env-file /path/to/.env.local \
        --data-dir /path/to/data --out /path/to/data/reconstructed_odds.csv
"""

# * Author(s): Thomas Glanzer
# * Creation : Oct 2026
# * License: MIT license

# %% --------------------------------------------
# * Libraries

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import tempfile
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median

import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parents[1] / "src" / "tip_genius"))

from eval_providers import (  # noqa: E402
    NAME_STOPWORDS,
    REPO_ROOT,
    HttpCache,
    Prediction,
    flatten,
    group_runs,
    last_before_kickoff,
    load_kv_snapshots,
    load_local_snapshots,
    load_results,
    logger,
    match_results,
    normalize_team,
    season_of,
)

# %% --------------------------------------------
# * Constants

OUTCOMES = ("home", "draw", "away")
MAX_DEVIATION = 0.20
MIN_ODDS, MAX_ODDS = 1.01, 60.0
# Words that only describe a team name in general, too ambiguous to attribute
AMBIGUOUS_WORDS = NAME_STOPWORDS | {
    "real", "united", "city", "club", "athletic", "sporting", "borussia",
    "inter", "racing", "dynamo", "olympique", "eintracht", "hotspur", "town",
    "county", "rovers", "wanderers", "albion", "saint", "st", "munich",
}  # fmt: skip
# Odds read by hand from the reasoning texts where providers disagreed, so the
# median rule left an outcome empty; each value is quoted verbatim in at least
# one provider's reasoning (home, draw, away)
MANUAL_ODDS = {
    ("La Liga - Spain", "Celta Vigo", "Espanyol", "2024/25"): (1.6, 3.8, 5.8),
    ("La Liga - Spain", "Athletic Bilbao", "Rayo Vallecano", "2024/25"): (
        1.67,
        3.5,
        5.7,
    ),
    ("La Liga - Spain", "Celta Vigo", "Getafe", "2025/26"): (1.62, 3.7, 5.8),
    ("La Liga - Spain", "Athletic Bilbao", "Sevilla", "2025/26"): (1.57, 3.9, 6.0),
    ("UEFA Champions League", "Qarabağ FK", "Newcastle United", "2025/26"): (
        7.7,
        5.05,
        1.41,
    ),
}
# Short names the LLMs use in their reasoning, keyed by part of the team name
NICKNAMES = {
    "paris saint germain": ["psg", "paris"],
    "wolverhampton": ["wolves"],
    "tottenham": ["spurs"],
    "manchester city": ["man city", "city"],
    "manchester united": ["man utd", "man united", "united"],
    "barcelona": ["barca"],
    "atletico madrid": ["atletico", "atleti"],
    "real madrid": ["madrid"],
    "bayern": ["bayern"],
    "borussia monchengladbach": ["gladbach"],
    "borussia dortmund": ["dortmund", "bvb"],
    "bayer leverkusen": ["leverkusen"],
    "internazionale": ["inter"],
    "athletic bilbao": ["athletic", "bilbao"],
    "newcastle": ["magpies"],
    "nottingham forest": ["forest"],
    "west ham": ["hammers"],
    "arsenal": ["gunners"],
    "brighton": ["seagulls"],
}
BOUND_WORDS = re.compile(
    r"(over|above|more than|less than|under|at least|nearly|almost|below|"
    r"exceed\w*|up to|>|<|≥|≤)\s*[~≈]?\s*$"
)
DRAW_PATTERN = re.compile(r"\b(draw|draws|tie|stalemate|level)\b")
HOME_PATTERN = re.compile(r"\b(home win|home side|home team|hosts|home)\b")
AWAY_PATTERN = re.compile(r"\b(away win|away side|away team|visitors|away)\b")
DECIMAL_PATTERN = re.compile(
    r"(?<![\d.:/\-])(?:1\s?/\s?)?(\d{1,2}\.\d{1,2})(?![\d%:\-])(?!\s?(?:goals?|xg|per))"
)
ODDS_INT_PATTERN = re.compile(r"\bodds (?:of |at )?(\d{1,2})(?![\d.%:\-])")
PERCENT_PATTERN = re.compile(r"(\d{1,2}(?:\.\d{1,2})?)\s?%")


# %% --------------------------------------------
# * Extraction


def fold(text: str) -> str:
    """Lowercase and strip accents while keeping every character position."""
    out = []
    for char in text:
        ascii_char = unicodedata.normalize("NFKD", char).encode("ascii", "ignore")
        out.append(ascii_char.decode()[:1] or char)
    return "".join(out).lower()


def team_patterns(team: str, other: str) -> re.Pattern[str]:
    """Regex matching mentions of a team by full name or distinctive words."""
    own = normalize_team(team).split()
    others = set(normalize_team(other).split())
    words = [w for w in own if len(w) >= 4 and w not in AMBIGUOUS_WORDS | others]  # noqa: PLR2004
    names = [re.escape(fold(team))] + [re.escape(w) for w in words]
    other_folded = fold(other)
    for part, nicknames in NICKNAMES.items():
        if part in fold(team):
            names += [re.escape(n) for n in nicknames if n not in other_folded]
    return re.compile(r"\b(" + "|".join(names) + r")\b")


def mentions(text: str, home: str, away: str) -> list[tuple[int, int, str]]:
    """Return (start, end, outcome) for every mention of an outcome."""
    found = []
    patterns = [
        (team_patterns(home, away), "home"),
        (team_patterns(away, home), "away"),
        (HOME_PATTERN, "home"),
        (AWAY_PATTERN, "away"),
        (DRAW_PATTERN, "draw"),
    ]
    for pattern, outcome in patterns:
        found.extend((m.start(), m.end(), outcome) for m in pattern.finditer(text))
    return sorted(found)


def sentence_bounds(text: str, pos: int) -> tuple[int, int]:
    """Return start and end of the sentence around a position."""
    start = max(text.rfind(". ", 0, pos), text.rfind("\n", 0, pos))
    end_candidates = [i for i in (text.find(". ", pos), text.find("\n", pos)) if i >= 0]
    return start + 1, min(end_candidates, default=len(text))


def attribute(
    text: str, start: int, end: int, found: list[tuple[int, int, str]]
) -> str | None:
    """Attribute a number to the outcome it most likely belongs to.

    "45.5% for Espanyol" attaches to the following mention; otherwise the
    nearest preceding mention in the same sentence wins, then the nearest
    following one.
    """
    after = text[end : end + 12]
    if re.match(r"[)\s]*(for|on|to)\b", after):
        following = [m for m in found if end <= m[0] <= end + 30]
        if following:
            return following[0][2]
    sent_start, sent_end = sentence_bounds(text, start)
    before = [m for m in found if sent_start <= m[1] <= start]
    if before:
        return before[-1][2]
    following = [m for m in found if end <= m[0] <= min(sent_end, end + 40)]
    return following[0][2] if following else None


def extract_odds(reasoning: str, home: str, away: str) -> dict[str, float]:
    """Extract decimal odds per outcome from one reasoning text.

    Decimal odds quoted in the text take precedence over odds derived from
    implied probabilities. Bounds such as "over 57%" are ignored.
    """
    text = fold(reasoning)
    found = mentions(text, home, away)
    decimals: dict[str, float] = {}
    from_percent: dict[str, float] = {}
    for pattern in (DECIMAL_PATTERN, ODDS_INT_PATTERN):
        for match in pattern.finditer(text):
            value = float(match[1])
            if not MIN_ODDS <= value <= MAX_ODDS:
                continue
            outcome = attribute(text, match.start(), match.end(), found)
            if outcome:
                decimals.setdefault(outcome, value)
    for match in PERCENT_PATTERN.finditer(text):
        if BOUND_WORDS.search(text[max(0, match.start() - 15) : match.start()]):
            continue
        percent = float(match[1])
        if not 1 <= percent <= 99:  # noqa: PLR2004
            continue
        outcome = attribute(text, match.start(), match.end(), found)
        if outcome:
            from_percent.setdefault(outcome, round(100 / percent, 2))
    return {
        o: decimals.get(o) or from_percent[o]
        for o in OUTCOMES
        if o in decimals or o in from_percent
    }


# %% --------------------------------------------
# * Combination


@dataclass
class Reconstructed:
    """Median odds per outcome across providers."""

    odds: dict[str, float | None] = field(default_factory=dict)
    sources: dict[str, int] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)


def combine(per_provider: list[dict[str, float]]) -> Reconstructed:
    """Combine provider extractions with the median, dropping outliers."""
    result = Reconstructed()
    for outcome in OUTCOMES:
        values = [p[outcome] for p in per_provider if outcome in p]
        if not values:
            result.odds[outcome], result.sources[outcome] = None, 0
            continue
        mid = median(values)
        kept = [v for v in values if abs(v - mid) / mid <= MAX_DEVIATION]
        if not kept:
            result.odds[outcome], result.sources[outcome] = None, 0
            result.flags.append(f"{outcome}:conflict")
            continue
        result.odds[outcome] = round(median(kept), 2)
        result.sources[outcome] = len(kept)
        if len(values) == 1:
            result.flags.append(f"{outcome}:single")
    return result


# %% --------------------------------------------
# * True odds from local files


def sport_names() -> dict[str, str]:
    """Map Odds API sport keys to the league names used in snapshots."""
    path = REPO_ROOT / "src" / "tip_genius" / "cfg" / "api_config.yaml"
    mapping = yaml.safe_load(path.read_text())["odds_api"]["sports_mapping"]
    return {key: name for name, key in mapping.items()}


def load_true_odds(data_dir: Path) -> dict[tuple, tuple[datetime, dict[str, float]]]:
    """Load logged odds keyed by (league, home, away, season).

    Keeps the latest file taken before kickoff. Raw ``api_result`` files are
    run through production's own bookmaker selection.
    """
    from lib.api_data import OddsAPI  # noqa: PLC0415

    names = sport_names()
    config_path = REPO_ROOT / "src" / "tip_genius" / "cfg" / "api_config.yaml"
    pipeline = object.__new__(OddsAPI)
    pipeline.bookmaker_priority = yaml.safe_load(config_path.read_text())["odds_api"][
        "bookmaker_priority"
    ]
    truth: dict[tuple, tuple[datetime, dict[str, float]]] = {}

    def add(stamp: datetime, row: dict) -> None:
        league = names.get(row["sport_key"])
        kickoff = datetime.fromisoformat(row["commence_time"])
        odds = {o: float(row[f"odds_{o}"]) for o in OUTCOMES}
        if not league or stamp >= kickoff or min(odds.values()) <= 0:
            return
        key = (league, row["home_team"], row["away_team"], season_of(kickoff))
        if key not in truth or truth[key][0] < stamp:
            truth[key] = (stamp, odds)

    def stamp_of(path: Path) -> datetime:
        return datetime.strptime(path.name[:19], "%Y-%m-%d_%H-%M-%S").replace(
            tzinfo=UTC
        )

    for path in sorted((data_dir / "llm_data").glob("20*.csv")):
        with path.open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                add(stamp_of(path), row)
    for path in sorted((data_dir / "api_result").glob("20*.json")):
        raw = json.loads(path.read_text())
        if not raw:
            continue
        try:
            data = pipeline.process_api_data(
                raw, named_teams=True, additional_info=True
            )
        except Exception:  # noqa: BLE001
            logger.warning("Skipping unreadable odds file %s", path.name)
            continue
        for row in data.iter_rows(named=True):
            add(stamp_of(path), row)
    logger.info("Loaded logged odds for %d fixtures", len(truth))
    return truth


# %% --------------------------------------------
# * Main


def summarize_errors(errors: list[float]) -> str:
    """Describe a list of relative errors."""
    errors = sorted(errors)
    return (
        f"{len(errors)} values, median error {median(errors):.1%}, "
        f"within 1%: {sum(e <= 0.01 for e in errors) / len(errors):.1%}, "  # noqa: PLR2004
        f"within 5%: {sum(e <= 0.05 for e in errors) / len(errors):.1%}, "  # noqa: PLR2004
        f"within 10%: {sum(e <= 0.10 for e in errors) / len(errors):.1%}, "  # noqa: PLR2004
        f"worst: {errors[-1]:.0%}"
    )


def validate_same_run(data_dir: Path) -> None:
    """Validate extraction on llm_data rows, where reasoning and odds match.

    Each CSV row holds the reasoning and the exact odds of the same LLM call,
    so this measures the extraction without odds drift between runs. Rows of
    one run and match are combined across providers like the reconstruction.
    """
    files = sorted((data_dir / "llm_data").glob("20*.csv"))
    stamps = {
        f: datetime.strptime(f.name[:19], "%Y-%m-%d_%H-%M-%S").replace(tzinfo=UTC)
        for f in files
    }
    run_of = group_runs(set(stamps.values()))
    per_provider: dict[str, list[float]] = defaultdict(list)
    attempted: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    groups: dict[tuple, list[dict[str, float]]] = defaultdict(list)
    truth: dict[tuple, dict[str, float]] = {}
    for path in files:
        provider = path.name.split("_")[3]
        with path.open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                odds = {o: float(row[f"odds_{o}"] or 0) for o in OUTCOMES}
                if not row.get("reasoning") or min(odds.values()) <= 0:
                    continue
                found = extract_odds(
                    row["reasoning"], row["home_team"], row["away_team"]
                )
                attempted[provider][0] += 1
                attempted[provider][1] += len(found) == len(OUTCOMES)
                key = (run_of[stamps[path]], row["id"])
                truth[key] = odds
                groups[key].append(found)
                per_provider[provider].extend(
                    abs(v - odds[o]) / odds[o] for o, v in found.items()
                )
    print("\n=== Same-run validation on data/llm_data (exact odds known) ===")
    for provider, errors in sorted(per_provider.items()):
        rows, full = attempted[provider]
        if errors:
            print(
                f"{provider:<30} {rows:>4} texts, all 3 found in {full / rows:>5.1%}; "
                + summarize_errors(errors)
            )
    combined_errors = []
    full = 0
    for key, extractions in groups.items():
        rec = combine(extractions)
        full += all(rec.odds.values())
        combined_errors.extend(
            abs(v - truth[key][o]) / truth[key][o] for o, v in rec.odds.items() if v
        )
    print(
        f"{'combined (median)':<30} {len(groups):>4} matches, all 3 in "
        f"{full / len(groups):>5.1%}; " + summarize_errors(combined_errors)
    )


def validate(
    estimates: dict[tuple, dict[str, float | None]],
    truth: dict[tuple, tuple[datetime, dict[str, float]]],
    label: str,
) -> None:
    """Print how close estimated odds are to logged odds."""
    errors = []
    covered = full = 0
    for key, odds in estimates.items():
        if key not in truth:
            continue
        covered += 1
        full += all(odds.get(o) for o in OUTCOMES)
        for outcome in OUTCOMES:
            value = odds.get(outcome)
            if value:
                true = truth[key][1][outcome]
                errors.append(abs(value - true) / true)
    if not errors:
        print(f"{label}: no overlap with logged odds")
        return
    print(
        f"{label}: {covered} fixtures with logged odds, {full} with all 3; "
        + summarize_errors(errors)
    )


def main() -> None:
    """Reconstruct odds, validate them and write the CSV."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--env-file", type=Path, default=REPO_ROOT / ".env.local")
    parser.add_argument("--data-dir", type=Path, default=REPO_ROOT / "data")
    parser.add_argument(
        "--out", type=Path, default=REPO_ROOT / "data" / "reconstructed_odds.csv"
    )
    args = parser.parse_args()
    load_dotenv(args.env_file)

    snapshots = load_kv_snapshots()
    first_kv = min(stamp for stamp, _ in snapshots)
    snapshots |= load_local_snapshots(args.data_dir / "match_predictions", first_kv)
    now = datetime.now(tz=UTC)
    selected = [
        p
        for p in last_before_kickoff(flatten(snapshots))
        if p.kickoff < now - timedelta(hours=3)
    ]
    cache = HttpCache(Path(tempfile.gettempdir()) / "tip-genius-eval-cache")
    matched = match_results(selected, load_results(cache, selected))
    selected = [p for p in selected if (p.league, p.home, p.away, p.kickoff) in matched]

    by_fixture: dict[tuple, list[Prediction]] = defaultdict(list)
    for pred in selected:
        by_fixture[pred.league, pred.home, pred.away, season_of(pred.kickoff)].append(
            pred
        )
    truth = load_true_odds(args.data_dir)
    validate_same_run(args.data_dir)

    extracted: dict[tuple, dict[str, dict[str, float]]] = {}
    for key, preds in by_fixture.items():
        extracted[key] = {
            p.provider: extract_odds(p.reasoning, p.home, p.away)
            for p in preds
            if p.reasoning
        }
    reconstructed = {key: combine(list(ex.values())) for key, ex in extracted.items()}

    print("\n=== Snapshot reasoning vs latest logged odds (may differ by run) ===")
    providers = sorted({prov for ex in extracted.values() for prov in ex})
    for provider in providers:
        single = {key: ex[provider] for key, ex in extracted.items() if provider in ex}
        validate(single, truth, f"{provider:<30}")  # type: ignore[arg-type]
    print("\n=== Combined (median) vs logged odds ===")
    validate({k: r.odds for k, r in reconstructed.items()}, truth, "combined")

    rows = []
    for key, preds in sorted(by_fixture.items(), key=lambda item: item[1][0].kickoff):
        league, home, away, season = key
        rec = reconstructed[key]
        if key in truth:
            odds, sources, source, flags = truth[key][1], {}, "logged", []
        elif key in MANUAL_ODDS and not all(rec.odds.values()):
            odds = dict(zip(OUTCOMES, MANUAL_ODDS[key], strict=True))
            sources, source, flags = {}, "manual", []
        else:
            odds, sources, source, flags = (
                rec.odds,
                rec.sources,
                "reconstructed",
                rec.flags,
            )
        rows.append(
            {
                "league": league,
                "season": season,
                "kickoff_utc": preds[0].kickoff.isoformat(),
                "home_team": home,
                "away_team": away,
                "odds_home": odds.get("home") or "",
                "odds_draw": odds.get("draw") or "",
                "odds_away": odds.get("away") or "",
                "n_home": sources.get("home", ""),
                "n_draw": sources.get("draw", ""),
                "n_away": sources.get("away", ""),
                "source": source,
                "flags": ";".join(flags),
            }
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    missing = [r for r in rows if r["source"] != "logged"]
    full = [r for r in missing if all(r[f"odds_{o}"] for o in OUTCOMES)]
    partial = [r for r in missing if any(r[f"odds_{o}"] for o in OUTCOMES)]
    flagged = [r for r in full if r["flags"]]
    print("\n=== Coverage ===")
    print(
        f"Backtest fixtures: {len(rows)}, with logged odds: {len(rows) - len(missing)}"
    )
    print(
        f"Without logged odds: {len(missing)}; now with all 3 odds: {len(full)} "
        f"({len(full) / max(len(missing), 1):.1%}), of which flagged: {len(flagged)}; "
        f"partial: {len(partial) - len(full)}; none: {len(missing) - len(partial)}; "
        f"read by hand: {sum(r['source'] == 'manual' for r in missing)}"
    )
    by_season: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in missing:
        by_season[r["season"]][0] += 1
        by_season[r["season"]][1] += r in full
    for season, (total, ok) in sorted(by_season.items()):
        print(f"  {season}: {ok}/{total} reconstructed with all 3 odds")
    print(f"\nWrote {len(rows)} rows to {args.out}")


if __name__ == "__main__":
    main()
