"""Unit tests for the Blob prediction table (Blob SDK mocked, no network)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from lib import blob_archive
from lib import prediction_table as pt

from .conftest import FakeBlob

BL = "Bundesliga - Germany"
CL = "UEFA Champions League"
SEASON = "2026/27"
NOW = datetime(2026, 11, 10, 3, 0, tzinfo=UTC)
TABLE = pt.table_pathname(SEASON)
SUFFIX = "_FourPointsScoring_named_with-info"


def at(day: int, hour: int = 12, month: int = 11) -> datetime:
    return datetime(2026, month, day, hour, tzinfo=UTC)


def ts(moment: datetime) -> str:
    return blob_archive.format_run_ts(moment)


def berlin(moment: datetime) -> str:
    return moment.astimezone(pt.results_archive.BERLIN).strftime("%d.%m.%Y %H:%M")


def tip(
    provider: str,
    made: datetime,
    kickoff: datetime,
    goals: tuple[int, int] = (2, 1),
    *,
    league: str = BL,
    home: str = "Bayern",
    away: str = "Köln",
    run: datetime | None = None,
) -> pt.Tip:
    runs = frozenset({ts(run or made)})
    return pt.Tip(provider, pt.LIVE, made, league, kickoff, home, away, goals, runs)


def quote(
    at_run: datetime | None,
    kickoff: datetime,
    odds: tuple[float, float, float] = (1.5, 4.0, 6.0),
) -> pt.Quote:
    if at_run is None:
        return pt.Quote(BL, kickoff, "Bayern", "Köln", odds, "", pt.ODDS_RECONSTRUCTED)
    return pt.Quote(BL, kickoff, "Bayern", "Köln", odds, ts(at_run), pt.ODDS_LOGGED)


# %% pick rule


def test_latest_prediction_before_kickoff_wins() -> None:
    kickoff = at(7, 15)
    rows = pt.build_rows(
        [
            tip("P", at(3), kickoff, (1, 0)),
            tip("P", at(6), kickoff, (2, 1)),
            tip("P", at(7, 16), kickoff, (3, 0)),  # after kickoff
        ],
        [],
        [],
    )
    [row] = rows
    assert (row["tip_home"], row["tip_away"]) == ("2", "1")
    assert row["predicted_at"] == ts(at(6))
    assert row["live_runs"] == f"{ts(at(3))};{ts(at(6))}"
    assert row["match_key"] == f"{BL}|2026-11-07T15:00Z|Bayern|Köln"


def test_reschedule_collapses_to_newest_kickoff() -> None:
    old, new = at(7, 15), at(28, 15)
    rows = pt.build_rows(
        [
            tip("A", at(3), old, (1, 0)),  # only predicted for the old date
            tip("B", at(3), old, (0, 0)),
            tip("B", at(20), new, (2, 2)),  # newest record: moved kickoff
        ],
        [],
        [],
    )
    assert {r["kickoff_utc"] for r in rows} == {"2026-11-28T15:00Z"}
    by_provider = {r["provider"]: r for r in rows}
    assert by_provider["A"]["tip_kickoff_utc"] == "2026-11-07T15:00Z"
    assert by_provider["B"]["tip_home"] == "2"


def test_uefa_repeat_pairing_keeps_legs_apart() -> None:
    first, second = at(4, 20, 11), datetime(2027, 3, 10, 20, tzinfo=UTC)
    rows = pt.build_rows(
        [
            tip("P", at(3), first, league=CL),
            tip("P", datetime(2027, 3, 9, tzinfo=UTC), second, (0, 1), league=CL),
        ],
        [],
        [],
    )
    assert sorted(r["kickoff_utc"] for r in rows) == [
        "2026-11-04T20:00Z",
        "2027-03-10T20:00Z",
    ]


def test_case_variants_map_to_one_provider() -> None:
    kickoff = at(7, 15)
    rows = pt.build_rows(
        [
            tip("Meta-Llama", at(3), kickoff),
            tip("Meta-Llama", at(4), kickoff, home="X", away="Y"),
            tip("meta-llama", at(6), kickoff, (0, 0)),
        ],
        [],
        [],
    )
    assert {r["provider"] for r in rows} == {"Meta-Llama"}
    assert len(rows) == 2


# %% odds


def test_latest_odds_before_kickoff_then_reconstructed() -> None:
    kickoff = at(7, 15)
    [row] = pt.build_rows(
        [tip("P", at(3), kickoff)],
        [],
        [
            quote(at(3), kickoff, (1.5, 4.0, 6.0)),
            quote(at(6), kickoff, (1.6, 4.0, 5.0)),
            quote(at(8), kickoff, (9.0, 9.0, 9.0)),  # after kickoff
        ],
        [quote(None, kickoff, (2.0, 3.0, 4.0))],
    )
    assert (row["odds_home"], row["odds_at"], row["odds_source"]) == (
        "1.6",
        ts(at(6)),
        "logged",
    )
    [row] = pt.build_rows(
        [tip("P", at(3), kickoff)], [], [], [quote(None, at(8, 15), (2.0, 3.0, 4.0))]
    )
    assert (row["odds_home"], row["odds_at"], row["odds_source"]) == (
        "2.0",
        "",
        "reconstructed",
    )


def test_incremental_merge_equals_full_rebuild() -> None:
    kickoff, later = at(7, 15), at(14, 15)
    old_tips = [tip("A", at(3), kickoff), tip("B", at(3), kickoff, (0, 0))]
    new_tips = [
        tip("A", at(6), kickoff, (1, 1)),
        tip("A", at(6), later, home="X", away="Y"),
    ]
    quotes = [quote(at(3), kickoff), quote(at(6), kickoff, (1.7, 4.0, 5.0))]
    first = pt.build_rows(old_tips, [], quotes[:1])
    base_tips, observations, base_quotes = pt.from_rows(first)
    merged = pt.build_rows(base_tips + new_tips, observations, base_quotes + quotes[1:])
    full = pt.build_rows(old_tips + new_tips, [], quotes)
    assert merged == full


def test_csv_round_trip_and_bad_header() -> None:
    rows = pt.build_rows([tip("P", at(3), at(7, 15))], [], [quote(at(3), at(7, 15))])
    assert pt.rows_from_csv(pt.rows_to_csv(rows)) == rows
    with pytest.raises(ValueError, match="header"):
        pt.rows_from_csv(b"a,b\n1,2\n")


# %% Blob update with watermark and guards


def prediction_file(
    fake: FakeBlob,
    provider: str,
    made: datetime,
    matches: list[tuple[datetime, str, str, int, int]],
    *,
    backfill_run: str | None = None,
) -> str:
    base = f"Match_Predictions_{provider}{SUFFIX}"
    if backfill_run:
        path = blob_archive.backfill_predictions_pathname(backfill_run, base)
    else:
        path = blob_archive.predictions_pathname(ts(made), base)
    records = [
        {
            "commence_time_str": berlin(k),
            "home_team": h,
            "away_team": a,
            "prediction_home": x,
            "prediction_away": y,
        }
        for k, h, a, x, y in matches
    ]
    fake.files[path] = blob_archive.to_jsonl(
        blob_archive.build_prediction_payload({BL: records}, "x")
    )
    return path


def odds_file(fake: FakeBlob, run: datetime, kickoff: datetime) -> str:
    path = blob_archive.odds_pathname(ts(run), BL)
    fake.files[path] = blob_archive.to_jsonl(
        blob_archive.build_odds_payload(
            BL,
            "x",
            blob_archive.ODDS_COLUMNS,
            [["e1", kickoff.isoformat(), "Bayern", "Köln", 1.5, 4.0, 6.0]],
        )
    )
    return path


def add_run(
    fake: FakeBlob, run: datetime, predictions: dict[str, str], odds: dict[str, str]
) -> None:
    current = fake.files.get(blob_archive.INDEX_PATHNAME)
    index = blob_archive.read_index() if current else None
    entry = {"run_ts": ts(run), "predictions": predictions, "odds": odds}
    fake.files[blob_archive.INDEX_PATHNAME] = blob_archive.to_json(
        blob_archive.merge_index(index, [entry])
    )


def seed_two_runs(fake: FakeBlob) -> None:
    for run, goals in ((at(3, 3), (1, 0)), (at(6, 3), (2, 1))):
        path = prediction_file(fake, "P", run, [(at(7, 15), "Bayern", "Köln", *goals)])
        add_run(fake, run, {"P": path}, {"BL": odds_file(fake, run, at(7, 15))})


def update(fake: FakeBlob, now: datetime = NOW, **kwargs: Any) -> pt.TableReport:
    index = blob_archive.read_index(fresh=True)
    reader = blob_archive.BlobArchiveReader()
    return pt.update_prediction_table(
        SEASON,
        index=index,
        now=now,
        fetch_jsonl=reader.fetch_jsonl,
        fetch_bytes=reader.fetch_bytes,
        wait=0,
        **kwargs,
    )


def store_state(fake: FakeBlob, report: pt.TableReport) -> None:
    index = blob_archive.read_index()
    index = blob_archive.merge_results_index(
        index, {TABLE: report.index_entry or {}}, key=blob_archive.STATS_KEY
    )
    index = blob_archive.merge_results_scan(
        index, {SEASON: report.scan_state}, key=blob_archive.STATS_SCAN_KEY
    )
    fake.files[blob_archive.INDEX_PATHNAME] = blob_archive.to_json(index)


def file_gets(fake: FakeBlob, prefix: str) -> list[str]:
    return [g for g in fake.gets if g.startswith(prefix)]


def test_first_run_scans_fully_and_writes_verified(fake_blob: FakeBlob) -> None:
    seed_two_runs(fake_blob)
    report = update(fake_blob)
    assert (report.mode, report.status) == ("full", "written")
    assert report.reason == "no scan state"
    [row] = pt.rows_from_csv(fake_blob.files[TABLE])
    assert (row["tip_home"], row["odds_at"]) == ("2", ts(at(6, 3)))
    assert report.index_entry == {"rows": 1, "updated_at": "2026-11-10 03:00:00"}
    assert report.scan_state is not None
    assert report.scan_state["through"] == ts(at(6, 3))
    assert report.scan_state["files"] == 2


def test_incremental_run_reads_only_new_files(fake_blob: FakeBlob) -> None:
    seed_two_runs(fake_blob)
    store_state(fake_blob, update(fake_blob))
    run = at(10, 3)
    path = prediction_file(fake_blob, "P", run, [(at(14, 15), "Union", "Mainz", 0, 0)])
    add_run(fake_blob, run, {"P": path}, {})
    fake_blob.gets.clear()
    report = update(fake_blob)
    assert report.mode == "incremental"
    assert file_gets(fake_blob, "predictions/") == [path]
    assert file_gets(fake_blob, "odds/") == []
    assert report.status == "written"
    assert report.rows == 2
    assert report.scan_state is not None
    assert report.scan_state["through"] == ts(run)


def test_unchanged_run_writes_nothing(fake_blob: FakeBlob) -> None:
    seed_two_runs(fake_blob)
    store_state(fake_blob, update(fake_blob))
    fake_blob.puts.clear()
    report = update(fake_blob)
    assert (report.mode, report.status, report.files_read) == (
        "incremental",
        "unchanged",
        0,
    )
    assert fake_blob.puts == []


def test_changed_older_files_trigger_full_scan(fake_blob: FakeBlob) -> None:
    seed_two_runs(fake_blob)
    store_state(fake_blob, update(fake_blob))
    path = prediction_file(
        fake_blob,
        "Late",
        at(3, 3),
        [(at(7, 15), "Bayern", "Köln", 3, 1)],
        backfill_run=ts(at(3, 3)),
    )
    index = blob_archive.merge_backfill_index(
        blob_archive.read_index(), {ts(at(3, 3)): {"Late": path}}
    )
    fake_blob.files[blob_archive.INDEX_PATHNAME] = blob_archive.to_json(index)
    report = update(fake_blob)
    assert report.mode == "full"
    assert "changed" in report.reason
    rows = pt.rows_from_csv(fake_blob.files[TABLE])
    assert {r["provider"]: r["origin"] for r in rows} == {
        "Late": "backfill",
        "P": "live",
    }


def test_full_scan_after_interval_or_when_forced(fake_blob: FakeBlob) -> None:
    seed_two_runs(fake_blob)
    store_state(fake_blob, update(fake_blob))
    assert update(fake_blob, NOW + timedelta(days=31)).mode == "full"
    report = update(fake_blob, force_full="results step scanned fully")
    assert (report.mode, report.reason) == ("full", "results step scanned fully")
    # Odds are carried over from the table, not re-read
    assert report.odds_read == 0


def test_never_shrinks(fake_blob: FakeBlob) -> None:
    seed_two_runs(fake_blob)
    report = update(fake_blob)
    report.index_entry = {"rows": 5, "updated_at": "2026-11-10 03:00:00"}
    store_state(fake_blob, report)
    fake_blob.puts.clear()
    # The table has 1 row, the index 5: a stale read, nothing written
    assert update(fake_blob).status == "stale"
    assert fake_blob.puts == []
    rebuilt = update(fake_blob, rebuild=True)
    assert rebuilt.status == "shrink-refused"
    assert fake_blob.puts == []
    assert update(fake_blob, rebuild=True, force=True).status == "written"


def test_dry_run_writes_nothing(fake_blob: FakeBlob) -> None:
    seed_two_runs(fake_blob)
    report = update(fake_blob, dry_run=True)
    assert (report.status, report.rows, report.scan_state) == ("dry-run", 1, None)
    assert TABLE not in fake_blob.files


def test_unreadable_file_keeps_state(fake_blob: FakeBlob) -> None:
    seed_two_runs(fake_blob)
    missing = blob_archive.predictions_pathname(
        ts(at(5)), f"Match_Predictions_X{SUFFIX}"
    )
    add_run(fake_blob, at(5), {"X": missing}, {})
    report = update(fake_blob)
    assert report.unread == [missing]
    assert report.status == "written"
    assert report.scan_state is None
    assert not report.ok


def test_write_failure_is_soft(fake_blob: FakeBlob) -> None:
    seed_two_runs(fake_blob)
    fake_blob.fail_all_puts = blob_archive.vercel_blob.BlobError("down")
    report = update(fake_blob, attempts=2)
    assert report.status == "write-failed"
    assert (report.contents, report.scan_state, report.index_entry) == ([], None, None)
