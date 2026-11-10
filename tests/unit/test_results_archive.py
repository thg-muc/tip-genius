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
from utils import kv_snapshots as ev

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


def test_read_back_verification_polls_then_retries_then_fails(
    fake_blob: FakeBlob, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = ra.Fixture(BL, datetime(2026, 11, 7, 14, 30, tzinfo=UTC), "A", "B")
    row = ra.result_row(fixture, bl_result(fixture.kickoff, "A", "B", (1, 0)), 2, NOW)
    sleeps: list[float] = []
    monkeypatch.setattr(blob_archive.time, "sleep", sleeps.append)
    polls = len(blob_archive.READBACK_DELAYS)
    real = blob_archive.read_fresh

    # Lagging reads within one attempt's polls: a single put, waits before
    # the first read-back
    reads = iter([ra.rows_to_csv([])] * (polls - 1))
    monkeypatch.setattr(
        blob_archive, "read_fresh", lambda p: next(reads, None) or real(p)
    )
    assert ra.write_rows_verified(BL_PATH, [row], wait=0)
    assert [p["pathname"] for p in fake_blob.puts] == [BL_PATH]
    assert sleeps == list(blob_archive.READBACK_DELAYS)

    # Lagging through every poll of the first attempt: put again
    fake_blob.puts.clear()
    reads = iter([ra.rows_to_csv([])] * polls)
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


def test_rebuild_with_unreadable_predictions_is_not_ok(
    fake_blob: FakeBlob, sources: dict[str, Any]
) -> None:
    seed(fake_blob, {BL: [record("07.11.2026 15:30", "Bayern Munich", "Union Berlin")]})
    del fake_blob.files[PRED_PATH]
    report = run(rebuild=True)
    assert report.aborted
    assert not report.ok
    assert fake_blob.puts == []
    assert sources["requested"] == []


def test_malformed_espn_event_is_skipped_not_fatal() -> None:
    broken = espn_event("STATUS_FULL_TIME", ("2", "0"), [[1, 1], [0, 0]])
    del broken["competitions"][0]["competitors"][0]["score"]
    good = espn_event("STATUS_FULL_TIME", ("1", "0"), [[1, 0], [0, 0]])
    results = ra.espn_results(
        FakeCache([broken, good]), "UEFA Champions League", {date(2026, 10, 21)}, NOW
    )
    assert [(r.home_goals, r.away_goals) for r in results] == [(1, 0)]


@pytest.mark.usefixtures("blob_token", "fake_blob")
def test_cron_step_uses_short_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_update(seasons: set[str], **kwargs: Any) -> ra.ArchiveReport:
        seen.update(kwargs, seasons=seasons)
        return ra.ArchiveReport()

    monkeypatch.setattr(
        sm_module.results_archive, "update_results_archive", fake_update
    )
    manager = StorageManager("data/match_predictions", write_to_blob=True)
    assert manager.update_results_archive(NOW)
    assert seen["seasons"] == {SEASON}
    assert seen["attempts"] == ra.CRON_ATTEMPTS
    assert seen["wait"] == ra.CRON_WAIT_SECONDS


def test_unreadable_prediction_file_makes_the_run_not_ok(
    fake_blob: FakeBlob, sources: dict[str, Any]
) -> None:
    seed(fake_blob, {BL: [record("07.11.2026 15:30", "Bayern Munich", "Union Berlin")]})
    del fake_blob.files[PRED_PATH]
    report = run()
    assert report.unread == [PRED_PATH]
    assert not report.ok


# %% incremental scan (watermark, open fixtures, full-scan fallback)

DEEPSEEK = "Match_Predictions_Deepseek-Chat_FourPointsScoring_named_with-info"
RUN_TS_2 = "2026-11-06_03-00-05"
PRED_PATH_2 = blob_archive.predictions_pathname(RUN_TS_2, DEEPSEEK)


def add_run(
    fake: FakeBlob, run_ts: str, matches: dict[str, list[dict[str, Any]]]
) -> str:
    """Store one more live prediction file and add its run to the index."""
    path = blob_archive.predictions_pathname(run_ts, DEEPSEEK)
    payload = blob_archive.build_prediction_payload(matches, run_ts)
    fake.files[path] = blob_archive.to_jsonl(payload)
    index = json.loads(fake.files[blob_archive.INDEX_PATHNAME])
    index = blob_archive.merge_index(
        index, [{"run_ts": run_ts, "predictions": {"Deepseek": path}}]
    )
    fake.files[blob_archive.INDEX_PATHNAME] = blob_archive.to_json(index)
    return path


def read_index(fake: FakeBlob) -> dict[str, Any]:
    return json.loads(fake.files[blob_archive.INDEX_PATHNAME])


def persist_scan(fake: FakeBlob, report: ra.ArchiveReport) -> None:
    """Store the report's scan state, as ``finalize_blob_archive`` does."""
    assert report.ok
    assert report.scan_state
    index = blob_archive.merge_results_scan(read_index(fake), report.scan_state)
    fake.files[blob_archive.INDEX_PATHNAME] = blob_archive.to_json(index)


def prediction_reads(fake: FakeBlob) -> list[str]:
    reads = [g for g in fake.gets if g.startswith("predictions/")]
    fake.gets.clear()
    return reads


def season_state(report: ra.ArchiveReport) -> dict[str, Any]:
    """Return the report's scan state for SEASON (must be set)."""
    assert report.scan_state is not None
    state = report.scan_state[SEASON]
    assert state is not None
    return state


def run_incremental(when: datetime = NOW) -> ra.ArchiveReport:
    return ra.update_results_archive({SEASON}, now=when, wait=0, incremental=True)


def test_first_incremental_run_scans_fully_and_records_open_fixtures(
    fake_blob: FakeBlob, sources: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    seed(
        fake_blob,
        {
            BL: [
                record("07.11.2026 15:30", "Bayern Munich", "Union Berlin"),
                record("14.11.2026 15:30", "Not", "Played"),
            ]
        },
    )
    kickoff = datetime(2026, 11, 7, 14, 30, tzinfo=UTC)
    sources["results"] = [bl_result(kickoff, "Bayern", "Union", (3, 0))]

    with caplog.at_level(logging.INFO):
        report = run_incremental()

    assert report.ok
    assert report.scan_modes == {SEASON: "full"}
    assert report.files_read == 1
    assert "read 1 prediction files" in caplog.text
    assert "no scan state" in caplog.text
    state = season_state(report)
    assert state["through"] == RUN_TS
    assert state["files"] == 1
    assert state["full_scan_at"] == blob_archive.format_payload_ts(NOW)
    assert state["open"] == [[BL, "2026-11-14T14:30Z", "Not", "Played"]]
    # Written with the results entries in the same verified index update
    assert read_index(fake_blob)[blob_archive.RESULTS_SCAN_KEY][SEASON] == state


def test_incremental_run_reads_only_new_files_and_retries_open_fixtures(
    fake_blob: FakeBlob, sources: dict[str, Any]
) -> None:
    seed(
        fake_blob,
        {
            BL: [
                record("07.11.2026 15:30", "Bayern Munich", "Union Berlin"),
                record("08.11.2026 15:30", "Open", "Later"),  # no result yet
                record("14.11.2026 15:30", "Not", "Played"),
            ]
        },
    )
    sat = datetime(2026, 11, 7, 14, 30, tzinfo=UTC)
    sources["results"] = [bl_result(sat, "Bayern", "Union", (3, 0))]
    persist_scan(fake_blob, run_incremental())
    prediction_reads(fake_blob)

    add_run(fake_blob, RUN_TS_2, {BL: [record("21.11.2026 15:30", "Next", "Week")]})
    sun = datetime(2026, 11, 8, 14, 30, tzinfo=UTC)
    sources["results"] = [bl_result(sun, "Open", "Later", (1, 1))]
    sources["requested"].clear()
    later = datetime(2026, 11, 15, 3, 0, tzinfo=UTC)

    report = run_incremental(later)

    assert report.ok
    assert report.scan_modes == {SEASON: "incremental"}
    assert prediction_reads(fake_blob) == [PRED_PATH_2]
    assert report.files_read == 1
    requested = {(f.home, f.away) for f in sources["requested"]}
    assert requested == {("Open", "Later"), ("Not", "Played")}
    assert [r["home_team"] for r in csv_rows(fake_blob)] == ["Bayern Munich", "Open"]
    state = season_state(report)
    assert state["through"] == RUN_TS_2
    assert state["files"] == 2
    # Not Played had no result in the sources: still open, as is next week's game
    assert [item[2] for item in state["open"]] == ["Not", "Next"]
    # The full scan time is kept, so the monthly full scan stays on schedule
    assert state["full_scan_at"] == blob_archive.format_payload_ts(NOW)


def test_unmatched_fixture_stays_open_without_rescanning(
    fake_blob: FakeBlob, sources: dict[str, Any]
) -> None:
    seed(fake_blob, {BL: [record("07.11.2026 15:30", "Abandoned", "Game")]})
    persist_scan(fake_blob, run_incremental())
    prediction_reads(fake_blob)
    sources["requested"].clear()

    report = run_incremental(datetime(2026, 11, 13, 3, 0, tzinfo=UTC))

    assert report.scan_modes == {SEASON: "incremental"}
    assert prediction_reads(fake_blob) == []
    assert [(f.home, f.away) for f in sources["requested"]] == [("Abandoned", "Game")]
    assert [u.home for u in report.unmatched] == ["Abandoned"]
    assert season_state(report)["open"][0][2] == "Abandoned"


def test_backfill_file_for_an_old_run_forces_a_full_scan(
    fake_blob: FakeBlob, sources: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    seed(fake_blob, {BL: [record("14.11.2026 15:30", "Not", "Played")]})
    persist_scan(fake_blob, run_incremental())
    add_run(fake_blob, RUN_TS_2, {BL: [record("21.11.2026 15:30", "Next", "Week")]})
    # A backfill file for a run before the watermark, indexed afterwards
    backfill = blob_archive.backfill_predictions_pathname("2026-10-30_03-00-05", "X")
    fake_blob.files[backfill] = blob_archive.to_jsonl(
        blob_archive.build_prediction_payload(
            {BL: [record("01.11.2026 15:30", "Late", "Provider")]},
            "2026-10-30 03:00:05",
        )
    )
    index = blob_archive.merge_backfill_index(
        read_index(fake_blob), {"2026-10-30_03-00-05": {"X": backfill}}
    )
    fake_blob.files[blob_archive.INDEX_PATHNAME] = blob_archive.to_json(index)
    prediction_reads(fake_blob)
    sources["requested"].clear()

    with caplog.at_level(logging.INFO):
        report = run_incremental()

    assert report.scan_modes == {SEASON: "full"}
    assert "changed since the last scan" in caplog.text
    assert sorted(prediction_reads(fake_blob)) == sorted(
        [PRED_PATH, PRED_PATH_2, backfill]
    )
    assert ("Late", "Provider") in {(f.home, f.away) for f in sources["requested"]}
    assert season_state(report)["files"] == 3


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ({"through": "garbage"}, "invalid scan state"),
        ("not a dict", "invalid scan state"),
        (
            {
                "through": RUN_TS,
                "files": 1,
                "digest": "x",
                "open": [["only", "two"]],
                "full_scan_at": "2026-11-09 03:00:00",
            },
            "invalid scan state",
        ),
        (
            {
                "through": RUN_TS,
                "files": 1,
                "digest": ra.paths_digest([PRED_PATH]),
                "open": [],
                "full_scan_at": "2026-10-01 03:00:00",
            },
            "last full scan",
        ),
        (
            {
                "through": RUN_TS,
                "files": 1,
                "digest": "wrong",
                "open": [],
                "full_scan_at": "2026-11-09 03:00:00",
            },
            "changed since the last scan",
        ),
    ],
)
def test_invalid_or_old_scan_state_falls_back_to_a_full_scan(
    state: object, reason: str
) -> None:
    index = blob_archive.merge_index(
        None, [{"run_ts": RUN_TS, "predictions": {"Deepseek": PRED_PATH}}]
    )
    index[blob_archive.RESULTS_SCAN_KEY] = {SEASON: state}
    plan = ra.plan_scan(index, SEASON, NOW)
    assert plan.mode == "full"
    assert reason in plan.reason
    assert plan.read == [PRED_PATH]


def test_valid_scan_state_with_nothing_new_reads_no_files() -> None:
    index = blob_archive.merge_index(
        None, [{"run_ts": RUN_TS, "predictions": {"Deepseek": PRED_PATH}}]
    )
    index[blob_archive.RESULTS_SCAN_KEY] = {
        SEASON: {
            "through": RUN_TS,
            "files": 1,
            "digest": ra.paths_digest([PRED_PATH]),
            "open": [[BL, "2026-11-14T14:30Z", "Not", "Played"]],
            "full_scan_at": "2026-11-09 03:00:00",
        }
    }
    plan = ra.plan_scan(index, SEASON, NOW)
    assert (plan.mode, plan.read) == ("incremental", [])
    assert [f.home for f in plan.open] == ["Not"]


def test_unreadable_new_file_does_not_advance_the_scan_state(
    fake_blob: FakeBlob, sources: dict[str, Any]
) -> None:
    seed(fake_blob, {BL: [record("14.11.2026 15:30", "Not", "Played")]})
    persist_scan(fake_blob, run_incremental())
    path = add_run(fake_blob, RUN_TS_2, {BL: [record("21.11.2026 15:30", "N", "W")]})
    body = fake_blob.files.pop(path)

    report = run_incremental()

    assert not report.ok
    assert report.scan_state is None
    # The next run (file readable again) still reads it
    fake_blob.files[path] = body
    prediction_reads(fake_blob)
    report = run_incremental()
    assert prediction_reads(fake_blob) == [path]
    assert season_state(report)["through"] == RUN_TS_2


def test_failed_results_write_keeps_its_fixtures_open(
    fake_blob: FakeBlob, sources: dict[str, Any]
) -> None:
    seed(fake_blob, {BL: [record("07.11.2026 15:30", "Bayern Munich", "Union")]})
    kickoff = datetime(2026, 11, 7, 14, 30, tzinfo=UTC)
    sources["results"] = [bl_result(kickoff, "Bayern", "Union", (3, 0))]
    fake_blob.fail_puts[BL_PATH] = [
        blob_archive.vercel_blob.BlobError("boom", status_code=503)
    ] * 20

    report = run_incremental()

    assert report.files[0].status == "write-failed"
    assert not report.ok
    # The state advances, the fixture stays open: no full scan next run
    assert report.scan_state is not None
    assert (report.scan_state[SEASON] or {}).get("open") == [
        [BL, "2026-11-07T14:30Z", "Bayern Munich", "Union"]
    ]
    assert blob_archive.RESULTS_SCAN_KEY not in read_index(fake_blob)
    index = blob_archive.merge_results_scan(read_index(fake_blob), report.scan_state)
    fake_blob.files[blob_archive.INDEX_PATHNAME] = blob_archive.to_json(index)
    fake_blob.fail_puts.clear()
    prediction_reads(fake_blob)

    retry = run_incremental()

    assert retry.scan_modes[SEASON] == "incremental"
    assert prediction_reads(fake_blob) == []
    assert retry.files[0].status == "written"
    assert retry.ok
    assert retry.scan_state is not None
    assert (retry.scan_state[SEASON] or {}).get("open") == []


def test_unverified_write_that_landed_heals_the_index(
    fake_blob: FakeBlob, sources: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(fake_blob, {BL: [record("07.11.2026 15:30", "Bayern Munich", "Union")]})
    kickoff = datetime(2026, 11, 7, 14, 30, tzinfo=UTC)
    sources["results"] = [bl_result(kickoff, "Bayern", "Union", (3, 0))]
    real = blob_archive.read_fresh
    lagging = True

    def read(pathname: str) -> bytes | None:
        # The put lands, but every read-back of this run still shows the old file
        if lagging and pathname == BL_PATH:
            return None
        return real(pathname)

    monkeypatch.setattr(blob_archive, "read_fresh", read)
    report = run_incremental()
    assert report.files[0].status == "write-failed"
    assert report.scan_state is not None
    index = blob_archive.merge_results_scan(read_index(fake_blob), report.scan_state)
    fake_blob.files[blob_archive.INDEX_PATHNAME] = blob_archive.to_json(index)
    lagging = False
    sources["results"] = []

    retry = run_incremental()

    # The row is there: nothing to fetch, the index entry is re-added
    assert retry.files[0].status == "unchanged"
    assert retry.index_entries[BL_PATH]["rows"] == 1
    assert retry.scan_state is not None
    assert (retry.scan_state[SEASON] or {}).get("open") == []


def test_rebuild_resets_the_scan_state(
    fake_blob: FakeBlob, sources: dict[str, Any]
) -> None:
    seed(fake_blob, {BL: [record("07.11.2026 15:30", "Bayern Munich", "Union")]})
    persist_scan(fake_blob, run_incremental())
    assert SEASON in read_index(fake_blob)[blob_archive.RESULTS_SCAN_KEY]
    kickoff = datetime(2026, 11, 7, 14, 30, tzinfo=UTC)
    sources["results"] = [bl_result(kickoff, "Bayern", "Union", (3, 0))]

    report = run(rebuild=True)

    assert report.ok
    assert read_index(fake_blob)[blob_archive.RESULTS_SCAN_KEY] == {}


def test_merge_results_scan_sets_and_removes_seasons() -> None:
    index = blob_archive.merge_results_scan(None, {"2025/26": {"a": 1}, SEASON: {}})
    index = blob_archive.merge_results_scan(index, {"2025/26": None, SEASON: {"b": 2}})
    assert index[blob_archive.RESULTS_SCAN_KEY] == {SEASON: {"b": 2}}


@pytest.mark.usefixtures("blob_token")
def test_cron_step_runs_incrementally_and_finalize_stores_the_state(
    fake_blob: FakeBlob, sources: dict[str, Any]
) -> None:
    seed(fake_blob, {BL: [record("14.11.2026 15:30", "Not", "Played")]})
    manager = StorageManager("data/match_predictions", write_to_blob=True)
    assert manager.update_results_archive(NOW)
    # Nothing to write, so the results step itself did not touch the index
    assert blob_archive.RESULTS_SCAN_KEY not in read_index(fake_blob)
    assert (manager.blob_results_scan[SEASON] or {}).get("through") == RUN_TS

    manager.blob_predictions = {"Deepseek": PRED_PATH_2}
    manager.run_ts = RUN_TS_2
    manager.finalize_blob_archive()

    stored = read_index(fake_blob)[blob_archive.RESULTS_SCAN_KEY][SEASON]
    assert stored == manager.blob_results_scan[SEASON]
    assert [run["run_ts"] for run in read_index(fake_blob)["runs"]] == [
        RUN_TS,
        RUN_TS_2,
    ]


@pytest.mark.usefixtures("blob_token")
def test_finalize_stores_entries_and_state_after_an_unverified_index_update(
    fake_blob: FakeBlob, sources: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(fake_blob, {BL: [record("07.11.2026 15:30", "Bayern Munich", "Union")]})
    kickoff = datetime(2026, 11, 7, 14, 30, tzinfo=UTC)
    sources["results"] = [bl_result(kickoff, "Bayern", "Union", (3, 0))]
    # The CSV is verified, but the step's own index read-back keeps lagging
    monkeypatch.setattr(
        blob_archive, "update_results_index", lambda *_args, **_kwargs: False
    )
    manager = StorageManager("data/match_predictions", write_to_blob=True)
    assert not manager.update_results_archive(NOW)
    assert manager.blob_results[BL_PATH]["rows"] == 1
    assert manager.blob_results_scan[SEASON] is not None

    manager.blob_predictions = {"Deepseek": PRED_PATH_2}
    manager.run_ts = RUN_TS_2
    manager.finalize_blob_archive()

    index = read_index(fake_blob)
    assert index[blob_archive.RESULTS_KEY][BL_PATH]["rows"] == 1
    assert index[blob_archive.RESULTS_SCAN_KEY][SEASON]["through"] == RUN_TS


def test_stale_results_csv_keeps_its_fixtures_open(
    fake_blob: FakeBlob, sources: dict[str, Any]
) -> None:
    seed(fake_blob, {BL: [record("07.11.2026 15:30", "Bayern Munich", "Union")]})
    index = blob_archive.merge_results_index(
        read_index(fake_blob), {BL_PATH: {"rows": 5, "updated_at": "x"}}
    )
    fake_blob.files[blob_archive.INDEX_PATHNAME] = blob_archive.to_json(index)
    fake_blob.files[BL_PATH] = ra.rows_to_csv([])  # fewer rows than indexed

    report = run_incremental()

    assert report.files[0].status == "stale"
    assert report.scan_state is not None
    assert (report.scan_state[SEASON] or {}).get("open") == [
        [BL, "2026-11-07T14:30Z", "Bayern Munich", "Union"]
    ]


def test_unreadable_prediction_file_does_not_advance_the_scan_state(
    fake_blob: FakeBlob, sources: dict[str, Any]
) -> None:
    seed(fake_blob, {BL: [record("07.11.2026 15:30", "Bayern Munich", "Union")]})

    def broken(_path: str) -> list[dict[str, Any]]:
        msg = "boom"
        raise RuntimeError(msg)

    report = ra.update_results_archive(
        {SEASON}, now=NOW, wait=0, incremental=True, fetch_jsonl=broken
    )

    assert report.unread
    assert report.scan_state is None


def test_malformed_scan_section_falls_back_to_a_full_scan() -> None:
    index = blob_archive.merge_index(
        None, [{"run_ts": RUN_TS, "predictions": {"Deepseek": PRED_PATH}}]
    )
    index[blob_archive.RESULTS_SCAN_KEY] = []
    plan = ra.plan_scan(index, SEASON, NOW)
    assert (plan.mode, plan.read) == ("full", [PRED_PATH])
    merged = blob_archive.merge_results_scan(index, {SEASON: {"a": 1}})
    assert merged[blob_archive.RESULTS_SCAN_KEY] == {SEASON: {"a": 1}}


def test_scan_reset_is_written_even_when_row_counts_are_indexed(
    fake_blob: FakeBlob,
) -> None:
    index = blob_archive.merge_results_index(
        None, {BL_PATH: {"rows": 3, "updated_at": "2026-11-09 03:00:00"}}
    )
    index = blob_archive.merge_results_scan(index, {SEASON: {"through": RUN_TS}})
    fake_blob.files[blob_archive.INDEX_PATHNAME] = blob_archive.to_json(index)

    assert blob_archive.update_results_index(
        {BL_PATH: {"rows": 3, "updated_at": "2026-11-10 03:00:00"}},
        expected=index,
        scan={SEASON: None},
        wait=0,
    )
    assert read_index(fake_blob)[blob_archive.RESULTS_SCAN_KEY] == {}
