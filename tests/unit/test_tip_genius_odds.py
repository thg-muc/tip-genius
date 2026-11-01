"""Unit tests for archiving odds from the workflow (no network)."""

from __future__ import annotations

import json
import logging
from typing import Any

import polars as pl
import pytest
from lib.api_data import BaseAPI
from lib.storage_manager import StorageManager

# Runtime resolves the module src/tip_genius/tip_genius.py (pytest pythonpath),
# pyright the package src/tip_genius
from tip_genius import TipGenius  # pyright: ignore[reportAttributeAccessIssue]

from .conftest import FakeBlob

ROW = {
    "id": "e1",
    "commence_time": "2026-11-07T14:30:00Z",
    "home_team": "Bayern München",
    "away_team": "Union Berlin",
    "odds_home": 1.3,
    "odds_draw": 6.0,
    "odds_away": 9.5,
}


class StubAPI(BaseAPI):
    """API pipeline returning one processed fixture."""

    def __init__(self) -> None:
        self.api_name = "stub_api"

    def fetch_api_data(self, sport_key: str) -> dict[str, Any]:  # noqa: ARG002
        return {"stub": True}

    def process_api_data(
        self,
        api_result: dict[str, Any],  # noqa: ARG002
        *,
        named_teams: bool,  # noqa: ARG002
        additional_info: bool,  # noqa: ARG002
    ) -> pl.DataFrame:
        return pl.DataFrame([ROW])


@pytest.fixture
def tip_genius(monkeypatch: pytest.MonkeyPatch) -> TipGenius:
    monkeypatch.delenv("DEBUG_MODE", raising=False)
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "blob-token")
    monkeypatch.delenv("KV_REST_API_TOKEN", raising=False)
    return TipGenius(api_pipeline=StubAPI())


def test_odds_logged_and_archived_once(
    tip_genius: TipGenius, fake_blob: FakeBlob, caplog: pytest.LogCaptureFixture
) -> None:
    tip_genius.storage_manager = StorageManager("x", write_to_blob=True)
    with caplog.at_level(logging.INFO):
        tip_genius.log_odds_snapshot(api_result={}, sport="Bundesliga - Germany")

    # Log line keeps its old shape (no id)
    line = next(
        r.getMessage() for r in caplog.records if "ODDS_SNAPSHOT" in r.getMessage()
    )
    snapshot = json.loads(line.removeprefix("ODDS_SNAPSHOT "))
    assert "id" not in snapshot["columns"]

    run_ts = tip_genius.storage_manager.run_ts
    pathname = f"odds/{run_ts}_Match_Odds_Bundesliga-Germany.jsonl"
    archived = json.loads(fake_blob.files[pathname])
    assert archived["columns"][0] == "id"
    assert archived["matches"] == [list(ROW.values())]
    assert archived["timestamp"] == snapshot["fetched_at"][:19].replace("T", " ")
    assert len(fake_blob.puts) == 1


def test_odds_archive_failure_never_raises(
    tip_genius: TipGenius, fake_blob: FakeBlob
) -> None:
    from vercel.blob import sync as vercel_blob  # noqa: PLC0415

    fake_blob.fail_all_puts = vercel_blob.BlobServiceNotAvailable("down")
    tip_genius.storage_manager = StorageManager("x", write_to_blob=True)
    tip_genius.log_odds_snapshot(api_result={}, sport="Bundesliga - Germany")
    assert fake_blob.files == {}
