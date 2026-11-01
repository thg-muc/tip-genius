"""Unit tests for the Blob archive in StorageManager (KV HTTP and SDK mocked)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import pytest
from lib import storage_manager as sm_module
from lib.storage_manager import StorageManager
from vercel.blob import sync as vercel_blob

from .conftest import FakeBlob

BASE_KEY = "Match_Predictions_Mistral-Ministral_FourPointsScoring_named_with-info"
DATA = {"Bundesliga - Germany": [{"home_team": "A", "away_team": "B"}]}
RUN_START = datetime(2026, 11, 3, 3, 0, 5, tzinfo=UTC)


class _Response:
    status_code = 200
    text = "OK"


@pytest.fixture
def kv_posts(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Configure KV and capture its HTTP calls."""
    monkeypatch.setenv("KV_REST_API_TOKEN", "kv-token")
    monkeypatch.setenv("KV_REST_API_URL", "https://kv.example")
    posts: list[dict[str, Any]] = []

    def post(url: str, **kwargs: Any) -> _Response:
        posts.append({"url": url, **kwargs})
        return _Response()

    monkeypatch.setattr(sm_module.requests, "post", post)
    return posts


@pytest.fixture
def blob_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "blob-token")


def make_manager(**kwargs: Any) -> StorageManager:
    options = {"write_to_kv": True, "write_to_blob": True, **kwargs}
    return StorageManager("data/match_predictions", run_started_at=RUN_START, **options)


@pytest.mark.usefixtures("blob_token")
def test_blob_file_matches_timestamped_kv_key(
    kv_posts: list[dict[str, Any]], fake_blob: FakeBlob
) -> None:
    manager = make_manager()
    assert manager.blob_initialized
    assert manager.store_predictions(DATA, BASE_KEY)

    timestamped, latest = kv_posts
    assert latest["url"] == f"https://kv.example/set/{BASE_KEY}"
    stamp = timestamped["url"].removeprefix("https://kv.example/set/")
    stamp = stamp.removesuffix(f"_{BASE_KEY}")
    # Same payload object (and timestamp) for both KV keys
    assert timestamped["json"] == latest["json"]

    pathname = f"predictions/{stamp}_{BASE_KEY}.jsonl"
    lines = fake_blob.files[pathname].decode("utf-8").splitlines()
    assert [json.loads(line) for line in lines] == timestamped["json"]
    # KV value format unchanged
    assert timestamped["json"] == [
        {
            "name": "Bundesliga - Germany",
            "timestamp": timestamped["json"][0]["timestamp"],
            "matches": DATA["Bundesliga - Germany"],
        }
    ]
    # Only timestamped files are archived
    assert [p["pathname"] for p in fake_blob.puts] == [pathname]
    key = "Mistral-Ministral_FourPointsScoring_named_with-info"
    assert manager.blob_predictions == {key: pathname}


@pytest.mark.usefixtures("blob_token")
def test_file_export_uses_same_payload(
    kv_posts: list[dict[str, Any]], fake_blob: FakeBlob, tmp_path: Any
) -> None:
    manager = make_manager(export_to_file=True)
    assert manager.store_predictions(DATA, BASE_KEY, str(tmp_path))
    latest = (tmp_path / f"{BASE_KEY}.jsonl").read_text(encoding="utf-8")
    blob_body = next(iter(fake_blob.files.values())).decode("utf-8")
    assert latest == blob_body
    assert [json.loads(line) for line in latest.splitlines()] == kv_posts[0]["json"]


