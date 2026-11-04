"""Unit tests for utils/backfill_llm_predictions.py (Blob and LLMs mocked)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import polars as pl
import pytest
from lib import blob_archive

from utils import backfill_llm_predictions as bl

from .conftest import FakeBlob

OPTIONS = bl.PromptOptions("FourPointsScoring", named_teams=True, additional_info=True)
SUFFIX = "FourPointsScoring_named_with-info"
MINISTRAL = "Mistral-Ministral"
NEMOTRON = "Nvidia-Nemotron-Lightning"
BL = "Bundesliga - Germany"
CL = "UEFA Champions League"
TITLES = {BL: "Bundesliga - Germany", CL: "UEFA Champions League"}
CSV_HEADER = (
    "league,season,kickoff_utc,home_team,away_team,odds_home,odds_draw,odds_away,"
    "n_home,n_draw,n_away,source,flags\n"
)

# Runs: two before the late providers joined, one live run with them
R1, R2, LIVE = "2026-09-04_10-23-34", "2026-09-11_09-37-34", "2026-10-14_17-59-51"


def offer(
    run_ts: str,
    home: str,
    away: str,
    kickoff: str,
    provider: str = "Deepseek-Chat",
    league: str = BL,
) -> bl.Offer:
    return bl.Offer(run_ts, provider, league, home, away, kickoff, "h.png", "a.png")


def plan_for(offers: list[bl.Offer], first_live: str | None = LIVE) -> bl.Plan:
    return bl.plan_tasks(
        bl.group_fixtures(offers),
        [MINISTRAL],
        {MINISTRAL: first_live},
        "2026/27",
        lambda _: bl.Odds(2.0, 3.4, 3.6, "reconstructed"),
    )


# %% Run reconstruction


def test_fixture_goes_to_latest_run_before_kickoff() -> None:
    plan = plan_for(
        [
            offer(R1, "Bayern", "Köln", "12.09.2026 15:30"),
            offer(R2, "Bayern", "Köln", "12.09.2026 15:30"),
            offer(R2, "Bayern", "Köln", "12.09.2026 15:30", provider="Meta-Llama-Mid"),
        ]
    )
    assert [(t.provider, t.offer.run_ts) for t in plan.tasks] == [(MINISTRAL, R2)]


def test_run_after_kickoff_does_not_count() -> None:
    # R2 ran after the 05.09 kickoff (stale listing), so R1 is the run
    plan = plan_for(
        [
            offer(R1, "Bayern", "Köln", "05.09.2026 15:30"),
            offer(R2, "Bayern", "Köln", "05.09.2026 15:30"),
        ]
    )
    assert [t.offer.run_ts for t in plan.tasks] == [R1]


def test_covered_and_missed_live_fixtures_are_skipped() -> None:
    plan = plan_for(
        [
            # Ministral predicted this live: nothing to do
            offer(LIVE, "Bayern", "Dortmund", "17.10.2026 15:30"),
            offer(LIVE, "Bayern", "Dortmund", "17.10.2026 15:30", provider=MINISTRAL),
            # Offered in the live run, Ministral missed it (API error): out of scope
            offer(R2, "Mainz", "Bremen", "18.10.2026 15:30"),
            offer(LIVE, "Mainz", "Bremen", "18.10.2026 15:30"),
        ]
    )
    assert plan.tasks == []
    assert plan.missed_live[MINISTRAL] == 1


def test_other_seasons_are_ignored() -> None:
    plan = plan_for([offer("2026-05-01_10-00-00", "A", "B", "09.05.2026 15:30")])
    assert plan.tasks == []


def test_match_moved_earlier_ignores_stale_offers() -> None:
    # R1 and R2 listed 20.09; the last run moved it to 10.09, before R2 ran
    stale = plan_for(
        [
            offer(R1, "Bayern", "Köln", "20.09.2026 15:30"),
            offer(R2, "Bayern", "Köln", "20.09.2026 15:30"),
            offer("2026-09-30_10-00-00", "Bayern", "Köln", "10.09.2026 15:30"),
        ]
    )
    assert [t.offer.run_ts for t in stale.tasks] == [R1]


def test_rescheduled_kickoff_collapses_and_uses_run_offer() -> None:
    plan = plan_for(
        [
            offer(R1, "Bayern", "Köln", "12.09.2026 15:30"),
            offer(R2, "Bayern", "Köln", "20.09.2026 17:30"),
        ]
    )
    assert len(plan.tasks) == 1
    assert plan.tasks[0].offer.commence_time_str == "20.09.2026 17:30"


def test_uefa_repeat_pairing_stays_apart() -> None:
    plan = plan_for(
        [
            offer(R1, "Real", "City", "16.09.2026 21:00", league=CL),
            offer("2027-02-01_10-00-00", "Real", "City", "17.02.2027 21:00", league=CL),
        ],
        first_live="2027-03-01_00-00-00",
    )
    assert sorted(t.offer.run_ts for t in plan.tasks) == [R1, "2027-02-01_10-00-00"]


def test_first_live_runs() -> None:
    runs = [
        {"run_ts": R1, "predictions": {f"Deepseek-Chat_{SUFFIX}": "p"}},
        {"run_ts": LIVE, "predictions": {f"{MINISTRAL}_{SUFFIX}": "p"}},
    ]
    first = bl.first_live_runs(runs, [MINISTRAL, NEMOTRON], OPTIONS)
    assert first == {MINISTRAL: LIVE, NEMOTRON: None}


def test_load_offers_reads_only_the_prompt_combination() -> None:
    files = {
        "p/a.jsonl": [
            {
                "name": BL,
                "matches": [
                    {
                        "commence_time_str": "12.09.2026 15:30",
                        "home_team": "Bayern",
                        "away_team": "Köln",
                        "home_logo": "h.png",
                    }
                ],
            }
        ]
    }
    runs = [
        {
            "run_ts": R2,
            "predictions": {
                f"Deepseek-Chat_{SUFFIX}": "p/a.jsonl",
                "Deepseek-Chat_Default_named_with-info": "p/missing.jsonl",
            },
        },
        {"run_ts": "2025-01-01_00-00-00", "predictions": {"x": "p/missing.jsonl"}},
    ]
    offers = bl.load_offers(
        runs, OPTIONS, files.__getitem__, datetime(2026, 5, 1, tzinfo=UTC)
    )
    assert offers == [
        bl.Offer(R2, "Deepseek-Chat", BL, "Bayern", "Köln", "12.09.2026 15:30", "h.png")
    ]
    assert offers[0].kickoff == datetime(2026, 9, 12, 13, 30, tzinfo=UTC)


# %% Odds


def odds_file(rows: list[list[Any]]) -> list[dict[str, Any]]:
    return [{"name": BL, "columns": blob_archive.ODDS_COLUMNS, "matches": rows}]


def test_odds_lookup_prefers_logged_run_odds() -> None:
    csv_text = (
        CSV_HEADER
        + f"{BL},2026/27,2026-09-12T13:30:00+00:00,Bayern,Köln,1.3,6.0,9.0,,,,"
        "reconstructed,\n"
        + f"{BL},2026/27,2026-09-19T13:30:00+00:00,Mainz,Bremen,2.4,3.4,2.9,,,,"
        "logged,\n"
        + f"{BL},2026/27,2026-09-19T13:30:00+00:00,Hertha,Union,,3.4,2.9,,,,"
        "reconstructed,\n"
    )
    runs = [{"run_ts": R2, "odds": {"Bundesliga-Germany": "odds/r2.jsonl"}}]
    files = {
        "odds/r2.jsonl": odds_file(
            [["id1", "2026-09-12T13:30:00Z", "Bayern", "Köln", 1.25, 6.5, 11.0]]
        )
    }
    lookup = bl.OddsLookup(runs, files.__getitem__, csv_text)

    assert lookup(offer(R2, "Bayern", "Köln", "12.09.2026 15:30")) == bl.Odds(
        1.25, 6.5, 11.0, "logged"
    )
    # No run odds: CSV row, its source column decides logged vs. reconstructed
    assert lookup(offer(R1, "Bayern", "Köln", "12.09.2026 15:30")) == bl.Odds(
        1.3, 6.0, 9.0, "reconstructed"
    )
    # Rescheduled by a day: nearest CSV kickoff within the tolerance
    assert lookup(offer(R1, "Mainz", "Bremen", "20.09.2026 15:30")) == bl.Odds(
        2.4, 3.4, 2.9, "logged"
    )
    # Incomplete CSV row, unknown pairing: no odds
    assert lookup(offer(R1, "Hertha", "Union", "19.09.2026 15:30")) is None
    assert lookup(offer(R1, "X", "Y", "19.09.2026 15:30")) is None


def test_newest_reconstructed() -> None:
    index = {"reconstructed_odds": ["odds/reconstructed/2026-11-01.csv", "odds/a.csv"]}
    assert bl.newest_reconstructed(index) == "odds/reconstructed/2026-11-01.csv"
    assert bl.newest_reconstructed({}) is None


# %% Execution


def make_plan() -> bl.Plan:
    odds = bl.Odds(1.25, 6.5, 11.0, "reconstructed")
    tasks = [
        bl.Task(provider, offer(R2, home, "Köln", kickoff), odds)
        for provider in [MINISTRAL, NEMOTRON]
        for home, kickoff in [
            ("Bayern", "12.09.2026 15:30"),
            ("Mainz", "12.09.2026 18:30"),
        ]
    ]
    tasks.append(
        bl.Task(MINISTRAL, offer(R1, "Hertha", "Union", "05.09.2026 15:30"), None)
    )
    return bl.Plan(first_live={MINISTRAL: LIVE, NEMOTRON: LIVE}, tasks=tasks)


class FakePredictor:
    """Stands in for TipGenius.predict_results; fails the configured teams."""

    def __init__(self, fail: set[str] | None = None) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.fail = fail or set()
        self.abandoned: set[str] = set()

    def __call__(self, data: pl.DataFrame, provider: str) -> pl.DataFrame:
        self.calls.append((provider, data["odds_summary"].to_list()))
        ok = [home not in self.fail for home in data["home_team"]]
        return data.with_columns(
            pl.lit("why").alias("reasoning"),
            pl.lit(2).alias("prediction_home"),
            pl.lit(1).alias("prediction_away"),
            pl.lit("outlook").alias("outlook"),
            pl.Series("validity", [int(v) for v in ok]),
        )


def run_plan(
    plan: bl.Plan,
    index: dict[str, Any],
    predictor: FakePredictor,
    *,
    execute: bool = True,
    max_calls: int | None = None,
    now: datetime = datetime(2026, 11, 4, 12, 0, tzinfo=UTC),  # a Wednesday
) -> dict[tuple[str, str], bl.Outcome]:
    return bl.execute_plan(
        plan,
        index,
        OPTIONS,
        TITLES,
        execute=execute,
        max_calls=max_calls,
        sleep=0.0,
        predict_factory=lambda: predictor,
        now=lambda: now,
    )


@pytest.fixture
def live_index(fake_blob: FakeBlob) -> dict[str, Any]:
    index = blob_archive.merge_index(
        None,
        [
            {
                "run_ts": R2,
                "source": "backfill",
                "odds": {},
                "predictions": {f"Deepseek-Chat_{SUFFIX}": "predictions/d.jsonl"},
            }
        ],
    )
    fake_blob.files[blob_archive.INDEX_PATHNAME] = blob_archive.to_json(index)
    return index


def path_of(provider: str, run_ts: str = R2) -> str:
    return blob_archive.backfill_predictions_pathname(
        run_ts, OPTIONS.base_key(provider)
    )


def test_dry_run_calls_and_writes_nothing(
    fake_blob: FakeBlob, live_index: dict[str, Any]
) -> None:
    predictor = FakePredictor()
    outcomes = run_plan(make_plan(), live_index, predictor, execute=False)
    assert predictor.calls == []
    assert fake_blob.puts == []
    assert fake_blob.heads == []
    assert {k: (o.status, o.rows) for k, o in outcomes.items()} == {
        (R2, MINISTRAL): ("planned", 2),
        (R2, NEMOTRON): ("planned", 2),
    }


def test_execute_writes_marked_files_and_indexes_them_apart(
    fake_blob: FakeBlob, live_index: dict[str, Any]
) -> None:
    predictor = FakePredictor(fail={"Mainz"})
    outcomes = run_plan(make_plan(), live_index, predictor)

    assert predictor.calls[0] == (
        MINISTRAL,
        [
            "Bayern: 1.25, Köln: 11.0, draw: 6.5, Bundesliga - Germany",
            "Mainz: 1.25, Köln: 11.0, draw: 6.5, Bundesliga - Germany",
        ],
    )
    assert outcomes[R2, MINISTRAL].status == "written"
    assert outcomes[R2, MINISTRAL].predicted == 1
    assert [t.offer.home_team for t in outcomes[R2, MINISTRAL].failed] == ["Mainz"]

    lines = [json.loads(x) for x in fake_blob.files[path_of(MINISTRAL)].splitlines()]
    assert lines == [
        {
            "name": BL,
            "timestamp": "2026-09-11 09:37:34",
            "matches": [
                {
                    "commence_time_str": "12.09.2026 15:30",
                    "home_team": "Bayern",
                    "away_team": "Köln",
                    "prediction_home": 2,
                    "prediction_away": 1,
                    "outlook": "outlook",
                    "reasoning": "why",
                    "home_logo": "h.png",
                    "away_logo": "a.png",
                }
            ],
            "backfill": {
                "generated_at": "2026-11-04 12:00:00",
                "odds_source": "reconstructed",
                "script": "backfill_llm_predictions.py",
            },
        }
    ]
    index = json.loads(fake_blob.files[blob_archive.INDEX_PATHNAME])
    assert index["runs"] == live_index["runs"]
    assert index["backfill_predictions"] == {
        R2: {
            OPTIONS.provider_key(MINISTRAL): path_of(MINISTRAL),
            OPTIONS.provider_key(NEMOTRON): path_of(NEMOTRON),
        }
    }
    # Write-once, never into live predictions/ or KV-style names
    assert all(
        not p["allow_overwrite"]
        for p in fake_blob.puts
        if p["pathname"] != "index.json"
    )
    assert {p["pathname"] for p in fake_blob.puts if p["pathname"] != "index.json"} == {
        path_of(MINISTRAL),
        path_of(NEMOTRON),
    }


def test_rerun_skips_archived_files(
    fake_blob: FakeBlob, live_index: dict[str, Any]
) -> None:
    run_plan(make_plan(), live_index, FakePredictor())
    index = json.loads(fake_blob.files[blob_archive.INDEX_PATHNAME])
    predictor = FakePredictor()
    outcomes = run_plan(make_plan(), index, predictor)
    assert predictor.calls == []
    assert {o.status for o in outcomes.values()} == {"archived"}


def test_unindexed_file_is_indexed_without_llm_calls(
    fake_blob: FakeBlob, live_index: dict[str, Any]
) -> None:
    fake_blob.files[path_of(MINISTRAL)] = b"{}\n"
    predictor = FakePredictor()
    outcomes = run_plan(make_plan(), live_index, predictor)
    assert outcomes[R2, MINISTRAL].status == "indexed"
    assert fake_blob.files[path_of(MINISTRAL)] == b"{}\n"
    assert [provider for provider, _ in predictor.calls] == [NEMOTRON]
    index = json.loads(fake_blob.files[blob_archive.INDEX_PATHNAME])
    assert OPTIONS.provider_key(MINISTRAL) in index["backfill_predictions"][R2]


def test_max_calls_stops_before_exceeding(
    fake_blob: FakeBlob, live_index: dict[str, Any]
) -> None:
    predictor = FakePredictor()
    outcomes = run_plan(make_plan(), live_index, predictor, max_calls=3)
    assert [provider for provider, _ in predictor.calls] == [MINISTRAL]
    assert outcomes[R2, NEMOTRON].status == "skipped"
    assert path_of(NEMOTRON) not in fake_blob.files


def test_max_calls_stops_at_first_file_over_the_limit(
    fake_blob: FakeBlob, live_index: dict[str, Any]
) -> None:
    plan = make_plan()
    # A later one-fixture file would fit the remaining budget, but the run stops
    plan.tasks.append(
        bl.Task(
            MINISTRAL,
            offer("2026-09-18_08-40-09", "Mainz", "Bremen", "19.09.2026 15:30"),
            bl.Odds(2.0, 3.4, 3.6, "reconstructed"),
        )
    )
    predictor = FakePredictor()
    outcomes = run_plan(plan, live_index, predictor, max_calls=3)
    assert [provider for provider, _ in predictor.calls] == [MINISTRAL]
    assert outcomes["2026-09-18_08-40-09", MINISTRAL].status == "skipped"


def test_cron_window_stops_execution(
    fake_blob: FakeBlob, live_index: dict[str, Any]
) -> None:
    tuesday = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
    assert bl.near_cron(tuesday)
    assert not bl.near_cron(datetime(2026, 11, 3, 18, 0, tzinfo=UTC))
    assert not bl.near_cron(datetime(2026, 11, 4, 9, 0, tzinfo=UTC))
    predictor = FakePredictor()
    outcomes = run_plan(make_plan(), live_index, predictor, now=tuesday)
    assert predictor.calls == []
    assert {o.status for o in outcomes.values()} == {"skipped"}


def test_failed_head_check_skips_instead_of_paying_twice(
    fake_blob: FakeBlob, live_index: dict[str, Any]
) -> None:
    fake_blob.fail_heads = blob_archive.vercel_blob.BlobError("rate", status_code=429)
    predictor = FakePredictor()
    outcomes = run_plan(make_plan(), live_index, predictor)
    assert predictor.calls == []
    assert {o.status for o in outcomes.values()} == {"skipped"}


def test_abandoned_provider_is_skipped(
    fake_blob: FakeBlob, live_index: dict[str, Any]
) -> None:
    predictor = FakePredictor()
    predictor.abandoned.add(MINISTRAL)
    outcomes = run_plan(make_plan(), live_index, predictor, max_calls=2)
    assert outcomes[R2, MINISTRAL].status == "skipped"
    # Its rows do not use up the budget
    assert outcomes[R2, NEMOTRON].status == "written"


def test_no_valid_prediction_writes_no_file(
    fake_blob: FakeBlob, live_index: dict[str, Any]
) -> None:
    outcomes = run_plan(
        make_plan(), live_index, FakePredictor(fail={"Bayern", "Mainz"})
    )
    assert outcomes[R2, MINISTRAL].status == "failed"
    assert len(outcomes[R2, MINISTRAL].failed) == 2
    assert path_of(MINISTRAL) not in fake_blob.files
    index = json.loads(fake_blob.files[blob_archive.INDEX_PATHNAME])
    assert "backfill_predictions" not in index


def test_report_lists_counts(capsys: pytest.CaptureFixture[str]) -> None:
    plan = make_plan()
    outcomes = {
        (R2, MINISTRAL): bl.Outcome("planned", rows=2),
        (R2, NEMOTRON): bl.Outcome("planned", rows=2),
    }
    bl.print_report(
        plan,
        outcomes,
        [MINISTRAL, NEMOTRON],
        execute=False,
        odds_label="x.csv",
        llm_attempts=4,
    )
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert f"{MINISTRAL}: 3 (Bundesliga 3)" in out
    assert f"{MINISTRAL}: 2 fixtures in 1 files -> 2 to 8 requests" in out
    assert "Without odds, not predicted: 1" in out
    assert "Hertha - Union" in out
