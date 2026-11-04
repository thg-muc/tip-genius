"""The per-match prompt is built in one place, shared by workflow and backfill."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from lib.api_data import OddsAPI, build_odds_summary

from utils import backfill_llm_predictions as bl

CFG = Path(__file__).parents[2] / "src" / "tip_genius" / "cfg"


def api_match(
    home: str, away: str, prices: tuple[float, float, float], title: str
) -> dict[str, Any]:
    outcomes = [
        {"name": home, "price": prices[0]},
        {"name": away, "price": prices[2]},
        {"name": "Draw", "price": prices[1]},
    ]
    return {
        "id": "abc",
        "sport_key": "soccer_epl",
        "sport_title": title,
        "commence_time": "2026-09-12T14:00:00Z",
        "home_team": home,
        "away_team": away,
        "bookmakers": [
            {"key": "tipico_de", "markets": [{"key": "h2h", "outcomes": outcomes}]}
        ],
    }


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> OddsAPI:
    monkeypatch.setenv("ODDS_API_KEY", "test-key")
    return OddsAPI()


def test_live_prompt_format_is_unchanged(api: OddsAPI) -> None:
    # Literal guard: the refactor must not change what the LLMs receive
    raw: Any = [api_match("Arsenal", "Chelsea", (1.5, 4.2, 5.0), "EPL")]
    named = api.process_api_data(raw, named_teams=True, additional_info=True)
    assert named["odds_summary"][0] == "Arsenal: 1.5, Chelsea: 5.0, draw: 4.2, EPL"
    anonymized = api.process_api_data(raw, named_teams=False, additional_info=False)
    assert anonymized["odds_summary"][0] == "home: 1.5, away: 5.0, draw: 4.2"


@pytest.mark.parametrize("named_teams", [True, False])
@pytest.mark.parametrize("additional_info", [True, False])
def test_shared_function_matches_process_api_data(
    api: OddsAPI, *, named_teams: bool, additional_info: bool
) -> None:
    raw: Any = [api_match("Bayern München", "1. FC Köln", (1.25, 6.5, 11.0), "EPL")]
    data = api.process_api_data(
        raw, named_teams=named_teams, additional_info=additional_info
    )
    expected = build_odds_summary(
        home_team="Bayern München",
        away_team="1. FC Köln",
        odds_home=1.25,
        odds_away=11.0,
        odds_draw=6.5,
        sport_title="EPL",
        named_teams=named_teams,
        additional_info=additional_info,
    )
    assert data["odds_summary"][0] == expected


def test_backfill_prompt_is_byte_identical_to_live(api: OddsAPI) -> None:
    """Archived odds for a Premier League match give the live prompt."""
    raw: Any = [api_match("Arsenal", "Chelsea", (2.1, 3.4, 3.3), "EPL")]
    live = api.process_api_data(raw, named_teams=True, additional_info=True)

    offer = bl.Offer(
        "2026-09-11_09-37-34",
        "Deepseek-Chat",
        "Premier League - England",
        "Arsenal",
        "Chelsea",
        "12.09.2026 16:00",
    )
    task = bl.Task("Mistral-Ministral", offer, bl.Odds(2.1, 3.4, 3.3, "logged"))
    options = bl.load_prompt_options(CFG / "tip_genius_config.yaml")
    titles = bl.load_sport_titles(CFG / "api_config.yaml")
    frame = bl.build_frame([task], titles["Premier League - England"], options)

    assert frame["odds_summary"][0].encode() == live["odds_summary"][0].encode()
    for column in ["odds_home", "odds_draw", "odds_away", "reasoning", "validity"]:
        assert frame[column].dtype == live[column].dtype
    assert frame["prediction_home"].dtype == live["prediction_home"].dtype


def test_csv_odds_format_like_api_floats() -> None:
    # CSV strings become floats, so "3.0" and "3" both print as the API's 3.0
    assert bl.valid_odds("2.1", "3", "3.0") == (2.1, 3.0, 3.0)
    assert bl.valid_odds("", "3", "3.0") is None
    assert bl.valid_odds("0", "3", "3.0") is None


def test_sport_titles_cover_configured_sports() -> None:
    with (CFG / "tip_genius_config.yaml").open(encoding="utf-8") as f:
        sports = yaml.safe_load(f)["sports_list"]
    titles = bl.load_sport_titles(CFG / "api_config.yaml")
    assert set(sports) <= set(titles)
    assert titles["Premier League - England"] == "EPL"
