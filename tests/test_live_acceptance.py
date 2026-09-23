"""Live READ-ONLY acceptance checks for Issue joeshoff/Hybrid-Athletic-Trainer#7 (AC3, AC4, AC6).

Opens the live DB only through ``db.open_ro_connection`` (mode=ro, query_only). Sessions are
picked by workout_pk, not position, so future workouts don't break these checks.

Known data (settled in AC1):
- Lower A = routine 65: workout 8 (2026-09-15), workout 11 (2026-09-22).
- Lower B = routine 59: workout 7 (2026-09-18); leg curl slot 487.
- Lower A slots: 476 Hack Squat, 544 Smith RDL, 539 Incline Leg Press, 498 Standing Leg Curl,
  541 Calf Press, 503 Cable Abduction, 446 Cable Pallof Press.

Not asserted: the 9/15 Pallof set-2 weight (ruling 5, pending Joe's check in the app).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from smartgym_mcp import db, queries

LOWER_A = 65
LOWER_B = 59
W_LA_0915 = 8
W_LA_0922 = 11
W_LB_0918 = 7

HACK, SMITH_RDL, INCLINE_LP, LEG_CURL_A, CALF, ABDUCTION, PALLOF = (
    476,
    544,
    539,
    498,
    541,
    503,
    446,
)
LEG_CURL_B = 487
LOWER_A_SLOTS = [HACK, SMITH_RDL, INCLINE_LP, LEG_CURL_A, CALF, ABDUCTION, PALLOF]

# Lower B's prescription rows rewritten on 9/22 (3 x 10 @ 35 lb); unused, never history.
LB_PRESCRIPTION_ROWS = {5971, 5966, 5977}

DEPTH = 50  # deep enough to include every workout so far


@pytest.fixture(scope="module")
def ro(live_cfg):
    conn = db.open_ro_connection(live_cfg.db_path)
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def lower_a(ro):
    return queries.get_routine(ro, LOWER_A, history_depth=DEPTH, weight_unit="lb")


@pytest.fixture(scope="module")
def lower_b(ro):
    return queries.get_routine(ro, LOWER_B, history_depth=DEPTH, weight_unit="lb")


def ex(detail, ue_pk):
    [e] = [e for e in detail.exercises if e.ue_pk == ue_pk]
    return e


def by_workout(entry):
    sessions = {s.workout_pk: s for s in entry.sessions}
    assert len(sessions) == len(entry.sessions), "one session per workout"
    return sessions


def wr(session):
    """(weight, reps) per set, in set_no order."""
    assert [s.set_no for s in session.sets] == sorted(s.set_no for s in session.sets)
    return [(s.weight, s.reps) for s in session.sets]


def routine_workouts(ro, routine_pk):
    return {
        r[0]
        for r in ro.execute(
            "SELECT DISTINCT h.ZWORKOUT FROM ZHISTORY h WHERE h.ZROUTINE = ? "
            "AND h.ZWORKOUT IS NOT NULL",
            (routine_pk,),
        )
    }


def workouts_logging_slot(ro, ue_pk, routine_pk):
    """Independent SQL: workouts of `routine_pk` with >=1 set of slot `ue_pk` linked."""
    return {
        r[0]
        for r in ro.execute(
            "SELECT DISTINCT w.Z_PK FROM Z_11SETSDONE s "
            "JOIN ZHISTORY h ON h.Z_PK = s.Z_11HISTORIES1 "
            "JOIN ZWORKOUT w ON w.Z_PK = h.ZWORKOUT "
            "JOIN ZVALUES v ON v.Z_PK = s.Z_21SETSDONE "
            "WHERE v.ZEXERCISE = ? AND h.ZROUTINE = ?",
            (ue_pk, routine_pk),
        )
    }


# --------------------------------------------------------------------------- #
# AC6: get_workout_detail(11) matches the app, set for set, in order
# --------------------------------------------------------------------------- #
AC6_EXPECTED = [
    (HACK, "hack squat", [(140.0, 8)] * 3),
    (SMITH_RDL, "romanian deadlift", [(140.0, 8)] * 3),
    (INCLINE_LP, "incline leg press", [(180.0, 10)] * 3),
    (LEG_CURL_A, "standing leg curl", [(35.0, 10)] * 3),
    (CALF, "calf press", [(180.0, 10)] * 3),
    (ABDUCTION, "cable abduction", [(10.0, 12), (15.0, 12)]),
    (PALLOF, "pallof", [(30.0, 10)] * 3),
]


def test_ac6_workout_11_detail_matches_app(ro):
    wd = queries.get_workout_detail(ro, W_LA_0922, weight_unit="lb")
    assert wd.workout_pk == W_LA_0922
    assert wd.routine_z_pk == LOWER_A
    assert wd.date == "2026-09-22"
    assert wd.weight_unit == "lb"
    assert [e.ue_pk for e in wd.exercises] == [ue for ue, _, _ in AC6_EXPECTED]
    for e, (ue, name, sets) in zip(wd.exercises, AC6_EXPECTED, strict=True):
        assert name in e.exercise_name.lower(), (ue, e.exercise_name)
        assert [(s.weight, s.reps) for s in e.sets] == sets, (ue, e.exercise_name)
        assert [s.set_no for s in e.sets] == list(range(len(sets)))
        assert e.slot_removed is False
    assert wd.warnings == [] or all(isinstance(w, str) for w in wd.warnings)


def test_ac6_workout_11_metadata_matches_history(ro):
    wd = queries.get_workout_detail(ro, W_LA_0922, weight_unit="lb")
    hist = queries.get_workout_history(
        ro, date_from="2026-09-22", date_to="2026-09-22", limit=100
    )
    [hs] = [s for s in hist.sessions if s.workout_pk == W_LA_0922]
    for field in ("date", "duration_min", "calories", "avg_hr", "max_hr"):
        assert getattr(wd, field) == getattr(hs, field), field
    assert wd.duration_min == 54


# --------------------------------------------------------------------------- #
# AC3: Lower A history, including the repeated session
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("ue_pk", "expected"),
    [
        (HACK, [(140.0, 8)] * 3),
        (SMITH_RDL, [(140.0, 8)] * 3),
        (INCLINE_LP, [(180.0, 10)] * 3),
    ],
)
def test_ac3_repeated_exercises_have_two_identical_sessions(lower_a, ue_pk, expected):
    s = by_workout(ex(lower_a, ue_pk))
    assert W_LA_0922 in s and W_LA_0915 in s
    assert s[W_LA_0922].date == "2026-09-22"
    assert s[W_LA_0915].date == "2026-09-15"
    assert wr(s[W_LA_0922]) == expected
    assert wr(s[W_LA_0915]) == expected
    pks = [x.workout_pk for x in ex(lower_a, ue_pk).sessions]
    assert pks.index(W_LA_0922) < pks.index(W_LA_0915), "newest first"


def test_ac3_leg_curl_sessions_not_split(lower_a):
    s = by_workout(ex(lower_a, LEG_CURL_A))
    assert wr(s[W_LA_0922]) == [(35.0, 10)] * 3
    assert wr(s[W_LA_0915]) == [(25.0, 10), (35.0, 10), (35.0, 10)]
    assert s[W_LA_0922].date == "2026-09-22"
    assert s[W_LA_0915].date == "2026-09-15"


def test_ac3_calf_press_logged_only_on_0922(lower_a):
    """Ruling 3: 9/15 had no calf press logged."""
    s = by_workout(ex(lower_a, CALF))
    assert wr(s[W_LA_0922]) == [(180.0, 10)] * 3
    assert W_LA_0915 not in s


def test_ac3_no_lower_a_session_on_0921(ro, lower_a):
    """No Lower A workout exists on 9/21, so no Lower A session is dated 9/21."""
    la_dates = {
        r[0]
        for r in ro.execute(
            f"SELECT date(w.ZSTARTDATE {db.EPOCH_SQL}) FROM ZWORKOUT w "
            "JOIN ZHISTORY h ON h.ZWORKOUT = w.Z_PK WHERE h.ZROUTINE = ?",
            (LOWER_A,),
        )
    }
    assert "2026-09-21" not in la_dates
    for e in lower_a.exercises:
        assert all(s.date != "2026-09-21" for s in e.sessions), e.exercise_name
    assert all(s.date != "2026-09-21" for s in ex(lower_a, PALLOF).sessions)


def test_ac3_session_count_equals_lower_a_workouts_logged(ro, lower_a):
    la_workouts = routine_workouts(ro, LOWER_A)
    assert {W_LA_0915, W_LA_0922} <= la_workouts
    for ue in LOWER_A_SLOTS:
        e = ex(lower_a, ue)
        expected = workouts_logging_slot(ro, ue, LOWER_A)
        got = [s.workout_pk for s in e.sessions]
        assert len(got) == len(expected), (ue, e.exercise_name)
        assert set(got) == expected, (ue, e.exercise_name)
        assert set(got) <= la_workouts


def test_ac3_sessions_dated_by_workout_start(ro, lower_a):
    starts = dict(
        ro.execute(f"SELECT Z_PK, date(ZSTARTDATE {db.EPOCH_SQL}) FROM ZWORKOUT").fetchall()
    )
    for e in lower_a.exercises:
        for s in e.sessions:
            assert s.date == starts[s.workout_pk], (e.exercise_name, s.workout_pk)


# --------------------------------------------------------------------------- #
# AC4: no cross-routine leakage (shared Standing Leg Curl: 487 Lower B vs 498 Lower A)
# --------------------------------------------------------------------------- #
def test_ac4_lower_b_leg_curl_workout_7(lower_b):
    s = by_workout(ex(lower_b, LEG_CURL_B))
    assert s[W_LB_0918].date == "2026-09-18"
    assert wr(s[W_LB_0918]) == [(37.5, 8)] * 4


def test_ac4_slot_487_only_under_lower_b(ro, lower_a, lower_b):
    assert LEG_CURL_B not in {e.ue_pk for e in lower_a.exercises}
    lb_workouts = routine_workouts(ro, LOWER_B)
    la_workouts = routine_workouts(ro, LOWER_A)
    assert {s.workout_pk for s in ex(lower_b, LEG_CURL_B).sessions} <= lb_workouts

    for row in queries.logged_sets(ro, ue_pks=[LEG_CURL_B]):
        assert row.history_routine_pk == LOWER_B
        assert row.workout_pk not in la_workouts

    la_ls = queries.logged_sets(ro, workout_pks=sorted(la_workouts))
    assert LEG_CURL_B not in {r.ue_pk for r in la_ls}
    for w in (W_LA_0915, W_LA_0922):
        wd = queries.get_workout_detail(ro, w, weight_unit="lb")
        assert LEG_CURL_B not in {e.ue_pk for e in wd.exercises}


def test_ac4_lower_b_prescription_rows_not_history(ro, lower_b):
    """Ruling 4: slot 487's 3 x 10 @ 35 prescription rows (rewritten 9/22) are not history.
    A future Lower B workout may legitimately link them, so only workouts up to 9/23 count."""
    cutoff = db.datetime_to_coredata(datetime(2026, 9, 24))
    for row in queries.logged_sets(ro, ue_pks=[LEG_CURL_B]):
        if row.workout_start < cutoff:
            assert row.value_pk not in LB_PRESCRIPTION_ROWS, row

    s = by_workout(ex(lower_b, LEG_CURL_B))
    assert (35.0, 10) not in wr(s[W_LB_0918])
    lb_dates = {
        r[0]
        for r in ro.execute(
            f"SELECT date(w.ZSTARTDATE {db.EPOCH_SQL}) FROM ZWORKOUT w "
            "JOIN ZHISTORY h ON h.ZWORKOUT = w.Z_PK WHERE h.ZROUTINE = ?",
            (LOWER_B,),
        )
    }
    assert "2026-09-22" not in lb_dates
    assert all(sess.date != "2026-09-22" for sess in s.values())
