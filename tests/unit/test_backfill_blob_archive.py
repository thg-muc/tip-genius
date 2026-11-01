"""Unit tests for utils/backfill_blob_archive.py (KV, gh and Blob mocked)."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from lib import blob_archive

from utils import backfill_blob_archive as bf

from .conftest import FakeBlob

SINCE = datetime(2025, 7, 1, tzinfo=UTC)
PRED_KEY = "Match_Predictions_Mistral-Ministral_FourPointsScoring_named_with-info"


def kv_value(home: str, away: str, kickoff: str) -> list[dict[str, Any]]:
    return [
        {
            "name": "Bundesliga - Germany",
            "timestamp": "2026-10-27 08:50:30",
            "matches": [
                {"commence_time_str": kickoff, "home_team": home, "away_team": away}
            ],
        }
    ]


def test_parse_log_snapshots() -> None:
    snap = {"sport": "S", "fetched_at": "2026-11-03T03:00:09+00:00"}
    log = (
        "job\tRun prediction update\t2026-11-03T03:00:10Z INFO - "
        f"ODDS_SNAPSHOT {json.dumps(snap)}\n"
        "job\tother\tno snapshot here\n"
    )
    assert bf.parse_log_snapshots(log) == [snap]


def test_berlin_to_utc() -> None:
    # CET in winter (UTC+1), CEST in summer (UTC+2)
    assert bf.berlin_to_utc("07.11.2026 15:30") == datetime(
        2026, 11, 7, 14, 30, tzinfo=UTC
    )
    assert bf.berlin_to_utc("15.08.2025 21:00") == datetime(
        2025, 8, 15, 19, 0, tzinfo=UTC
    )


def test_kv_items_and_fixtures() -> None:
    values = {
        f"2026-10-27_08-50-30_{PRED_KEY}": kv_value("A", "B", "31.10.2026 15:30"),
        f"2026-10-30_09-50-00_{PRED_KEY}": kv_value("A", "B", "31.10.2026 15:30"),
        "not_a_prediction_key": [],
    }
    items, fixtures, rescheduled = bf.kv_items(values, SINCE)
    assert [i.pathname for i in items] == [
        f"predictions/2026-10-27_08-50-30_{PRED_KEY}.jsonl",
        f"predictions/2026-10-30_09-50-00_{PRED_KEY}.jsonl",
    ]
    assert json.loads(items[0].body) == values[f"2026-10-27_08-50-30_{PRED_KEY}"][0]
    assert items[0].key == "Mistral-Ministral_FourPointsScoring_named_with-info"
    assert len(fixtures) == 1
    assert fixtures[0].kickoff == datetime(2026, 10, 31, 14, 30, tzinfo=UTC)
    assert rescheduled == 0


def test_rescheduled_kickoff_uses_latest_snapshot() -> None:
    values = {
        f"2026-08-11_03-00-00_{PRED_KEY}": kv_value("A", "B", "16.08.2026 21:00"),
        f"2026-08-14_03-00-00_{PRED_KEY}": kv_value("A", "B", "17.08.2026 19:00"),
        # Return fixture months later is a different match
        f"2026-12-01_03-00-00_{PRED_KEY}": kv_value("A", "B", "05.12.2026 15:30"),
    }
    _, fixtures, rescheduled = bf.kv_items(values, SINCE)
    assert [f.kickoff for f in fixtures] == [
        datetime(2026, 8, 17, 17, 0, tzinfo=UTC),
        datetime(2026, 12, 5, 14, 30, tzinfo=UTC),
    ]
    assert rescheduled == 1


def _item(stamp: str, kind: str, key: str) -> bf.Item:
    return bf.Item(
        pathname=f"{kind}/{stamp}_{key}",
        body=b"{}",
        content_type="t",
        source="kv" if kind == "predictions" else "api_result",
        stamp=bf.parse_ts(stamp),
        kind=kind,
        key=key,
    )


def test_group_runs_by_gap_and_clash() -> None:
    items = [
        _item("2026-10-27_08-50-04", "odds", "L1"),
        _item("2026-10-27_09-20-00", "predictions", "P1"),
        _item("2026-10-27_09-20-03", "predictions", "P2"),
        # same league again within the gap -> new run
        _item("2026-10-27_09-25-00", "odds", "L1"),
        # more than 30 min later -> new run
        _item("2026-10-30_09-49-58", "odds", "L1"),
    ]
    runs = bf.group_runs(items)
    assert list(runs) == [
        "2026-10-27_08-50-04",
        "2026-10-27_09-25-00",
        "2026-10-30_09-49-58",
    ]
    assert [i.key for i in runs["2026-10-27_08-50-04"]] == ["L1", "P1", "P2"]


def test_log_items_skip_debug_and_old_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    runs = [
        {"databaseId": 1, "startedAt": "2026-11-03T03:00:05Z", "status": "completed"},
        {"databaseId": 2, "startedAt": "2026-11-02T10:00:00Z", "status": "completed"},
        {"databaseId": 3, "startedAt": "2026-10-27T08:50:04Z", "status": "completed"},
    ]
    monkeypatch.setattr(bf, "gh", lambda *args: json.dumps(runs))
    snap = {
        "sport": "Bundesliga - Germany",
        "fetched_at": "2026-11-03T03:00:09+00:00",
        "columns": ["commence_time", "home_team", "away_team", "odds_home"],
        "matches": [["2026-11-07T14:30:00Z", "A", "B", 1.5]],
    }
    logs = {
        "1": f"x ODDS_SNAPSHOT {json.dumps(snap)}\n",
        "2": f"Debug mode is enabled...\nx ODDS_SNAPSHOT {json.dumps(snap)}\n",
    }
    items, notes = bf.log_items("o/r", run_fetcher=lambda run_id, _: logs[run_id])
    assert [i.pathname for i in items] == [
        "odds/2026-11-03_03-00-05_Match_Odds_Bundesliga-Germany.jsonl"
    ]
    payload = json.loads(items[0].body)
    assert payload["timestamp"] == "2026-11-03 03:00:09"
    assert payload["columns"] == snap["columns"]  # no id in log-line odds
    assert notes == ["Actions run 2: debug mode, skipped"]


def test_api_result_items(tmp_path: Path) -> None:
    api_dir = tmp_path / "api_result"
    api_dir.mkdir()
    (api_dir / "2025-08-21_14-26-54_Bundesliga-Germany_odds_api.json").write_text(
        '[{"id": "e1"}]'
    )
    (api_dir / "2024-10-06_09-12-48_Bundesliga-Germany_odds_api.json").write_text(
        '[{"id": "old"}]'
    )
    (api_dir / "2025-08-22_10-00-00_LaLiga-Spain_odds_api.json").write_text("[]")
    columns = blob_archive.ODDS_COLUMNS
    row = ["e1", "2025-08-23T13:30:00Z", "A", "B", 1.5, 4.0, 6.0]
    items = bf.api_result_items(tmp_path, SINCE, lambda _: (columns, [row]))
    assert [i.pathname for i in items] == [
        "odds/2025-08-21_14-26-54_Match_Odds_Bundesliga-Germany.jsonl"
    ]
    assert json.loads(items[0].body) == {
        "name": "Bundesliga - Germany",
        "timestamp": "2025-08-21 14:26:54",
        "columns": columns,
        "matches": [row],
    }


def test_reconstructed_items(tmp_path: Path) -> None:
    csv_path = tmp_path / "reconstructed_odds.csv"
    csv_path.write_text(
        "league,season,kickoff_utc,home_team,away_team,odds_home,odds_draw,odds_away\n"
        "L,2025/26,2025-08-15T17:00:00+00:00,A,B,2.25,3.2,3.3\n"
    )
    items, keys = bf.reconstructed_items(csv_path)
    assert items[0].pathname.startswith("odds/reconstructed/")
    assert items[0].pathname.endswith("_Match_Odds_Reconstructed.csv")
    assert items[1].pathname == "odds/reconstructed/README.txt"
    assert b"ESTIMATED" in items[1].body
    assert keys == {("A", "B", datetime(2025, 8, 15, 17, 0, tzinfo=UTC))}


def _plan(tmp_path: Path) -> bf.Plan:
    values = {
        f"2026-10-27_08-50-30_{PRED_KEY}": kv_value("A", "B", "31.10.2026 15:30"),
        f"2026-10-30_09-50-30_{PRED_KEY}": kv_value("C", "D", "01.11.2026 15:30"),
    }
    preds, fixtures, _ = bf.kv_items(values, SINCE)
    odds = [_item("2026-10-27_08-50-04", "odds", "Bundesliga-Germany")]
    csv_path = tmp_path / "reconstructed_odds.csv"
    csv_path.write_text(
        "league,season,kickoff_utc,home_team,away_team,odds_home\n"
        "L,2026/27,2026-10-31T14:30:00+00:00,A,B,1.5\n"
    )
    recon, keys = bf.reconstructed_items(csv_path)
    return bf.Plan(items=preds + odds + recon, predicted=fixtures, odds_keys=keys)


def test_dry_run_counts_and_uploads_nothing(
    tmp_path: Path, fake_blob: FakeBlob, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = _plan(tmp_path)
    uploader = bf.run_backfill(
        plan, blob_archive.empty_index(), dry_run=True, head_check=False
    )
    assert fake_blob.puts == []
    assert fake_blob.heads == []
    assert uploader.stats["kv"]["uploads"] == 2
    assert uploader.stats["api_result"]["uploads"] == 1
    assert uploader.stats["reconstructed"]["uploads"] == 2
    assert uploader.stats["manifest"]["uploads"] == 2
    assert uploader.stats["index"]["uploads"] == 1

    bf.print_report(plan, uploader, dry_run=True)
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "Odds coverage: 1/2" in out
    assert "missing: 2026-11-01 14:30 UTC" in out
    assert "C - D" in out


def test_real_run_then_second_run_uploads_nothing(
    tmp_path: Path, fake_blob: FakeBlob
) -> None:
    plan = _plan(tmp_path)
    first = bf.run_backfill(
        plan, blob_archive.read_index(), dry_run=False, head_check=False
    )
    assert first.stats["kv"]["uploads"] == 2
    assert all(
        not p["allow_overwrite"]
        for p in fake_blob.puts
        if p["pathname"] != "index.json"
    )

    index = json.loads(fake_blob.files["index.json"])
    assert [r["run_ts"] for r in index["runs"]] == [
        "2026-10-27_08-50-04",
        "2026-10-30_09-50-30",
    ]
    assert index["runs"][0]["odds"] == {
        "Bundesliga-Germany": "odds/2026-10-27_08-50-04_Bundesliga-Germany"
    }
    assert len(index["reconstructed_odds"]) == 1
    manifest = json.loads(fake_blob.files["runs/2026-10-27_08-50-04.json"])
    assert manifest["source"] == "backfill"

    puts_before = len(fake_blob.puts)
    second = bf.run_backfill(
        _plan(tmp_path), blob_archive.read_index(), dry_run=False, head_check=False
    )
    assert len(fake_blob.puts) == puts_before
    assert sum(s["uploads"] for s in second.stats.values()) == 0


def test_real_run_skips_files_missing_from_index(
    tmp_path: Path, fake_blob: FakeBlob
) -> None:
    plan = _plan(tmp_path)
    # Uploaded earlier but the index update failed
    fake_blob.files[plan.items[0].pathname] = b"x"
    uploader = bf.run_backfill(
        plan, blob_archive.read_index(), dry_run=False, head_check=False
    )
    assert uploader.stats["kv"]["existing"] == 1
    assert plan.items[0].pathname in fake_blob.heads
    index = json.loads(fake_blob.files["index.json"])
    assert plan.items[0].pathname in blob_archive.index_pathnames(index)


def test_live_cutoff_and_until() -> None:
    index = blob_archive.merge_index(
        None,
        [
            {"run_ts": "2026-10-30_09-49-58", "source": "backfill"},
            {"run_ts": "2026-11-03_03-01-30", "source": "workflow"},
        ],
    )
    cutoff = bf.live_cutoff(index)
    assert cutoff == datetime(2026, 11, 3, 2, 31, 30, tzinfo=UTC)
    assert bf.live_cutoff(blob_archive.empty_index()) is None
    earlier = datetime(2026, 9, 1, tzinfo=UTC)
    assert bf.effective_until(None, cutoff) == cutoff
    assert bf.effective_until(earlier, cutoff) == earlier
    assert bf.effective_until(None, None) is None


def test_backfill_after_go_live_skips_live_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Tue 03.11 was archived live (run_ts at StorageManager init); Actions
    # started 03:00:05, KV keys at 03:10. None of it may be backfilled again.
    index = blob_archive.merge_index(
        None, [{"run_ts": "2026-11-03_03-01-30", "source": "workflow"}]
    )
    values = {
        f"2026-10-30_09-55-00_{PRED_KEY}": kv_value("A", "B", "31.10.2026 15:30"),
        f"2026-11-03_03-10-00_{PRED_KEY}": kv_value("C", "D", "07.11.2026 15:30"),
    }
    live_log = _item("2026-11-03_03-00-05", "odds", "Bundesliga-Germany")
    live_log.source = "actions_log"
    old_log = _item("2026-10-30_09-49-58", "odds", "Bundesliga-Germany")
    old_log.source = "actions_log"
    for item in (live_log, old_log):
        item.body = json.dumps({"columns": ["odds_home"], "matches": []}).encode()
    monkeypatch.setattr(bf, "load_kv_values", lambda since: values)
    monkeypatch.setattr(bf, "make_processor", lambda: None)
    monkeypatch.setattr(bf, "api_result_items", lambda *args: [])
    monkeypatch.setattr(bf, "log_items", lambda repo: ([old_log, live_log], []))
    args = argparse.Namespace(
        since=SINCE,
        until=bf.effective_until(None, bf.live_cutoff(index)),
        data_dir=tmp_path,
        skip_logs=False,
        repo="o/r",
    )
    plan = bf.build_plan(args)
    pathnames = {i.pathname for i in plan.items}
    assert old_log.pathname in pathnames
    assert f"predictions/2026-10-30_09-55-00_{PRED_KEY}.jsonl" in pathnames
    assert live_log.pathname not in pathnames
    assert f"predictions/2026-11-03_03-10-00_{PRED_KEY}.jsonl" not in pathnames
    # Coverage check ignores matches only predicted in live runs
    assert [f.home_team for f in plan.predicted] == ["A"]

    uploader = bf.run_backfill(plan, index, dry_run=True, head_check=False)
    # One backfill run (30.10), no second manifest for the live run
    assert uploader.stats["manifest"]["uploads"] == 1


def test_crash_partway_still_indexes_uploaded_files(
    tmp_path: Path, fake_blob: FakeBlob
) -> None:
    plan = _plan(tmp_path)
    fake_blob.fail_puts[plan.items[1].pathname] = [KeyboardInterrupt()]
    with pytest.raises(KeyboardInterrupt):
        bf.run_backfill(
            plan, blob_archive.read_index(), dry_run=False, head_check=False
        )
    index = json.loads(fake_blob.files["index.json"])
    indexed = blob_archive.index_pathnames(index)
    assert plan.items[0].pathname in indexed
    assert plan.items[1].pathname not in indexed
    assert f"runs/{index['runs'][0]['run_ts']}.json" in fake_blob.files