@pytest.mark.usefixtures("blob_token")
def test_odds_manifest_and_index(
    kv_posts: list[dict[str, Any]], fake_blob: FakeBlob
) -> None:
    manager = make_manager()
    assert manager.run_ts == "2026-11-03_03-00-05"
    fetched = datetime(2026, 11, 3, 3, 0, 9, tzinfo=UTC)
    columns = ["id", "commence_time", "home_team", "away_team"]
    rows = [["x1", "2026-11-07T14:30:00Z", "A", "B"]]
    assert manager.store_odds("Bundesliga - Germany", columns, rows, fetched)
    manager.store_predictions(DATA, BASE_KEY)
    manager.finalize_blob_archive()

    odds_path = "odds/2026-11-03_03-00-05_Match_Odds_Bundesliga-Germany.jsonl"
    assert json.loads(fake_blob.files[odds_path]) == {
        "name": "Bundesliga - Germany",
        "timestamp": "2026-11-03 03:00:09",
        "columns": columns,
        "matches": rows,
    }
    manifest = json.loads(fake_blob.files["runs/2026-11-03_03-00-05.json"])
    assert manifest["run_ts"] == "2026-11-03_03-00-05"
    assert manifest["odds"] == {"Bundesliga-Germany": odds_path}
    assert list(manifest["predictions"].values()) == list(
        manager.blob_predictions.values()
    )
    index = json.loads(fake_blob.files["index.json"])
    assert index["runs"] == [
        {
            "run_ts": "2026-11-03_03-00-05",
            "manifest": "runs/2026-11-03_03-00-05.json",
            "odds": manifest["odds"],
            "predictions": manifest["predictions"],
        }
    ]
    assert len(kv_posts) == 2


@pytest.mark.usefixtures("blob_token")
def test_blob_failure_keeps_run_green(
    kv_posts: list[dict[str, Any]],
    fake_blob: FakeBlob,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_blob.fail_all_puts = vercel_blob.BlobServiceNotAvailable(
        "down", status_code=503
    )
    manager = make_manager()
    with caplog.at_level(logging.WARNING):
        assert manager.store_predictions(DATA, BASE_KEY) is True
        assert not manager.store_odds("L", ["id"], [], RUN_START)
        manager.finalize_blob_archive()
    assert len(kv_posts) == 2  # KV writes unaffected
    assert "Blob write failed for predictions/" in caplog.text
    assert "(HTTP 503)" in caplog.text
    assert manager.blob_predictions == {}
    # Nothing was written, so no manifest or index attempt
    assert all(not p["pathname"].startswith("runs/") for p in fake_blob.puts)


def test_missing_token_skips_blob(
    monkeypatch: pytest.MonkeyPatch,
    kv_posts: list[dict[str, Any]],
    fake_blob: FakeBlob,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    with caplog.at_level(logging.WARNING):
        manager = make_manager()
    assert not manager.blob_initialized
    assert caplog.text.count("BLOB_READ_WRITE_TOKEN is not set") == 1
    assert manager.store_predictions(DATA, BASE_KEY)
    assert not manager.store_odds("L", ["id"], [], RUN_START)
    manager.finalize_blob_archive()
    assert fake_blob.puts == []
    assert len(kv_posts) == 2


@pytest.mark.usefixtures("blob_token")
def test_debug_mode_skips_blob(
    kv_posts: list[dict[str, Any]],
    fake_blob: FakeBlob,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        manager = make_manager(debug=True)
    assert not manager.blob_initialized
    assert "Debug mode: skipping Vercel Blob archive" in caplog.text
    manager.store_predictions(DATA, f"{BASE_KEY}_debug")
    manager.store_odds("L", ["id"], [], RUN_START)
    manager.finalize_blob_archive()
    assert fake_blob.puts == []
    assert len(kv_posts) == 2


@pytest.mark.usefixtures("blob_token")
def test_blob_switch_off(kv_posts: list[dict[str, Any]], fake_blob: FakeBlob) -> None:
    manager = make_manager(write_to_blob=False)
    assert not manager.blob_initialized
    manager.store_predictions(DATA, BASE_KEY)
    assert fake_blob.puts == []
    assert len(kv_posts) == 2


@pytest.mark.usefixtures("blob_token")
def test_finalize_without_files_writes_nothing(
    kv_posts: list[dict[str, Any]], fake_blob: FakeBlob
) -> None:
    make_manager().finalize_blob_archive()
    assert fake_blob.puts == []
