"""Unit tests for fixture selection in ``utils/reconstruct_odds.py``."""

from datetime import UTC, datetime, timedelta

from utils import reconstruct_odds as ro
from utils.kv_snapshots import Prediction

CL = "UEFA Champions League"
LIGA = "La Liga - Spain"


def pred(
    league: str, kickoff: datetime, snapshot: datetime, provider: str = "P"
) -> Prediction:
    return Prediction(provider, snapshot, league, kickoff, "Home", "Away", 1, 0)


def at(day: int, month: int = 11, year: int = 2025) -> datetime:
    return datetime(year, month, day, 20, tzinfo=UTC)


def test_uefa_repeat_pairing_splits_into_legs() -> None:
    preds = [
        pred(CL, at(4), at(3)),
        pred(CL, at(28, 4, 2026), at(27, 4, 2026)),
    ]
    legs = ro.fixture_legs(preds)
    keys = {ro.fixture_key(p, legs) for p in preds}
    assert keys == {
        (CL, "Home", "Away", "2025/26", "2025-11-04"),
        (CL, "Home", "Away", "2025/26"),  # last leg keeps the plain key
    }


def test_domestic_reschedule_stays_one_fixture() -> None:
    preds = [
        pred(LIGA, at(14, 12), at(13, 12)),
        pred(LIGA, at(18, 2, 2026), at(17, 2, 2026)),
    ]
    legs = ro.fixture_legs(preds)
    assert {ro.fixture_key(p, legs) for p in preds} == {
        (LIGA, "Home", "Away", "2025/26")
    }


def test_all_predicted_falls_back_to_earliest_after_kickoff_snapshot() -> None:
    kickoff = at(14, 10, 2026)
    late = [
        pred(CL, kickoff, kickoff + timedelta(hours=2)),
        pred(CL, kickoff, kickoff + timedelta(hours=1)),
    ]
    legs = ro.fixture_legs(late)
    (selected,) = ro.all_predicted(late, legs)
    assert selected.snapshot == kickoff + timedelta(hours=1)


def test_all_predicted_prefers_last_snapshot_before_kickoff() -> None:
    kickoff = at(4)
    preds = [
        pred(CL, kickoff, kickoff - timedelta(days=3)),
        pred(CL, kickoff, kickoff - timedelta(days=1)),
        pred(CL, kickoff, kickoff + timedelta(hours=1)),
    ]
    (selected,) = ro.all_predicted(preds, ro.fixture_legs(preds))
    assert selected.snapshot == kickoff - timedelta(days=1)
