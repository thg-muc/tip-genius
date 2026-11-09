"""Unit tests for the dashboard stats workflow step (Blob SDK and KV mocked)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from lib import blob_archive, dashboard, stats
from lib import prediction_table as pt
from lib import results_archive as ra
from lib import storage_manager as sm_module
from lib.storage_manager import StorageManager

from utils import backfill_stats

from .conftest import FakeBlob

BL = "Bundesliga - Germany"
NOW = datetime(2026, 11, 10, 3, 0, tzinfo=UTC)
RUN = "2026-11-06_03-00-05"
BASE = "Match_Predictions_Deepseek-Chat_FourPointsScoring_named_with-info"
PRED_PATH = blob_archive.predictions_pathname(RUN, BASE)
KICKOFF = datetime(2026, 11, 7, 14, 30, tzinfo=UTC)
KEY = ra.match_key(BL, KICKOFF, "Bayern Munich", "Union Berlin")
RESULTS_PATH = ra.results_pathname("2026/27", BL)


class _Response:
    status_code = 200
    text = "OK"


@pytest.fixture
def kv_posts(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    monkeypatch.setenv("KV_REST_API_TOKEN", "kv-token")
    monkeypatch.setenv("KV_REST_API_URL", "https://kv.example")
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "blob-token")
    posts: list[dict[str, Any]] = []

    def post(url: str, **kwargs: Any) -> _Response:
        posts.append({"url": url, **kwargs})
        return _Response()

    monkeypatch.setattr(sm_module.requests, "post", post)
    return posts


def seed(fake: FakeBlob) -> None:
    """One prediction file, one results CSV and an index listing both."""
    record = {
        "commence_time_str": "07.11.2026 15:30",
        "home_team": "Bayern Munich",
        "away_team": "Union Berlin",
        "prediction_home": 2,
        "prediction_away": 0,
    }
    fake.files[PRED_PATH] = blob_archive.to_jsonl(
        blob_archive.build_prediction_payload({BL: [record]}, "2026-11-06 03:00:05")
    )
    row = {
        "match_key": KEY,
        "league": BL,
        "season": "2026/27",
        "kickoff_utc": "2026-11-07T14:30Z",
        "home_team": "Bayern Munich",
        "away_team": "Union Berlin",
        "home_goals": "3",
        "away_goals": "1",
    }
    fake.files[RESULTS_PATH] = ra.rows_to_csv([row])
    index = blob_archive.merge_index(
        None, [{"run_ts": RUN, "predictions": {"Deepseek-Chat": PRED_PATH}}]
    )
    index = blob_archive.merge_results_index(
        index, {RESULTS_PATH: {"rows": 1, "updated_at": "2026-11-07 20:00:00"}}
    )
    fake.files[blob_archive.INDEX_PATHNAME] = blob_archive.to_json(index)


def test_step_writes_compact_json_to_kv_and_finalize_stores_state(
    kv_posts: list[dict[str, Any]], fake_blob: FakeBlob
) -> None:
    seed(fake_blob)
    manager = StorageManager(
        "data/match_predictions", write_to_kv=True, write_to_blob=True
    )
    assert manager.update_dashboard_stats(["Deepseek-Chat"], now=NOW)

    [post] = kv_posts
    assert post["url"] == "https://kv.example/set/dashboard_stats"
    body = post["data"]
    assert isinstance(body, bytes)
    data = json.loads(body)
    assert body == stats.to_json_bytes(data)  # compact separators
    assert data["meta"]["matches"] == 1
    assert data["models"][0]["id"] == "Deepseek-Chat"
    columns = data["meta"]["columns"]["leaderboard"]
    leader = dict(
        zip(columns, data["slices"]["2026/27|all"]["leaderboard"][0], strict=True)
    )
    assert (leader["model"], leader["points"]) == (0, 3)

    table = pt.table_pathname("2026/27")
    assert table in fake_blob.files
    assert manager.blob_stats[table]["rows"] == 1
    state = manager.blob_stats_scan["2026/27"]
    assert state is not None
    assert state["through"] == RUN

    # No odds/predictions this run: no manifest, but the state reaches the index
    manager.finalize_blob_archive()
    index = json.loads(fake_blob.files[blob_archive.INDEX_PATHNAME])
    assert index[blob_archive.STATS_KEY][table]["rows"] == 1
    assert index[blob_archive.STATS_SCAN_KEY]["2026/27"]["through"] == RUN
    assert not any(p.startswith("runs/") for p in fake_blob.files)


def test_step_reuses_the_results_step_reads(
    kv_posts: list[dict[str, Any]], fake_blob: FakeBlob
) -> None:
    seed(fake_blob)
    manager = StorageManager(
        "data/match_predictions", write_to_kv=True, write_to_blob=True
    )
    report = ra.ArchiveReport(index=blob_archive.read_index(fresh=True))
    report.files.append(
        ra.FileReport(
            RESULTS_PATH, BL, "2026/27", existing=1, rows=1, status="unchanged"
        )
    )
    report.contents[RESULTS_PATH] = ra.rows_from_csv(fake_blob.files[RESULTS_PATH])
    manager.results_report = report
    manager.blob_reader.fetch_jsonl(PRED_PATH)  # read by the results step
    fake_blob.gets.clear()
    fake_blob.heads.clear()

    assert manager.update_dashboard_stats(["Deepseek-Chat"], now=NOW)
    # No index or results re-read, the prediction file came from memory;
    # only the (missing) table HEAD and the verified table write remain
    assert blob_archive.INDEX_PATHNAME not in fake_blob.heads
    assert RESULTS_PATH not in fake_blob.heads
    assert not [g for g in fake_blob.gets if "predictions/" in g]


def test_step_fails_softly(
    kv_posts: list[dict[str, Any]],
    fake_blob: FakeBlob,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed(fake_blob)

    def boom(*_args: Any, **_kwargs: Any) -> None:
        msg = "broken"
        raise RuntimeError(msg)

    monkeypatch.setattr(dashboard, "collect_inputs", boom)
    manager = StorageManager(
        "data/match_predictions", write_to_kv=True, write_to_blob=True
    )
    assert manager.update_dashboard_stats(["Deepseek-Chat"], now=NOW) is False
    assert kv_posts == []
    assert manager.blob_stats_scan == {}


def test_step_keeps_old_json_when_the_table_is_stale(
    kv_posts: list[dict[str, Any]], fake_blob: FakeBlob
) -> None:
    seed(fake_blob)
    index = blob_archive.read_index()
    index = blob_archive.merge_results_index(
        index,
        {pt.table_pathname("2026/27"): {"rows": 9, "updated_at": "x"}},
        key=blob_archive.STATS_KEY,
    )
    fake_blob.files[blob_archive.INDEX_PATHNAME] = blob_archive.to_json(index)
    manager = StorageManager(
        "data/match_predictions", write_to_kv=True, write_to_blob=True
    )
    assert manager.update_dashboard_stats(["Deepseek-Chat"], now=NOW) is False
    assert kv_posts == []


def test_step_skipped_in_debug_mode(
    kv_posts: list[dict[str, Any]], fake_blob: FakeBlob
) -> None:
    seed(fake_blob)
    manager = StorageManager(
        "data/match_predictions", debug=True, write_to_kv=True, write_to_blob=True
    )
    assert manager.update_dashboard_stats(["Deepseek-Chat"], now=NOW) is False
    assert fake_blob.gets == []
    assert kv_posts == []


def test_verified_results_drop_unwritten_rows() -> None:
    report = ra.ArchiveReport()
    report.files = [
        ra.FileReport("a", BL, "2026/27", existing=1, status="write-failed"),
        ra.FileReport("b", BL, "2026/27", status="stale"),
        ra.FileReport("c", BL, "2026/27", status="written"),
    ]
    report.contents = {"a": [{"x": "1"}, {"x": "2"}], "c": [{"x": "3"}]}
    assert dashboard.verified_results(report) == {"a": [{"x": "1"}], "c": [{"x": "3"}]}


def test_seasons_since_and_backfill_refuses_cron_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert dashboard.seasons_since("2025/26", NOW) == ["2025/26", "2026/27"]
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "blob-token")
    monkeypatch.setattr("sys.argv", ["backfill_stats.py", "--execute"])
    tuesday = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)

    class _Now(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return tuesday

    monkeypatch.setattr(backfill_stats, "datetime", _Now)
    with pytest.raises(SystemExit, match="cron window"):
        backfill_stats.main()
