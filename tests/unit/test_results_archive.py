"""Unit tests for the Blob results archive (Blob SDK and result sources mocked)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from typing import Any

import pytest
from lib import blob_archive
from lib import results_archive as ra
from lib import storage_manager as sm_module
from lib.storage_manager import StorageManager

from utils import backfill_results
from utils import eval_providers as ev

from .conftest import FakeBlob

BL = "Bundesliga - Germany"
PL = "Premier League - England"
SEASON = "2026/27"
NOW = datetime(2026, 11, 10, 3, 0, tzinfo=UTC)
RUN_TS = "2026-11-03_03-00-05"
PRED_PATH = blob_archive.predictions_pathname(
    RUN_TS, "Match_Predictions_Deepseek-Chat_FourPointsScoring_named_with-info"
)
BL_PATH = ra.results_pathname(SEASON, BL)


def record(when: str, home: str, away: str) -> dict[str, Any]:
    """Return a prediction record as the workflow writes it."""
    return {
        "commence_time_str": when,
        "home_team": home,
        "away_team": away,
        "prediction_home": 2,
        "prediction_away": 1,
    }


def seed(fake: FakeBlob, matches: dict[str, list[dict[str, Any]]]) -> None:
    """Store one prediction file and an index listing it."""
    payload = blob_archive.build_prediction_payload(matches, "2026-11-03 03:00:05")
    fake.files[PRED_PATH] = blob_archive.to_jsonl(payload)
    index = blob_archive.merge_index(
        None, [{"run_ts": RUN_TS, "predictions": {"Deepseek": PRED_PATH}}]
    )
    fake.files[blob_archive.INDEX_PATHNAME] = blob_archive.to_json(index)


def bl_result(kickoff: datetime, home: str, away: str, goals: tuple[int, int]) -> Any:
    return ra.Result(
        BL, kickoff, home, away, *goals, status="finished", source="openligadb"
    )


@pytest.fixture
def sources(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the result sources; record which fixtures were requested."""
    state: dict[str, Any] = {"results": [], "requested": []}

    def fetch(_cache: Any, fixtures: Any, _now: Any = None) -> list[ra.Result]:
        state["requested"].extend(fixtures)
        return state["results"]

    monkeypatch.setattr(ra, "fetch_results", fetch)
    return state


def run(**kwargs: Any) -> ra.ArchiveReport:
    return ra.update_results_archive({SEASON}, now=NOW, wait=0, **kwargs)


def csv_rows(fake: FakeBlob, path: str = BL_PATH) -> list[dict[str, str]]:
    return ra.rows_from_csv(fake.files[path])


# %% match_key and scores


def test_match_key_from_prediction_record_converts_berlin_to_utc() -> None:
    winter = record("07.11.2026 15:30", "Bayern Munich", "Union Berlin")
    summer = record("22.08.2026 15:30", "Bayern Munich", "Union Berlin")
    assert ra.prediction_match_key(BL, winter) == (
        "Bundesliga - Germany|2026-11-07T14:30Z|Bayern Munich|Union Berlin"
    )
    assert ra.prediction_match_key(BL, summer) == (
        "Bundesliga - Germany|2026-08-22T13:30Z|Bayern Munich|Union Berlin"
    )
    assert ra.prediction_match_key(BL, {"home_team": "A", "away_team": "B"}) is None
    fixture = ra.fixture_from_record(BL, winter)
    assert fixture is not None
    assert fixture.key == ra.prediction_match_key(BL, winter)
    assert fixture.season == SEASON


def test_results_pathname_uses_league_key_and_dashed_season() -> None:
    assert BL_PATH == "results/2026-27_Bundesliga-Germany.csv"


