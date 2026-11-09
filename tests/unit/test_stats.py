"""Unit tests for the dashboard statistics (pure computations)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from lib import blob_archive
from lib import stats as st
from lib.results_archive import match_key, season_of

BL = "Bundesliga - Germany"
PL = "Premier League - England"
NOW = datetime(2026, 11, 10, 3, 0, tzinfo=UTC)


def row(  # noqa: PLR0913
    provider: str,
    kickoff: datetime,
    goals: tuple[int, int],
    *,
    home: str = "Bayern",
    away: str = "Köln",
    league: str = BL,
    made: datetime | None = None,
    origin: str = "live",
    odds: tuple[float, float, float] | None = None,
) -> dict[str, str]:
    made = made or kickoff - timedelta(days=1)
    return {
        "match_key": match_key(league, kickoff, home, away),
        "league": league,
        "season": season_of(kickoff),
        "kickoff_utc": kickoff.strftime("%Y-%m-%dT%H:%MZ"),
        "home_team": home,
        "away_team": away,
        "provider": provider,
        "tip_home": str(goals[0]),
        "tip_away": str(goals[1]),
        "predicted_at": blob_archive.format_run_ts(made),
        "origin": origin,
        "tip_kickoff_utc": kickoff.strftime("%Y-%m-%dT%H:%MZ"),
        "live_runs": blob_archive.format_run_ts(made) if origin == "live" else "",
        "odds_home": str(odds[0]) if odds else "",
        "odds_draw": str(odds[1]) if odds else "",
        "odds_away": str(odds[2]) if odds else "",
        "odds_at": "",
        "odds_source": "logged" if odds else "",
    }


def result(
    kickoff: datetime,
    goals: tuple[int, int],
    home: str = "Bayern",
    away: str = "Köln",
    league: str = BL,
) -> dict[str, str]:
    return {
        "match_key": match_key(league, kickoff, home, away),
        "home_goals": str(goals[0]),
        "away_goals": str(goals[1]),
    }


def at(day: int, hour: int = 15) -> datetime:
    return datetime(2026, 10, day, hour, tzinfo=UTC)


# %% baselines


def test_odds_favourite_never_tips_a_draw() -> None:
    assert st.odds_favourite((1.5, 4.0, 6.0)) == (2, 1)
    assert st.odds_favourite((5.0, 4.0, 1.6)) == (0, 1)
    assert st.odds_favourite((2.8, 3.0, 2.8)) == (2, 1)  # tie: home
    assert st.odds_favourite(None) is None


def test_consensus_median_rounds_half_up() -> None:
    assert st.consensus([(2, 1), (1, 1), (3, 0)]) == (2, 1)
    # Even count: medians 1.5 and 0.5 round up
    assert st.consensus([(1, 0), (2, 1)]) == (2, 1)
    assert st.consensus([(0, 2), (1, 3), (1, 1), (0, 0)]) == (1, 2)
    assert st.consensus([(2, 1)]) is None


# %% join and scoring


def test_join_on_match_key_scores_missing_tips_zero() -> None:
    k1, k2, k3 = at(3), at(10), at(17)
    rows = [
        row("A", k1, (2, 1)),
        row("A", k2, (1, 1), home="X", away="Y"),
        row("A", k3, (0, 1), home="Z", away="W"),
        row("B", k1, (2, 0)),  # B missed k2, predicted k3
        row("B", k3, (0, 1), home="Z", away="W"),
        row("C", k3, (3, 3), home="Z", away="W", made=at(5)),  # joined late
    ]
    results = st.parse_results(
        [result(k1, (2, 1)), result(k2, (2, 2), "X", "Y"), result(k3, (1, 2), "Z", "W")]
    )
    entries = st.score_entries(st.parse_table(rows), results)
    scores = st.aggregate(entries)
    a, b, c = scores["A"], scores["B"], scores["C"]
    # A: exact (4), draw with wrong score (2), goal difference (3)
    assert (a.points, a.exact, a.diff, a.tendency, a.wrong) == (9, 1, 1, 1, 0)
    # B: tendency (2), missed k2 (0), goal difference (3)
    assert (b.offered, b.tipped, b.missed, b.points) == (3, 2, 1, 5)
    assert b.avg0 == 5 / 3
    # C is only offered fixtures after its first prediction (k2 missed, k3 wrong)
    assert (c.offered, c.missed, c.points, c.wrong) == (2, 1, 0, 1)
    assert scores[st.ALWAYS_HOME].offered == 3
    # k2 has a single tip: below the consensus minimum of two
    assert scores[st.CONSENSUS].tipped == 2


def test_exclusive_hit_type_shares_add_up() -> None:
    k = [at(d) for d in (1, 8, 15, 22)]
    teams = [("A", "B"), ("C", "D"), ("E", "F"), ("G", "H")]
    tips = [(2, 1), (2, 0), (1, 0), (0, 2)]
    actual = [(2, 1), (3, 1), (3, 0), (1, 0)]
    rows = [
        row("M", ko, t, home=h, away=a)
        for ko, t, (h, a) in zip(k, tips, teams, strict=True)
    ]
    rows.append(row("N", k[0], (0, 0), home="A", away="B", made=at(1, 0)))
    res = [result(ko, g, h, a) for ko, g, (h, a) in zip(k, actual, teams, strict=True)]
    data = st.build_stats(rows, res, active=["M", "N"], now=NOW)
    columns = data["meta"]["columns"]["leaderboard"]
    ids = {m["id"]: i for i, m in enumerate(data["models"])}
    board = {
        r[0]: dict(zip(columns, r, strict=True))
        for r in data["slices"]["2026/27|all"]["leaderboard"]
    }
    m = board[ids["M"]]
    assert (m["exact"], m["diff"], m["tendency"], m["wrong"], m["missed"]) == (
        1,
        1,
        1,
        1,
        0,
    )
    shares = [m[f"{h}_share"] for h in ("exact", "diff", "tendency", "wrong", "missed")]
    assert sum(shares) == 1.0
    assert m["ci_low"] < m["avg0"] < m["ci_high"]


def test_backfill_counts_by_default_and_is_flagged() -> None:
    k1, k2 = at(3), at(10)
    rows = [
        row("A", k1, (2, 1)),
        row("A", k2, (2, 1), home="X", away="Y"),
        row("L", k1, (2, 1), origin="backfill"),
        row("L", k2, (0, 0), home="X", away="Y"),
    ]
    res = [result(k1, (2, 1)), result(k2, (2, 1), "X", "Y")]
    tips = st.parse_table(rows)
    results = st.parse_results(res)
    assert st.aggregate(st.score_entries(tips, results))["L"].backfill == 1
    live = st.aggregate(st.score_entries(tips, results, include_backfill=False))
    assert (live["L"].offered, live["L"].points) == (1, 0)
    data = st.build_stats(rows, res, active=["A", "L"], now=NOW)
    ids = {m["id"]: i for i, m in enumerate(data["models"])}
    part = data["slices"]["2026/27|all"]
    assert [r[0] for r in part["live"]] == [ids["L"]]
    assert data["models"][ids["L"]]["backfill"]["tips"] == 1


def test_common_fixtures_comparison() -> None:
    k1, k2 = at(3), at(10)
    rows = [
        row("A", k1, (2, 1)),
        row("A", k2, (2, 1), home="X", away="Y"),
        row("B", k2, (1, 1), home="X", away="Y", made=at(9)),
    ]
    res = [result(k1, (2, 1)), result(k2, (1, 1), "X", "Y")]
    entries = st.score_entries(st.parse_table(rows), st.parse_results(res))
    common = st.common_entries(entries, {"A", "B"})
    assert {e.fixture.match_key for e in common} == {match_key(BL, k2, "X", "Y")}
    assert {e.model for e in common} == {"A", "B", *st.BASELINES}


def test_reliability_counts_runs_and_pre_kickoff_tips() -> None:
    r1, r2, r3 = at(1, 3), at(4, 3), at(8, 3)
    rows = [
        row("A", at(2), (1, 0), made=r1),
        row("A", at(5), (1, 0), home="X", away="Y", made=r2),
        row("A", at(9), (1, 0), home="Z", away="W", made=r3),
        row("B", at(2), (1, 0), made=r1),
        row("B", at(9), (1, 0), home="Z", away="W", made=r3),  # missed run r2
    ]
    coverage = st.reliability(st.parse_table(rows), NOW)
    b = coverage["B"]
    assert (b.runs, b.missed_runs, b.offered, b.predicted) == (3, 1, 3, 2)
    assert (b.past_fixtures, b.past_predicted) == (3, 2)


def test_matchdays_split_on_repeat_team_or_break() -> None:
    labels = st.cluster_matchdays(
        [(at(3), "A", "B"), (at(4), "C", "D"), (at(5), "A", "C"), (at(20), "E", "F")],
        BL,
    )
    assert len(set(labels.values())) == 3


# %% JSON


def season_rows(
    season_start: datetime, leagues: list[str], models: list[str]
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Return a full synthetic season: 34 matchdays of 9 fixtures per league."""
    rows, res = [], []
    for li, league in enumerate(leagues):
        for md in range(34):
            day = season_start + timedelta(days=7 * md, hours=li)
            for f in range(9):
                kickoff = day + timedelta(minutes=f)
                home, away = f"H{f}-{md}", f"A{f}-{md}"
                for mi, model in enumerate(models):
                    goals = ((mi + f) % 4, (md + mi) % 3)
                    rows.append(
                        row(
                            model,
                            kickoff,
                            goals,
                            home=home,
                            away=away,
                            league=league,
                            odds=(1.5 + f / 10, 4.0, 3.0),
                        )
                    )
                res.append(result(kickoff, (f % 3, md % 2), home, away, league))
    return rows, res


def test_json_shape_and_size_stay_compact() -> None:
    leagues = [BL, PL, "La Liga - Spain", "UEFA Champions League"]
    models = [f"Model-{i}" for i in range(7)]
    rows, res = season_rows(datetime(2025, 8, 15, 18, tzinfo=UTC), leagues, models)
    more, more_res = season_rows(datetime(2026, 8, 14, 18, tzinfo=UTC), leagues, models)
    data = st.build_stats(
        rows + more,
        res + more_res,
        active=models,
        now=datetime(2027, 5, 30, tzinfo=UTC),
    )
    body = st.to_json_bytes(data)
    assert len(body) < 100_000
    assert set(data) == {"version", "meta", "models", "slices", "recent"}
    assert len(data["slices"]) == 15
    part = data["slices"]["2026/27|" + BL]
    assert len(part["timeline"]["steps"]) == 34
    _, start, values = part["timeline"]["points"][0]
    assert start == 0
    assert len(values) == 34
    assert all(b >= a for a, b in pairwise(values))
    assert data["slices"]["all|all"]["timeline"]["step"] == "month"
    assert len(data["recent"]["leagues"][BL]) == st.RECENT_MATCHDAYS
    json.loads(body)
