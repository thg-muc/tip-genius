"""Unit tests for the Blob archive helpers (SDK mocked)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import pytest
from lib import blob_archive
from vercel.blob import sync as vercel_blob

from .conftest import ALREADY_EXISTS, STORE_URL, FakeBlob

TS = "2026-10-27_09-49-22"
BASE_KEY = "Match_Predictions_Meta-Llama-Mid_FourPointsScoring_named_with-info"


def test_pathnames() -> None:
    assert blob_archive.predictions_pathname(TS, BASE_KEY) == (
        "predictions/2026-10-27_09-49-22_Match_Predictions_Meta-Llama-Mid_"
        "FourPointsScoring_named_with-info.jsonl"
    )
    assert (
        blob_archive.odds_pathname(TS, "Bundesliga - Germany")
        == "odds/2026-10-27_09-49-22_Match_Odds_Bundesliga-Germany.jsonl"
    )
    assert (
        blob_archive.odds_pathname(TS, "UEFA Champions League")
        == "odds/2026-10-27_09-49-22_Match_Odds_UEFAChampionsLeague.jsonl"
    )
    assert blob_archive.manifest_pathname(TS) == "runs/2026-10-27_09-49-22.json"
    assert (
        blob_archive.reconstructed_odds_pathname(TS)
        == "odds/reconstructed/2026-10-27_09-49-22_Match_Odds_Reconstructed.csv"
    )
    assert (
        blob_archive.provider_key(BASE_KEY)
        == "Meta-Llama-Mid_FourPointsScoring_named_with-info"
    )


def test_timestamp_formats_are_utc() -> None:
    moment = datetime(2026, 10, 27, 9, 49, 22, tzinfo=UTC)
    assert blob_archive.format_run_ts(moment) == TS
    assert blob_archive.format_payload_ts(moment) == "2026-10-27 09:49:22"


def test_prediction_payload_and_jsonl() -> None:
    data = {
        "Bundesliga - Germany": [{"home_team": "Bayern München"}],
        "La Liga - Spain": [],
    }
    payload = blob_archive.build_prediction_payload(data, "2026-10-27 09:49:22")
    assert payload == [
        {
            "name": "Bundesliga - Germany",
            "timestamp": "2026-10-27 09:49:22",
            "matches": [{"home_team": "Bayern München"}],
        },
        {"name": "La Liga - Spain", "timestamp": "2026-10-27 09:49:22", "matches": []},
    ]
    lines = blob_archive.to_jsonl(payload).decode("utf-8").splitlines()
    assert [json.loads(line) for line in lines] == payload
    assert "München" in lines[0]  # ensure_ascii=False, as _store_to_file


def test_odds_payload() -> None:
    payload = blob_archive.build_odds_payload(
        "Bundesliga - Germany",
        "2026-10-27 09:49:22",
        blob_archive.ODDS_COLUMNS,
        [["abc", "2026-10-31T14:30:00Z", "A", "B", 1.5, 4.0, 6.0]],
    )
    assert payload == [
        {
            "name": "Bundesliga - Germany",
            "timestamp": "2026-10-27 09:49:22",
            "columns": blob_archive.ODDS_COLUMNS,
            "matches": [["abc", "2026-10-31T14:30:00Z", "A", "B", 1.5, 4.0, 6.0]],
        }
    ]
    assert blob_archive.ODDS_COLUMNS[0] == "id"


def test_manifest_and_index_entry() -> None:
    manifest = blob_archive.build_manifest(
        TS, {"LaLiga-Spain": "odds/b", "Bundesliga-Germany": "odds/a"}, {"P": "p/x"}
    )
    assert manifest == {
        "run_ts": TS,
        "source": "workflow",
        "odds": {"Bundesliga-Germany": "odds/a", "LaLiga-Spain": "odds/b"},
        "predictions": {"P": "p/x"},
    }
    entry = blob_archive.index_entry(manifest, manifest_written=True)
    assert entry["manifest"] == "runs/2026-10-27_09-49-22.json"
    assert (
        blob_archive.index_entry(manifest, manifest_written=False)["manifest"] is None
    )


def test_merge_index_sorts_and_deduplicates() -> None:
    now = datetime(2026, 11, 1, tzinfo=UTC)
    first = {"run_ts": "2026-10-30_03-00-00", "odds": {"A": "o1"}, "predictions": {}}
    older = {"run_ts": "2025-08-01_03-00-00", "odds": {}, "predictions": {"P": "p0"}}
    index = blob_archive.merge_index(None, [first], ["odds/reconstructed/x.csv"], now)
    again = {
        "run_ts": "2026-10-30_03-00-00",
        "manifest": "runs/2026-10-30_03-00-00.json",
        "odds": {},
        "predictions": {"P": "p1"},
    }
    merged = blob_archive.merge_index(
        index, [again, older], ["odds/reconstructed/x.csv"], now
    )
    assert [r["run_ts"] for r in merged["runs"]] == [
        "2025-08-01_03-00-00",
        "2026-10-30_03-00-00",
    ]
    assert merged["runs"][1] == {
        "run_ts": "2026-10-30_03-00-00",
        "source": "workflow",
        "manifest": "runs/2026-10-30_03-00-00.json",
        "odds": {"A": "o1"},
        "predictions": {"P": "p1"},
    }
    assert merged["reconstructed_odds"] == ["odds/reconstructed/x.csv"]
    assert merged["updated_at"] == "2026-11-01 00:00:00"
    assert blob_archive.index_pathnames(merged) == {
        "o1",
        "p0",
        "p1",
        "runs/2026-10-30_03-00-00.json",
        "odds/reconstructed/x.csv",
    }


def test_put_is_private_write_once(fake_blob: FakeBlob) -> None:
    assert blob_archive.put_with_retry("odds/a.jsonl", b"x", content_type="t")
    put = fake_blob.puts[0]
    assert put["access"] == "private"
    assert put["add_random_suffix"] is False
    assert put["allow_overwrite"] is False


def test_put_retries_once_then_succeeds(fake_blob: FakeBlob) -> None:
    fake_blob.fail_puts["a"] = [vercel_blob.BlobServiceNotAvailable("down")]
    assert blob_archive.put_with_retry("a", b"x", content_type="t")
    assert len(fake_blob.puts) == 2


def test_put_gives_up_after_one_retry(
    fake_blob: FakeBlob, caplog: pytest.LogCaptureFixture
) -> None:
    fake_blob.fail_puts["a"] = [
        vercel_blob.BlobServiceNotAvailable("down", status_code=503),
        vercel_blob.BlobServiceNotAvailable("down", status_code=503),
        vercel_blob.BlobServiceNotAvailable("down", status_code=503),
    ]
    with caplog.at_level(logging.WARNING):
        assert not blob_archive.put_with_retry("a", b"x", content_type="t")
    assert len(fake_blob.puts) == 2
    assert "Blob write failed for a (HTTP 503)" in caplog.text


def test_put_already_exists_is_success_without_retry(
    fake_blob: FakeBlob, caplog: pytest.LogCaptureFixture
) -> None:
    fake_blob.files["a"] = b"old"
    with caplog.at_level(logging.WARNING):
        assert blob_archive.put_with_retry("a", b"new", content_type="t")
    assert len(fake_blob.puts) == 1
    assert fake_blob.files["a"] == b"old"
    assert caplog.text == ""


def test_other_bad_request_is_not_already_exists() -> None:
    assert not blob_archive.is_already_exists(
        vercel_blob.BlobError("Invalid pathname", status_code=400)
    )
    assert blob_archive.is_already_exists(
        vercel_blob.BlobError(ALREADY_EXISTS, status_code=400)
    )


def test_blob_exists_transient_failure_returns_false(fake_blob: FakeBlob) -> None:
    fake_blob.files["a"] = b"x"
    assert blob_archive.blob_exists("a")
    fake_blob.fail_heads = vercel_blob.BlobServiceRateLimited(1)
    assert not blob_archive.blob_exists("a")


def test_fresh_index_read_bypasses_cache(fake_blob: FakeBlob) -> None:
    assert blob_archive.update_index([{"run_ts": TS}])
    assert blob_archive.read_index(fresh=True)["runs"][0]["run_ts"] == TS
    assert fake_blob.gets[-1] == f"{STORE_URL}/index.json?etag=%22v1%22"


def test_first_workflow_run() -> None:
    entries = [
        {"run_ts": "2026-11-06_03-01-00", "source": "workflow"},
        {"run_ts": "2025-08-01_03-00-00", "source": "backfill"},
        {"run_ts": "2026-11-03_03-01-30", "source": "workflow"},
    ]
    index = blob_archive.merge_index(None, entries)
    assert blob_archive.first_workflow_run(index) == "2026-11-03_03-01-30"
    assert blob_archive.first_workflow_run(blob_archive.empty_index()) is None


def test_put_does_not_retry_auth_errors(fake_blob: FakeBlob) -> None:
    fake_blob.fail_puts["a"] = [vercel_blob.BlobAccessError()]
    assert not blob_archive.put_with_retry("a", b"x", content_type="t")
    assert len(fake_blob.puts) == 1


def test_update_index_creates_and_appends(fake_blob: FakeBlob) -> None:
    entry = {"run_ts": TS, "manifest": None, "odds": {"A": "o"}, "predictions": {}}
    assert blob_archive.update_index([entry])
    put = fake_blob.puts[-1]
    assert put["pathname"] == "index.json"
    assert put["allow_overwrite"] is True
    entry2 = {**entry, "run_ts": "2026-10-30_03-00-00", "odds": {"A": "o2"}}
    assert blob_archive.update_index([entry2])
    index = json.loads(fake_blob.files["index.json"])
    assert [r["run_ts"] for r in index["runs"]] == [TS, "2026-10-30_03-00-00"]


def test_update_index_never_overwrites_unreadable_index(
    fake_blob: FakeBlob, caplog: pytest.LogCaptureFixture
) -> None:
    fake_blob.files["index.json"] = b'{"runs": []}'
    fake_blob.fail_get = vercel_blob.BlobServiceNotAvailable("down", status_code=503)
    with caplog.at_level(logging.WARNING):
        assert not blob_archive.update_index([{"run_ts": TS}])
    assert fake_blob.puts == []
    assert "index not updated" in caplog.text


def test_reader_uses_index_not_list(fake_blob: FakeBlob) -> None:
    payload = [{"name": "L", "timestamp": "t", "matches": [1]}]
    fake_blob.files["predictions/p.jsonl"] = blob_archive.to_jsonl(payload)
    entry = {"run_ts": TS, "odds": {}, "predictions": {"P": "predictions/p.jsonl"}}
    fake_blob.files["index.json"] = blob_archive.to_json(
        blob_archive.merge_index(None, [entry])
    )
    reader = blob_archive.BlobArchiveReader()
    runs = reader.runs()
    assert runs[0]["predictions"]["P"] == "predictions/p.jsonl"
    assert reader.fetch_jsonl(runs[0]["predictions"]["P"]) == payload


def test_reader_empty_store(fake_blob: FakeBlob) -> None:
    assert blob_archive.BlobArchiveReader().runs() == []
    assert fake_blob.files == {}