def test_csv_round_trip_keeps_columns_and_sorts_by_kickoff() -> None:
    late = ra.Fixture(BL, datetime(2026, 11, 8, 14, 30, tzinfo=UTC), "C", "D")
    early = ra.Fixture(BL, datetime(2026, 11, 7, 14, 30, tzinfo=UTC), "A", "B")
    result = bl_result(early.kickoff, "A", "B", (1, 0))
    rows = [ra.result_row(f, result, 2.0, NOW) for f in (late, early)]
    body = ra.rows_to_csv(rows)
    assert body.decode().splitlines()[0] == ",".join(ra.RESULT_COLUMNS)
    parsed = ra.rows_from_csv(body)
    assert [r["match_key"] for r in parsed] == [early.key, late.key]
    assert parsed[0]["match_score"] == "2.000"
    with pytest.raises(ValueError, match="header"):
        ra.rows_from_csv(b"a,b\n1,2\n")


def espn_event(name: str, score: tuple[str, str], lines: list[list[float]]) -> dict:
    def side(where: str, team: str, goals: str, periods: list[float]) -> dict:
        return {
            "homeAway": where,
            "score": goals,
            "team": {"displayName": team},
            "linescores": [{"value": v} for v in periods],
        }

    return {
        "id": "401",
        "date": "2026-10-21T19:00Z",
        "status": {"type": {"completed": True, "name": name}},
        "competitions": [
            {
                "competitors": [
                    side("home", "Arsenal", score[0], lines[0]),
                    side("away", "Chelsea", score[1], lines[1]),
                ]
            }
        ],
    }


class FakeCache(ra.HttpCache):
    def __init__(self, events: list[dict]) -> None:
        super().__init__()
        self.events = events

    def get_json(self, url: str, *, cacheable: bool = True) -> object:
        return {"events": self.events}


def test_espn_uses_90_minute_score_after_extra_time_and_penalties() -> None:
    events = [
        espn_event("STATUS_FINAL_AET", ("3", "2"), [[1, 1, 1], [0, 2, 0]]),
        espn_event("STATUS_FINAL_PEN", ("1", "1"), [[0, 1, 0], [1, 0, 0]]),
        espn_event("STATUS_FULL_TIME", ("2", "0"), [[1, 1], [0, 0]]),
        espn_event("STATUS_FINAL_AET", ("1", "0"), [[], []]),  # no periods: skip
        espn_event("STATUS_POSTPONED", ("0", "0"), [[], []]),
    ]
    results = ra.espn_results(
        FakeCache(events), "UEFA Champions League", {date(2026, 10, 21)}, NOW
    )
    assert [(r.home_goals, r.away_goals, r.status) for r in results] == [
        (2, 2, "STATUS_FINAL_AET"),
        (1, 1, "STATUS_FINAL_PEN"),
        (2, 0, "STATUS_FULL_TIME"),
    ]
    assert results[0].source == "espn"
    assert results[0].source_id == "401"


def test_kicktipp_points() -> None:
    assert ra.kicktipp_points((2, 1), (2, 1)) == 4
    assert ra.kicktipp_points((2, 1), (3, 2)) == 3
    assert ra.kicktipp_points((1, 1), (2, 2)) == 2
    assert ra.kicktipp_points((2, 1), (0, 1)) == 0


def test_eval_match_results_wrapper_keeps_tuple_keys() -> None:
    kickoff = datetime(2026, 11, 7, 14, 30, tzinfo=UTC)
    pred = ev.Prediction(
        "P", kickoff, BL, kickoff, "Bayern Munich", "Union Berlin", 2, 0
    )
    result = bl_result(kickoff, "FC Bayern München", "1. FC Union Berlin", (3, 0))
    matched = ev.match_results([pred], [result])
    assert matched == {(BL, "Bayern Munich", "Union Berlin", kickoff): result}


# %% archive update


