"""``eval_providers --include-backfill`` reads backfill files from Blob only."""

from __future__ import annotations

from datetime import UTC, datetime

from lib import blob_archive

from utils import eval_providers as ev

from .conftest import FakeBlob

KEY = "Match_Predictions_Mistral-Ministral_FourPointsScoring_named_with-info"


def test_load_backfill_snapshots(fake_blob: FakeBlob) -> None:
    path = blob_archive.backfill_predictions_pathname("2026-09-11_09-37-34", KEY)
    payload = blob_archive.build_backfill_payload(
        {
            "Bundesliga - Germany": [
                {
                    "commence_time_str": "12.09.2026 15:30",
                    "home_team": "Bayern",
                    "away_team": "Köln",
                    "prediction_home": 2,
                    "prediction_away": 0,
                }
            ]
        },
        "2026-09-11 09:37:34",
        odds_sources={"Bundesliga - Germany": "reconstructed"},
        generated_at="2026-11-04 12:00:00",
        script="backfill_llm_predictions.py",
    )
    fake_blob.files[path] = blob_archive.to_jsonl(payload)
    index = blob_archive.merge_backfill_index(
        None, {"2026-09-11_09-37-34": {blob_archive.provider_key(KEY): path}}
    )
    fake_blob.files[blob_archive.INDEX_PATHNAME] = blob_archive.to_json(index)

    snapshots = ev.load_backfill_snapshots()
    stamp = datetime(2026, 9, 11, 9, 37, 34, tzinfo=UTC)
    assert list(snapshots) == [(stamp, "Mistral-Ministral")]
    [pred] = ev.flatten(snapshots)
    assert (pred.provider, pred.snapshot, pred.home_goals) == (
        "Mistral-Ministral",
        stamp,
        2,
    )
    assert pred.kickoff == datetime(2026, 9, 12, 13, 30, tzinfo=UTC)