@pytest.mark.usefixtures("sources")
def test_first_run_writes_matched_rows_and_index(
    fake_blob: FakeBlob, sources: dict[str, Any]
) -> None:
    seed(fake_blob, {BL: [record("07.11.2026 15:30", "Bayern Munich", "Union Berlin")]})
    kickoff = datetime(2026, 11, 7, 14, 30, tzinfo=UTC)
    sources["results"] = [
        bl_result(kickoff, "FC Bayern München", "1. FC Union Berlin", (3, 0))
    ]

    report = run()

    assert report.ok
    rows = csv_rows(fake_blob)
    assert len(rows) == 1
    assert rows[0]["match_key"] == f"{BL}|2026-11-07T14:30Z|Bayern Munich|Union Berlin"
    assert (rows[0]["home_goals"], rows[0]["away_goals"]) == ("3", "0")
    assert rows[0]["source_home_team"] == "FC Bayern München"
    index = json.loads(fake_blob.files[blob_archive.INDEX_PATHNAME])
    assert index["results"][BL_PATH]["rows"] == 1
    assert index["runs"][0]["run_ts"] == RUN_TS  # runs untouched
    csv_put = next(p for p in fake_blob.puts if p["pathname"] == BL_PATH)
    assert csv_put["allow_overwrite"]
    assert csv_put["cache_control_max_age"] == blob_archive.INDEX_CACHE_MAX_AGE


def test_gap_detection_targets_every_due_fixture_without_a_row(
    fake_blob: FakeBlob, sources: dict[str, Any]
) -> None:
    seed(
        fake_blob,
        {
            BL: [
                record("01.11.2026 15:30", "Old", "Done"),  # has a row
                record("02.11.2026 15:30", "Lost", "Row"),  # gap from an old run
                record("10.11.2026 02:30", "Too", "Recent"),  # < 3h before NOW
                record("14.11.2026 15:30", "Not", "Played"),  # future
            ]
        },
    )
    done = ra.Fixture(BL, datetime(2026, 11, 1, 14, 30, tzinfo=UTC), "Old", "Done")
    existing = [
        ra.result_row(done, bl_result(done.kickoff, "Old", "Done", (1, 1)), 2, NOW)
    ]
    fake_blob.files[BL_PATH] = ra.rows_to_csv(existing)
    lost_kickoff = datetime(2026, 11, 2, 14, 30, tzinfo=UTC)
    sources["results"] = [bl_result(lost_kickoff, "Lost", "Row", (0, 2))]

    report = run()

    assert [(f.home, f.away) for f in sources["requested"]] == [("Lost", "Row")]
    assert [r["home_team"] for r in csv_rows(fake_blob)] == ["Old", "Lost"]
    assert report.files[0].existing == 1
    assert report.files[0].added == 1


def test_nothing_new_means_no_write(
    fake_blob: FakeBlob, sources: dict[str, Any]
) -> None:
    seed(fake_blob, {BL: [record("14.11.2026 15:30", "Not", "Played")]})
    report = run()
    assert sources["requested"] == []
    assert [p["pathname"] for p in fake_blob.puts] == []
    assert report.files[0].status == "unchanged"
    assert report.index_updated is None


@pytest.mark.usefixtures("sources")
def test_unmatched_fixtures_are_logged_not_written(
    fake_blob: FakeBlob, sources: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    seed(
        fake_blob,
        {
            BL: [
                record("07.11.2026 15:30", "Bayern Munich", "Union Berlin"),
                record("07.11.2026 18:30", "Werder Bremen", "Hamburger SV"),
            ]
        },
    )
    kickoff = datetime(2026, 11, 7, 14, 30, tzinfo=UTC)
    sources["results"] = [
        bl_result(kickoff, "FC Bayern München", "1. FC Union Berlin", (3, 0)),
        # Wrong teams at the right time: below the confidence threshold
        bl_result(kickoff, "VfL Wolfsburg", "SC Freiburg", (1, 0)),
    ]
    with caplog.at_level(logging.INFO):
        report = run()
    assert [r["home_team"] for r in csv_rows(fake_blob)] == ["Bayern Munich"]
    assert [(f.home, f.away) for f in report.unmatched] == [
        ("Werder Bremen", "Hamburger SV")
    ]
    assert "No result matched: Bundesliga - Germany" in caplog.text


def test_never_shrink_stale_read_writes_nothing(
    fake_blob: FakeBlob, sources: dict[str, Any]
) -> None:
    seed(fake_blob, {BL: [record("07.11.2026 15:30", "Bayern Munich", "Union Berlin")]})
    index = json.loads(fake_blob.files[blob_archive.INDEX_PATHNAME])
    index["results"] = {BL_PATH: {"rows": 5, "updated_at": "2026-11-06 03:00:00"}}
    fake_blob.files[blob_archive.INDEX_PATHNAME] = blob_archive.to_json(index)
    fake_blob.files[BL_PATH] = ra.rows_to_csv([])  # stale: 0 rows instead of 5
    sources["results"] = [
        bl_result(
            datetime(2026, 11, 7, 14, 30, tzinfo=UTC), "Bayern", "Union Berlin", (1, 0)
        )
    ]

    report = run(attempts=3)

    assert report.files[0].status == "stale"
    assert not report.ok
    assert fake_blob.puts == []
    assert fake_blob.heads.count(BL_PATH) == 3  # retried fresh reads
    assert sources["requested"] == []


def test_stale_read_that_recovers_is_used(
    fake_blob: FakeBlob, sources: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(fake_blob, {BL: [record("07.11.2026 15:30", "Bayern Munich", "Union Berlin")]})
    done = ra.Fixture(BL, datetime(2026, 11, 1, 14, 30, tzinfo=UTC), "Old", "Done")
    good = ra.rows_to_csv(
        [ra.result_row(done, bl_result(done.kickoff, "Old", "Done", (1, 1)), 2, NOW)]
    )
    fake_blob.files[BL_PATH] = good
    index = json.loads(fake_blob.files[blob_archive.INDEX_PATHNAME])
    index["results"] = {BL_PATH: {"rows": 1, "updated_at": "2026-11-06 03:00:00"}}
    fake_blob.files[blob_archive.INDEX_PATHNAME] = blob_archive.to_json(index)
    real = blob_archive.read_fresh
    reads = iter([ra.rows_to_csv([])])  # first fresh read lags behind

    def lagging(pathname: str) -> bytes | None:
        if pathname == BL_PATH:
            return next(reads, None) or real(pathname)
        return real(pathname)

    monkeypatch.setattr(blob_archive, "read_fresh", lagging)
    sources["results"] = [
        bl_result(datetime(2026, 11, 7, 14, 30, tzinfo=UTC), "Bayern", "Union", (1, 0))
    ]
    report = run()
    assert report.ok
    assert [r["home_team"] for r in csv_rows(fake_blob)] == ["Old", "Bayern Munich"]


def test_read_back_verification_retries_then_fails(
    fake_blob: FakeBlob, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = ra.Fixture(BL, datetime(2026, 11, 7, 14, 30, tzinfo=UTC), "A", "B")
    row = ra.result_row(fixture, bl_result(fixture.kickoff, "A", "B", (1, 0)), 2, NOW)
    reads = iter([ra.rows_to_csv([])])
    real = blob_archive.read_fresh
    monkeypatch.setattr(
        blob_archive, "read_fresh", lambda p: next(reads, None) or real(p)
    )
    assert ra.write_rows_verified(BL_PATH, [row], wait=0)
    assert [p["pathname"] for p in fake_blob.puts] == [BL_PATH, BL_PATH]

    monkeypatch.setattr(blob_archive, "read_fresh", lambda _: ra.rows_to_csv([]))
    assert not ra.write_rows_verified(BL_PATH, [row], attempts=2, wait=0)


@pytest.mark.usefixtures("sources")
def test_unverified_write_is_not_indexed(
    fake_blob: FakeBlob, sources: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(fake_blob, {BL: [record("07.11.2026 15:30", "Bayern Munich", "Union Berlin")]})
    sources["results"] = [
        bl_result(datetime(2026, 11, 7, 14, 30, tzinfo=UTC), "Bayern", "Union", (1, 0))
    ]
    monkeypatch.setattr(ra, "write_rows_verified", lambda *_, **__: False)
    report = run()
    assert report.files[0].status == "write-failed"
    assert report.index_entries == {}
    assert not report.ok


def test_dry_run_writes_nothing(fake_blob: FakeBlob, sources: dict[str, Any]) -> None:
    seed(fake_blob, {BL: [record("07.11.2026 15:30", "Bayern Munich", "Union Berlin")]})
    sources["results"] = [
        bl_result(datetime(2026, 11, 7, 14, 30, tzinfo=UTC), "Bayern", "Union", (1, 0))
    ]
    report = run(dry_run=True)
    assert fake_blob.puts == []
    assert report.files[0].status == "dry-run"
    assert report.files[0].rows == 1


def test_rebuild_refuses_to_shrink_without_force(
    fake_blob: FakeBlob, sources: dict[str, Any]
) -> None:
    seed(fake_blob, {BL: [record("07.11.2026 15:30", "Bayern Munich", "Union Berlin")]})
    index = json.loads(fake_blob.files[blob_archive.INDEX_PATHNAME])
    index["results"] = {BL_PATH: {"rows": 2, "updated_at": "2026-11-06 03:00:00"}}
    fake_blob.files[blob_archive.INDEX_PATHNAME] = blob_archive.to_json(index)
    sources["results"] = [
        bl_result(datetime(2026, 11, 7, 14, 30, tzinfo=UTC), "Bayern", "Union", (1, 0))
    ]
    assert run(rebuild=True).files[0].status == "shrink-refused"
    assert fake_blob.puts == []
    assert run(rebuild=True, force=True).files[0].status == "written"


def test_index_self_heals_a_dropped_results_entry(
    fake_blob: FakeBlob, sources: dict[str, Any]
) -> None:
    seed(fake_blob, {BL: [record("01.11.2026 15:30", "Old", "Done")]})
    done = ra.Fixture(BL, datetime(2026, 11, 1, 14, 30, tzinfo=UTC), "Old", "Done")
    fake_blob.files[BL_PATH] = ra.rows_to_csv(
        [ra.result_row(done, bl_result(done.kickoff, "Old", "Done", (1, 1)), 2, NOW)]
    )
    report = run()
    assert sources["requested"] == []
    assert report.index_updated
    assert [p["pathname"] for p in fake_blob.puts] == [blob_archive.INDEX_PATHNAME]
    index = json.loads(fake_blob.files[blob_archive.INDEX_PATHNAME])
    assert index["results"][BL_PATH]["rows"] == 1


# %% index helpers


def test_update_results_index_refuses_stale_read(fake_blob: FakeBlob) -> None:
    seen = blob_archive.merge_results_index(
        None, {"results/x.csv": {"rows": 9, "updated_at": "2026-11-06 03:00:00"}}
    )
    lagging = blob_archive.merge_results_index(
        None, {"results/x.csv": {"rows": 3, "updated_at": "2026-11-03 03:00:00"}}
    )
    fake_blob.files[blob_archive.INDEX_PATHNAME] = blob_archive.to_json(lagging)
    entry = {BL_PATH: {"rows": 1, "updated_at": "2026-11-10 03:00:00"}}
    assert not blob_archive.update_results_index(
        entry, expected=seen, attempts=2, wait=0
    )
    assert fake_blob.puts == []


def test_update_index_carries_results_over_a_lagging_read(fake_blob: FakeBlob) -> None:
    fake_blob.files[blob_archive.INDEX_PATHNAME] = blob_archive.to_json(
        blob_archive.empty_index()
    )
    results = {BL_PATH: {"rows": 4, "updated_at": "2026-11-10 03:00:00"}}
    assert blob_archive.update_index([{"run_ts": RUN_TS}], results=results)
    index = json.loads(fake_blob.files[blob_archive.INDEX_PATHNAME])
    assert index["results"] == results
    assert BL_PATH in blob_archive.index_pathnames(index)
    # An older entry never replaces a newer row count
    older = {BL_PATH: {"rows": 2, "updated_at": "2026-11-06 03:00:00"}}
    assert blob_archive.merge_results_index(index, older)["results"] == results


# %% cron step and backfill script


@pytest.fixture
def blob_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "blob-token")


@pytest.mark.usefixtures("blob_token", "fake_blob")
def test_cron_step_fails_softly(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(*_: Any, **__: Any) -> None:
        msg = "index unreadable"
        raise RuntimeError(msg)

    monkeypatch.setattr(sm_module.results_archive, "update_results_archive", boom)
    manager = StorageManager("data/match_predictions", write_to_blob=True)
    with caplog.at_level(logging.WARNING):
        assert manager.update_results_archive(NOW) is False
    assert "Could not update the Blob results archive" in caplog.text
    assert manager.blob_results == {}


def test_cron_step_skipped_without_blob(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[Any] = []
    monkeypatch.setattr(
        sm_module.results_archive, "update_results_archive", called.append
    )
    manager = StorageManager("data/match_predictions", debug=True, write_to_blob=True)
    assert manager.update_results_archive(NOW) is False
    assert called == []


@pytest.mark.usefixtures("blob_token")
def test_cron_step_targets_current_season_and_keeps_entries(
    fake_blob: FakeBlob, sources: dict[str, Any]
) -> None:
    seed(fake_blob, {BL: [record("07.11.2026 15:30", "Bayern Munich", "Union Berlin")]})
    sources["results"] = [
        bl_result(datetime(2026, 11, 7, 14, 30, tzinfo=UTC), "Bayern", "Union", (1, 0))
    ]
    manager = StorageManager("data/match_predictions", write_to_blob=True)
    assert manager.update_results_archive(NOW)
    assert manager.blob_results[BL_PATH]["rows"] == 1


def test_backfill_near_cron_and_default_seasons() -> None:
    assert backfill_results.near_cron(datetime(2026, 11, 10, 3, 0, tzinfo=UTC))
    assert backfill_results.near_cron(datetime(2026, 11, 13, 16, 59, tzinfo=UTC))
    assert not backfill_results.near_cron(datetime(2026, 11, 13, 17, 0, tzinfo=UTC))
    assert not backfill_results.near_cron(datetime(2026, 11, 12, 3, 0, tzinfo=UTC))
    assert backfill_results.default_seasons(NOW) == ["2024/25", "2025/26", "2026/27"]


def test_prediction_pathnames_filter_runs_by_season() -> None:
    index = blob_archive.merge_index(
        None,
        [
            {"run_ts": "2025-03-01_03-00-00", "predictions": {"a": "old.jsonl"}},
            {"run_ts": "2026-06-20_03-00-00", "predictions": {"a": "lookback.jsonl"}},
            {"run_ts": RUN_TS, "predictions": {"a": "new.jsonl"}},
        ],
    )
    index = blob_archive.merge_backfill_index(index, {RUN_TS: {"b": "bf.jsonl"}})
    assert ra.prediction_pathnames(index, {SEASON}) == [
        "bf.jsonl",
        "lookback.jsonl",
        "new.jsonl",
    ]
    assert PL not in BL_PATH  # sanity: one file per league


class SummaryCache(ra.HttpCache):
    """Scoreboard without line scores; the summary has them (displayValue)."""

    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def get_json(self, url: str, *, cacheable: bool = True) -> object:
        self.urls.append(url)
        if "summary?event=401" in url:
            periods = {"home": ["1", "0", "1", "0"], "away": ["0", "1", "0", "0"]}
            return {
                "header": {
                    "competitions": [
                        {
                            "competitors": [
                                {
                                    "homeAway": side,
                                    "linescores": [{"displayValue": v} for v in vals],
                                }
                                for side, vals in periods.items()
                            ]
                        }
                    ]
                }
            }
        return {"events": [espn_event("STATUS_FINAL_PEN", ("2", "1"), [[], []])]}


def test_espn_extra_time_without_line_scores_uses_the_summary() -> None:
    cache = SummaryCache()
    results = ra.espn_results(cache, "UEFA Champions League", {date(2026, 10, 21)}, NOW)
    assert [(r.home_goals, r.away_goals) for r in results] == [(1, 1)]
    assert "uefa.champions/summary?event=401" in cache.urls[-1]


def test_espn_regular_time_never_reads_missing_values_as_zero() -> None:
    assert ra.espn_regular_time({"linescores": [{"displayValue": "2"}, {}]}) is None
    assert ra.espn_regular_time({"linescores": [{"value": 1.0}, {"value": 2.0}]}) == 3


def test_rescheduled_fixture_reuses_the_row_of_its_pairing(
    fake_blob: FakeBlob, sources: dict[str, Any]
) -> None:
    seed(
        fake_blob,
        {
            BL: [
                # First prediction with the provisional kickoff, then the real one
                record("01.11.2026 15:30", "Bayern Munich", "Union Berlin"),
                record("05.11.2026 20:30", "Bayern Munich", "Union Berlin"),
            ]
        },
    )
    actual = datetime(2026, 11, 5, 19, 30, tzinfo=UTC)
    sources["results"] = [
        bl_result(actual, "FC Bayern München", "1. FC Union Berlin", (2, 2))
    ]

    report = run()

    rows = csv_rows(fake_blob)
    assert [r["kickoff_utc"] for r in rows] == [
        "2026-11-01T14:30Z",
        "2026-11-05T19:30Z",
    ]
    assert {(r["home_goals"], r["away_goals"]) for r in rows} == {("2", "2")}
    assert report.unmatched == []


def test_uefa_repeat_pairings_are_not_aliased() -> None:
    cl = "UEFA Champions League"
    fixture = ra.Fixture(cl, datetime(2026, 9, 16, 19, 0, tzinfo=UTC), "A", "B")
    other = ra.Fixture(cl, datetime(2027, 3, 10, 20, 0, tzinfo=UTC), "A", "B")
    row = ra.result_row(other, ra.Result(cl, other.kickoff, "A", "B", 1, 0), 2, NOW)
    assert ra.reschedule_rows([fixture], [row]) == {}


def test_summary_key_events_beat_inconsistent_line_scores() -> None:
    # Shape of the 2026 final on ESPN: 1:1 after 120', bogus line scores
    def team(side: str, team_id: str, lines: list[str]) -> dict:
        return {
            "homeAway": side,
            "score": "1",
            "team": {"id": team_id},
            "linescores": [{"displayValue": v} for v in lines],
        }

    data = {
        "header": {
            "competitions": [
                {
                    "competitors": [
                        team("home", "160", ["0", "2", "-1", "0", "4"]),
                        team("away", "359", ["1", "1", "-1", "0", "3"]),
                    ]
                }
            ]
        },
        "keyEvents": [
            {"scoringPlay": True, "period": {"number": 1}, "team": {"id": "359"}},
            {"scoringPlay": True, "period": {"number": 2}, "team": {"id": "160"}},
            {"scoringPlay": False, "period": {"number": 2}, "team": {"id": "160"}},
        ],
    }
    assert ra.espn_key_event_regular_time(data) == (1, 1)
    home = data["header"]["competitions"][0]["competitors"][0]
    assert ra.espn_regular_time(home) is None  # negative period, sum != score
    # Key events that do not add up to the final score are not trusted
    data["keyEvents"] = data["keyEvents"][:1]
    assert ra.espn_key_event_regular_time(data) is None


def test_espn_regular_time_checks_the_final_score() -> None:
    lines = [{"value": 1.0}, {"value": 2.0}, {"value": 1.0}]
    assert ra.espn_regular_time({"score": "4", "linescores": lines}) == 3
    assert ra.espn_regular_time({"score": "5", "linescores": lines}) is None
