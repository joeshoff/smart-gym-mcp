"""Workout-scoped set history + get_workout_detail (Issue joeshoff/Hybrid-Athletic-Trainer#7).

AC7 fixture regression tests, amended by the PO rulings on the Issue. Written from the Issue
and the interface contract, before and without reading the implementation.

Every test builds its own rows inside the ``temp_db_cfg`` temp copy (never the live DB), using
Z_PKs well above the copy's current maxima, and asserts only on those rows.

Schema facts the fixtures encode (settled in AC1):
- a logged set is a ZVALUES row linked to a ZHISTORY through Z_11SETSDONE;
- ZHISTORY.ZWORKOUT -> ZWORKOUT (inverse ZWORKOUT.ZHISTORY);
- unlinked ZVALUES rows are the routine's prescription/template, never history;
- linked rows are history even when soft-deleted; one row may be linked to several workouts.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from datetime import date, datetime, timedelta

import pytest

from smartgym_mcp import db, queries

LB = 0.45359237

# Core Data entity numbers for Z_ENT (from Z_PRIMARYKEY on the live schema).
ENT_HISTORY = 11
ENT_ROUTINE = 15
ENT_UNIQEXERCISE = 20
ENT_VALUES = 21
ENT_WORKOUT = 22


def cd(dt: datetime) -> float:
    """Naive local datetime -> Core Data seconds."""
    return db.datetime_to_coredata(dt)


def cd_day(d: date) -> float:
    """Local midnight of a day -> Core Data seconds (how the app stamps ZDATEADDED)."""
    return db.datetime_to_coredata(datetime(d.year, d.month, d.day))


def kg(lb: float) -> float:
    """Pounds -> kg the way the app stores it (4 dp)."""
    return round(lb * LB, 4)


class Fixture:
    """Writes self-contained routine/slot/set/workout rows into a temp DB copy."""

    def __init__(self, db_path) -> None:
        self.conn = sqlite3.connect(db_path)
        self._next: dict[str, int] = {}
        for table in (
            "ZROUTINE",
            "ZUNIQEXERCISE",
            "ZVALUES",
            "ZHISTORY",
            "ZWORKOUT",
        ):
            (mx,) = self.conn.execute(f"SELECT COALESCE(MAX(Z_PK), 0) FROM {table}").fetchone()
            self._next[table] = mx + 1000
        rows = self.conn.execute(
            "SELECT Z_PK, ZNAME FROM ZEXERCISE WHERE ZNAME IS NOT NULL AND ZNAME <> '' "
            "ORDER BY Z_PK LIMIT 3"
        ).fetchall()
        assert len(rows) == 3, "catalog copy should have exercises"
        self.catalog: list[tuple[int, str]] = [(r[0], r[1]) for r in rows]

    def _pk(self, table: str) -> int:
        pk = self._next[table]
        self._next[table] += 1
        return pk

    def routine(self, name: str) -> int:
        pk = self._pk("ZROUTINE")
        self.conn.execute(
            "INSERT INTO ZROUTINE (Z_PK, Z_ENT, Z_OPT, ZHASSYNCED, ZHIDDEN, ZISTEMP, ZNAME, "
            "ZDATECREATED) VALUES (?, ?, 1, 1, 0, 0, ?, ?)",
            (pk, ENT_ROUTINE, name, cd(datetime(2026, 7, 1, 12, 0))),
        )
        return pk

    def slot(
        self,
        routine_pk: int,
        index: int,
        exercise_pk: int | None = None,
        *,
        removed: bool = False,
    ) -> int:
        pk = self._pk("ZUNIQEXERCISE")
        removed_at = cd(datetime(2026, 8, 20, 12, 0)) if removed else None
        self.conn.execute(
            "INSERT INTO ZUNIQEXERCISE (Z_PK, Z_ENT, Z_OPT, ZINDEX, ZLISTGROUP, ZMODE, ZPAUSE, "
            "ZEXERCISE, ZROUTINE, ZDATEADDED, ZDATEREMOVED, ZPRECISEDATEADDED, "
            "ZPRECISEDATEREMOVED) VALUES (?, ?, 1, ?, 0, 0, 60, ?, ?, ?, ?, ?, ?)",
            (
                pk,
                ENT_UNIQEXERCISE,
                index,
                exercise_pk if exercise_pk is not None else self.catalog[0][0],
                routine_pk,
                cd_day(date(2026, 7, 1)),
                removed_at,
                cd(datetime(2026, 7, 1, 12, 0)),
                removed_at,
            ),
        )
        return pk

    def value(
        self,
        ue_pk: int,
        set_no: int,
        reps: int,
        weight_kg: float,
        *,
        added: date,
        removed: datetime | None = None,
        date_logged: datetime | None = None,
    ) -> int:
        pk = self._pk("ZVALUES")
        self.conn.execute(
            "INSERT INTO ZVALUES (Z_PK, Z_ENT, Z_OPT, ZINDEX, ZTYPE, ZEXERCISE, ZDATEADDED, "
            "ZDATELOGGED, ZDATEREMOVED, ZFIRSTVALUE, ZPRECISEDATEADDED, ZPRECISEDATELOGGED, "
            "ZPRECISEDATEREMOVED, ZSECONDVALUE, ZTHIRDVALUE) "
            "VALUES (?, ?, 1, ?, 0, ?, ?, ?, ?, 1.0, ?, ?, ?, ?, ?)",
            (
                pk,
                ENT_VALUES,
                set_no,
                ue_pk,
                cd_day(added),
                cd(date_logged) if date_logged else None,
                cd_day(removed.date()) if removed else None,
                cd(datetime(added.year, added.month, added.day, 12, 0)),
                cd(date_logged) if date_logged else None,
                cd(removed) if removed else None,
                float(reps),
                weight_kg,
            ),
        )
        return pk

    def history(self, routine_pk: int, workout_pk: int | None, start: datetime) -> int:
        pk = self._pk("ZHISTORY")
        self.conn.execute(
            "INSERT INTO ZHISTORY (Z_PK, Z_ENT, Z_OPT, ZHASFETCHED, ZROUTINE, ZWORKOUT, "
            "ZDATEADDED, ZPRECISEDATEADDED) VALUES (?, ?, 1, 1, ?, ?, ?, ?)",
            (
                pk,
                ENT_HISTORY,
                routine_pk,
                workout_pk,
                cd_day(start.date()),
                cd(start + timedelta(minutes=3)),
            ),
        )
        return pk

    def link(self, history_pk: int, value_pks: Sequence[int]) -> None:
        self.conn.executemany(
            "INSERT INTO Z_11SETSDONE (Z_11HISTORIES1, Z_21SETSDONE) VALUES (?, ?)",
            [(history_pk, v) for v in value_pks],
        )
        ue_pks = (
            {
                r[0]
                for r in self.conn.execute(
                    f"SELECT DISTINCT ZEXERCISE FROM ZVALUES WHERE Z_PK IN "
                    f"({','.join('?' * len(value_pks))})",
                    list(value_pks),
                )
            }
            if value_pks
            else set()
        )
        self.conn.executemany(
            "INSERT OR IGNORE INTO Z_11EXERCISES (Z_11HISTORIES, Z_20EXERCISES) VALUES (?, ?)",
            [(history_pk, ue) for ue in ue_pks],
        )

    def workout(
        self,
        routine_pk: int,
        start: datetime,
        value_pks: Sequence[int] = (),
        *,
        duration_s: int = 3125,
        calories: float = 233.7,
        avg_hr: float = 99.6,
        max_hr: float = 141.0,
    ) -> int:
        """A workout plus its (main) history, with `value_pks` linked as sets done."""
        w_pk = self._pk("ZWORKOUT")
        h_pk = self.history(routine_pk, w_pk, start)
        self.conn.execute(
            "INSERT INTO ZWORKOUT (Z_PK, Z_ENT, Z_OPT, ZACTIVITYTYPE, ZDURATION, ZHISTORY, "
            "ZAVERAGEHEARTRATE, ZCALORIES, ZDISTANCE, ZENDDATE, ZMAXHEARTRATE, ZMINHEARTRATE, "
            "ZSTARTDATE) VALUES (?, ?, 1, 0, ?, ?, ?, ?, 0.0, ?, ?, 70.0, ?)",
            (
                w_pk,
                ENT_WORKOUT,
                duration_s,
                h_pk,
                avg_hr,
                calories,
                cd(start) + duration_s,
                max_hr,
                cd(start),
            ),
        )
        self.link(h_pk, value_pks)
        self.last_history_pk = h_pk
        return w_pk

    def commit(self) -> None:
        self.conn.commit()
        self.conn.close()


@pytest.fixture
def fx(temp_db_cfg) -> Fixture:
    return Fixture(temp_db_cfg.db_path)


@pytest.fixture
def open_ro(temp_db_cfg) -> Iterator:
    """Call after fx.commit(): returns a read-only connection to the temp copy."""
    conns: list[sqlite3.Connection] = []

    def _open() -> sqlite3.Connection:
        conn = db.open_ro_connection(temp_db_cfg.db_path)
        conns.append(conn)
        return conn

    yield _open
    for c in conns:
        c.close()


def exercise(detail, ue_pk: int):
    matches = [e for e in detail.exercises if e.ue_pk == ue_pk]
    assert len(matches) == 1, f"ue_pk {ue_pk} not listed exactly once"
    return matches[0]


def sets_of(session) -> list[tuple[int, int, float]]:
    return [(s.set_no, s.reps, s.weight_kg) for s in session.sets]


# --------------------------------------------------------------------------- #
# get_routine: history is workout-scoped (AC2, AC7)
# --------------------------------------------------------------------------- #
def test_template_sets_never_appear_as_history(fx, open_ro):
    """AC7 'template sets excluded': unlinked rows are prescription, even when live
    (and even if ZDATELOGGED happens to be set: 'logged' means linked)."""
    r = fx.routine("ZZ-QA template")
    used = fx.slot(r, 0)
    unused = fx.slot(r, 1)
    d = date(2026, 8, 3)
    done = [fx.value(used, i, 8, 60.0, added=d) for i in range(3)]
    # live prescription rows on both slots, never linked
    for i in range(3):
        fx.value(used, i, 10, 99.9, added=date(2026, 8, 4))
        fx.value(unused, i, 12, 88.8, added=d, date_logged=datetime(2026, 8, 3, 18, 30))
    w = fx.workout(r, datetime(2026, 8, 3, 18, 0), done)
    fx.commit()

    detail = queries.get_routine(open_ro(), r, history_depth=10)
    e_used = exercise(detail, used)
    assert [s.workout_pk for s in e_used.sessions] == [w]
    assert sets_of(e_used.sessions[0]) == [(0, 8, 60.0), (1, 8, 60.0), (2, 8, 60.0)]

    e_unused = exercise(detail, unused)
    assert e_unused.sessions == []
    assert e_unused.top_set is None
    assert e_unused.total_volume == 0


def test_identical_workouts_shared_rows_are_two_sessions_newest_first(fx, open_ro):
    """AC7 the 9/15-9/22 case: the SAME ZVALUES rows linked to both histories."""
    r = fx.routine("ZZ-QA repeat")
    ue = fx.slot(r, 0)
    rows = [fx.value(ue, i, 8, kg(140), added=date(2026, 8, 3)) for i in range(3)]
    w1 = fx.workout(r, datetime(2026, 8, 3, 21, 49), rows)
    w2 = fx.workout(r, datetime(2026, 8, 10, 20, 42), rows)
    fx.commit()

    e = exercise(queries.get_routine(open_ro(), r, history_depth=5), ue)
    assert [s.workout_pk for s in e.sessions] == [w2, w1]
    assert [s.date for s in e.sessions] == ["2026-08-10", "2026-08-03"]
    for sess in e.sessions:
        assert sets_of(sess) == [(i, 8, kg(140)) for i in range(3)]


def test_one_changed_set_gives_two_complete_sessions(fx, open_ro):
    """AC7 + ruling 1: old row soft-deleted but still linked to workout 1, new row linked to
    workout 2, unchanged rows linked to both. Neither session is split."""
    r = fx.routine("ZZ-QA one changed")
    ue = fx.slot(r, 0)
    d1, d2 = date(2026, 8, 3), date(2026, 8, 10)
    old0 = fx.value(ue, 0, 10, kg(25), added=d1, removed=datetime(2026, 8, 10, 20, 46, 10))
    s1 = fx.value(ue, 1, 10, kg(35), added=d1)
    s2 = fx.value(ue, 2, 10, kg(35), added=d1)
    new0 = fx.value(ue, 0, 10, kg(35), added=d2)
    w1 = fx.workout(r, datetime(2026, 8, 3, 21, 49), [old0, s1, s2])
    w2 = fx.workout(r, datetime(2026, 8, 10, 20, 42), [new0, s1, s2])
    fx.commit()

    e = exercise(queries.get_routine(open_ro(), r, history_depth=5), ue)
    assert [s.workout_pk for s in e.sessions] == [w2, w1]
    newest, oldest = e.sessions
    assert newest.date == "2026-08-10"
    assert sets_of(newest) == [(0, 10, kg(35)), (1, 10, kg(35)), (2, 10, kg(35))]
    assert oldest.date == "2026-08-03"
    # the soft-deleted linked row is real history for workout 1
    assert sets_of(oldest) == [(0, 10, kg(25)), (1, 10, kg(35)), (2, 10, kg(35))]


def test_soft_deleted_linked_row_appears_in_workout_detail(fx, open_ro):
    """Ruling 1: linked + soft-deleted is history in get_workout_detail too."""
    r = fx.routine("ZZ-QA softdel detail")
    ue = fx.slot(r, 0)
    old0 = fx.value(
        ue, 0, 10, kg(25), added=date(2026, 8, 3), removed=datetime(2026, 8, 10, 20, 46)
    )
    s1 = fx.value(ue, 1, 10, kg(35), added=date(2026, 8, 3))
    w1 = fx.workout(r, datetime(2026, 8, 3, 21, 49), [old0, s1])
    fx.commit()

    wd = queries.get_workout_detail(open_ro(), w1, weight_unit="lb")
    assert [(s.set_no, s.reps, s.weight) for s in exercise(wd, ue).sets] == [
        (0, 10, 25.0),
        (1, 10, 35.0),
    ]


def test_set_added_on_other_day_is_grouped_and_dated_by_its_workout(fx, open_ro):
    """AC7: ZDATEADDED day != workout day; the session is the workout's, dated by the
    workout's local start date."""
    r = fx.routine("ZZ-QA dateadded")
    ue = fx.slot(r, 0)
    early = fx.value(ue, 0, 8, 60.0, added=date(2026, 8, 1))
    same = fx.value(ue, 1, 8, 60.0, added=date(2026, 8, 5))
    late = fx.value(ue, 2, 8, 60.0, added=date(2026, 8, 6))
    w = fx.workout(r, datetime(2026, 8, 5, 18, 0), [late, early, same])
    fx.commit()

    e = exercise(queries.get_routine(open_ro(), r, history_depth=5), ue)
    assert len(e.sessions) == 1
    sess = e.sessions[0]
    assert sess.workout_pk == w
    assert sess.date == "2026-08-05"
    assert [s.set_no for s in sess.sets] == [0, 1, 2]


def test_two_workouts_same_day_stay_separate(fx, open_ro):
    """AC7: same calendar day, two workouts, two sessions (newest first), same date."""
    r = fx.routine("ZZ-QA same day")
    ue = fx.slot(r, 0)
    d = date(2026, 8, 7)
    am = [fx.value(ue, i, 5, 80.0, added=d) for i in range(2)]
    pm = [fx.value(ue, i, 12, 40.0, added=d) for i in range(3)]
    w_am = fx.workout(r, datetime(2026, 8, 7, 7, 0), am)
    w_pm = fx.workout(r, datetime(2026, 8, 7, 18, 30), pm)
    fx.commit()

    e = exercise(queries.get_routine(open_ro(), r, history_depth=5), ue)
    assert [s.workout_pk for s in e.sessions] == [w_pm, w_am]
    assert [s.date for s in e.sessions] == ["2026-08-07", "2026-08-07"]
    assert sets_of(e.sessions[0]) == [(i, 12, 40.0) for i in range(3)]
    assert sets_of(e.sessions[1]) == [(i, 5, 80.0) for i in range(2)]


def test_history_depth_counts_workouts_not_days(fx, open_ro):
    """AC2: history_depth counts workouts; a same-day pair uses two slots of depth."""
    r = fx.routine("ZZ-QA depth")
    ue = fx.slot(r, 0)
    starts = [
        datetime(2026, 8, 1, 18, 0),
        datetime(2026, 8, 4, 7, 0),
        datetime(2026, 8, 4, 19, 0),
        datetime(2026, 8, 8, 18, 0),
    ]
    rows = [fx.value(ue, 0, 8, 60.0, added=date(2026, 8, 1))]  # one row reused everywhere
    wks = [fx.workout(r, s, rows) for s in starts]
    fx.commit()
    conn = open_ro()

    newest_first = list(reversed(wks))
    for depth in (1, 2, 3, 4):
        e = exercise(queries.get_routine(conn, r, history_depth=depth), ue)
        assert [s.workout_pk for s in e.sessions] == newest_first[:depth]
    e = exercise(queries.get_routine(conn, r, history_depth=10), ue)
    assert [s.workout_pk for s in e.sessions] == newest_first


def test_several_histories_of_one_workout_are_one_session(fx, open_ro):
    """Contract: several ZHISTORY rows may share one workout; they form one session."""
    r = fx.routine("ZZ-QA multi history")
    ue = fx.slot(r, 0)
    start = datetime(2026, 8, 12, 18, 0)
    a = fx.value(ue, 0, 8, 60.0, added=date(2026, 8, 12))
    b = fx.value(ue, 1, 8, 62.5, added=date(2026, 8, 12))
    w = fx.workout(r, start, [a])
    extra_h = fx.history(r, w, start + timedelta(minutes=30))
    fx.link(extra_h, [b])
    fx.commit()

    e = exercise(queries.get_routine(open_ro(), r, history_depth=5), ue)
    assert [s.workout_pk for s in e.sessions] == [w]
    assert sets_of(e.sessions[0]) == [(0, 8, 60.0), (1, 8, 62.5)]


def test_shared_catalog_exercise_no_cross_routine_leakage(fx, open_ro):
    """AC4 + ruling 4: routine A and B share a catalog exercise. B's logged set never shows
    under A, and B's unused prescription rows never show as history under B."""
    catalog_pk = fx.catalog[1][0]
    ra = fx.routine("ZZ-QA shared A")
    rb = fx.routine("ZZ-QA shared B")
    ue_a = fx.slot(ra, 0, catalog_pk)
    ue_b = fx.slot(rb, 0, catalog_pk)
    a_rows = [fx.value(ue_a, i, 10, 15.8758, added=date(2026, 8, 3)) for i in range(3)]
    b_rows = [fx.value(ue_b, i, 8, 17.0097, added=date(2026, 8, 5)) for i in range(4)]
    # B's prescription rewritten later (the app's cross-routine prefill); never linked
    for i in range(3):
        fx.value(ue_b, i, 10, 15.8758, added=date(2026, 8, 10))
    wa = fx.workout(ra, datetime(2026, 8, 3, 18, 0), a_rows)
    wb = fx.workout(rb, datetime(2026, 8, 5, 18, 0), b_rows)
    fx.commit()
    conn = open_ro()

    a = queries.get_routine(conn, ra, history_depth=10)
    assert [e.ue_pk for e in a.exercises] == [ue_a]
    assert [s.workout_pk for s in exercise(a, ue_a).sessions] == [wa]
    assert sets_of(exercise(a, ue_a).sessions[0]) == [(i, 10, 15.8758) for i in range(3)]

    b = queries.get_routine(conn, rb, history_depth=10)
    assert [e.ue_pk for e in b.exercises] == [ue_b]
    b_sessions = exercise(b, ue_b).sessions
    assert [s.workout_pk for s in b_sessions] == [wb]
    assert sets_of(b_sessions[0]) == [(i, 8, 17.0097) for i in range(4)]

    assert {s.ue_pk for s in queries.logged_sets(conn, workout_pks=[wa])} == {ue_a}
    assert {s.ue_pk for s in queries.logged_sets(conn, workout_pks=[wb])} == {ue_b}


def test_slot_set_linked_to_another_routines_history_is_not_history_of_the_slot(fx, open_ro):
    """Contract: get_routine keeps only rows whose history routine == slot routine."""
    ra = fx.routine("ZZ-QA leak A")
    rb = fx.routine("ZZ-QA leak B")
    ue_a = fx.slot(ra, 0)
    ue_b = fx.slot(rb, 0)
    a_row = fx.value(ue_a, 0, 8, 60.0, added=date(2026, 8, 3))
    stray = fx.value(ue_b, 0, 8, 70.0, added=date(2026, 8, 3))
    wa = fx.workout(ra, datetime(2026, 8, 3, 18, 0), [a_row, stray])
    fx.commit()
    conn = open_ro()

    assert exercise(queries.get_routine(conn, rb, history_depth=10), ue_b).sessions == []
    a = queries.get_routine(conn, ra, history_depth=10)
    assert [s.workout_pk for s in exercise(a, ue_a).sessions] == [wa]
    assert sets_of(exercise(a, ue_a).sessions[0]) == [(0, 8, 60.0)]


def test_top_set_and_total_volume_come_from_newest_workout(fx, open_ro):
    """AC2: top_set/total_volume from the latest workout, not the latest day or best ever."""
    r = fx.routine("ZZ-QA top set")
    ue = fx.slot(r, 0)
    old = [fx.value(ue, 0, 5, 100.0, added=date(2026, 8, 1))]
    new = [
        fx.value(ue, 0, 10, 60.0, added=date(2026, 8, 1)),  # created earlier than `old`'s day
        fx.value(ue, 1, 8, 70.0, added=date(2026, 8, 1)),
    ]
    fx.workout(r, datetime(2026, 8, 2, 18, 0), old)
    w_new = fx.workout(r, datetime(2026, 8, 9, 18, 0), new)
    fx.commit()
    conn = open_ro()

    e = exercise(queries.get_routine(conn, r, history_depth=5), ue)
    assert e.sessions[0].workout_pk == w_new
    assert e.top_set is not None
    assert e.top_set in e.sessions[0].sets
    assert e.top_set.weight_kg != 100.0
    assert e.total_volume == pytest.approx(10 * 60.0 + 8 * 70.0)

    # total_volume is in the response's weight_unit
    e_lb = exercise(queries.get_routine(conn, r, history_depth=5, weight_unit="lb"), ue)
    assert e_lb.total_volume == pytest.approx(
        10 * round(60.0 / LB, 1) + 8 * round(70.0 / LB, 1), abs=0.5
    )


def test_get_routine_names_weight_unit_and_converts(fx, open_ro):
    """AC5 units applied to get_routine: weight in the named unit, weight_kg raw."""
    r = fx.routine("ZZ-QA routine units")
    ue = fx.slot(r, 0)
    rows = [fx.value(ue, 0, 8, kg(140), added=date(2026, 8, 3))]
    fx.workout(r, datetime(2026, 8, 3, 18, 0), rows)
    fx.commit()
    conn = open_ro()

    d_lb = queries.get_routine(conn, r, history_depth=5, weight_unit="lb")
    assert d_lb.weight_unit == "lb"
    s = exercise(d_lb, ue).sessions[0].sets[0]
    assert s.weight == 140.0 and s.weight_kg == kg(140)

    d_kg = queries.get_routine(conn, r, history_depth=5, weight_unit="kg")
    assert d_kg.weight_unit == "kg"
    assert exercise(d_kg, ue).sessions[0].sets[0].weight == round(kg(140), 1)


# --------------------------------------------------------------------------- #
# get_workout_detail (AC5, AC7)
# --------------------------------------------------------------------------- #
def test_workout_detail_unknown_workout_raises(fx, open_ro):
    fx.commit()
    conn = open_ro()
    (mx,) = conn.execute("SELECT MAX(Z_PK) FROM ZWORKOUT").fetchone()
    with pytest.raises(queries.WorkoutNotFound) as exc:
        queries.get_workout_detail(conn, mx + 99_999)
    assert isinstance(exc.value, ValueError)
    assert "smartgym_get_workout_history" in str(exc.value)


def test_workout_detail_no_linked_sets_is_empty_with_warning(fx, open_ro):
    r = fx.routine("ZZ-QA empty workout")
    ue = fx.slot(r, 0)
    fx.value(ue, 0, 10, 50.0, added=date(2026, 8, 3))  # prescription only
    w = fx.workout(r, datetime(2026, 8, 3, 18, 0), [])
    fx.commit()

    wd = queries.get_workout_detail(open_ro(), w)
    assert wd.workout_pk == w
    assert wd.exercises == []
    assert wd.warnings and all(isinstance(m, str) and m for m in wd.warnings)


def test_workout_detail_orders_exercises_by_slot_index_and_sets_by_set_no(fx, open_ro):
    r = fx.routine("ZZ-QA order")
    ue2 = fx.slot(r, 2, fx.catalog[2][0])
    ue0 = fx.slot(r, 0, fx.catalog[0][0])
    ue1 = fx.slot(r, 1, fx.catalog[1][0])
    d = date(2026, 8, 3)
    rows = []
    for ue in (ue2, ue0, ue1):
        for set_no in (2, 0, 1):  # inserted out of order
            rows.append(fx.value(ue, set_no, 10 + set_no, 20.0 + set_no, added=d))
    w = fx.workout(r, datetime(2026, 8, 3, 18, 0), rows)
    fx.commit()

    wd = queries.get_workout_detail(open_ro(), w, weight_unit="kg")
    assert [e.ue_pk for e in wd.exercises] == [ue0, ue1, ue2]
    assert [e.index for e in wd.exercises] == [0, 1, 2]
    names = dict(fx.catalog)
    for e, cat in zip(wd.exercises, [c for c, _ in fx.catalog], strict=True):
        assert e.exercise_z_pk == cat
        assert e.exercise_name == names[cat]
        assert e.slot_removed is False
        assert [(s.set_no, s.reps, s.weight_kg) for s in e.sets] == [
            (0, 10, 20.0),
            (1, 11, 21.0),
            (2, 12, 22.0),
        ]


def test_workout_detail_weight_conversion_lb_and_kg(fx, open_ro):
    """AC5: weight = round(kg / 0.45359237, 1) for lb, round(kg, 1) for kg; unit named."""
    r = fx.routine("ZZ-QA units")
    ue = fx.slot(r, 0)
    stored = [kg(140), kg(35), kg(37.5), 20.0, 11.3398]
    rows = [fx.value(ue, i, 8, w, added=date(2026, 8, 3)) for i, w in enumerate(stored)]
    w = fx.workout(r, datetime(2026, 8, 3, 18, 0), rows)
    fx.commit()
    conn = open_ro()

    lb = queries.get_workout_detail(conn, w, weight_unit="lb")
    assert lb.weight_unit == "lb"
    got = exercise(lb, ue).sets
    assert [s.weight_kg for s in got] == stored
    assert [s.weight for s in got] == [round(x / LB, 1) for x in stored]
    assert [s.weight for s in got][:3] == [140.0, 35.0, 37.5]
    assert got[3].weight == 44.1
    assert got[4].weight == 25.0

    kgd = queries.get_workout_detail(conn, w, weight_unit="kg")
    assert kgd.weight_unit == "kg"
    assert [s.weight for s in exercise(kgd, ue).sets] == [round(x, 1) for x in stored]


def test_workout_detail_metadata_matches_workout_history(fx, open_ro):
    """AC5: same derivation as get_workout_history, so the two never disagree."""
    r = fx.routine("ZZ-QA metadata")
    ue = fx.slot(r, 0)
    rows = [fx.value(ue, 0, 8, 60.0, added=date(2026, 8, 3))]
    start = datetime(2026, 8, 3, 20, 42, 48)
    w = fx.workout(
        r, start, rows, duration_s=3269, calories=233.25, avg_hr=99.19, max_hr=123.0
    )
    fx.commit()
    conn = open_ro()

    wd = queries.get_workout_detail(conn, w)
    hist = queries.get_workout_history(
        conn, date_from="2026-08-01", date_to="2026-08-05", limit=100
    )
    [hs] = [s for s in hist.sessions if s.workout_pk == w]

    assert wd.routine_z_pk == r
    assert wd.routine_name == "ZZ-QA metadata"
    assert wd.start_local == "2026-08-03 20:42:48"
    assert wd.date == "2026-08-03"
    for field in ("date", "duration_min", "calories", "avg_hr", "max_hr"):
        assert getattr(wd, field) == getattr(hs, field), field
    assert wd.duration_min == 3269 // 60
    assert hs.routine == wd.routine_name


def test_soft_deleted_slot_logged_sets_resolve_in_workout_detail(fx, open_ro):
    """AC7: a slot removed from the routine after the workout still shows its logged sets."""
    r = fx.routine("ZZ-QA removed slot")
    live = fx.slot(r, 0, fx.catalog[0][0])
    gone = fx.slot(r, 1, fx.catalog[1][0], removed=True)
    d = date(2026, 8, 3)
    rows = [fx.value(live, i, 8, 60.0, added=d) for i in range(2)]
    rows += [fx.value(gone, i, 12, 20.0, added=d) for i in range(3)]
    w = fx.workout(r, datetime(2026, 8, 3, 18, 0), rows)
    fx.commit()
    conn = open_ro()

    wd = queries.get_workout_detail(conn, w)
    assert [e.ue_pk for e in wd.exercises] == [live, gone]
    assert exercise(wd, live).slot_removed is False
    removed = exercise(wd, gone)
    assert removed.slot_removed is True
    assert removed.exercise_z_pk == fx.catalog[1][0]
    assert [(s.set_no, s.reps, s.weight_kg) for s in removed.sets] == [
        (i, 12, 20.0) for i in range(3)
    ]

    # get_routine lists only the routine's live slots
    assert [e.ue_pk for e in queries.get_routine(conn, r, history_depth=5).exercises] == [live]


def test_workout_detail_uses_only_that_workouts_sets(fx, open_ro):
    """Shared rows belong to each workout they are linked to; the detail of each workout is
    exactly its own linked set list."""
    r = fx.routine("ZZ-QA detail scoping")
    ue = fx.slot(r, 0)
    old0 = fx.value(
        ue, 0, 10, kg(25), added=date(2026, 8, 3), removed=datetime(2026, 8, 10, 20, 46)
    )
    s1 = fx.value(ue, 1, 10, kg(35), added=date(2026, 8, 3))
    new0 = fx.value(ue, 0, 10, kg(35), added=date(2026, 8, 10))
    w1 = fx.workout(r, datetime(2026, 8, 3, 18, 0), [old0, s1])
    w2 = fx.workout(r, datetime(2026, 8, 10, 18, 0), [new0, s1])
    fx.commit()
    conn = open_ro()

    d1 = queries.get_workout_detail(conn, w1, weight_unit="lb")
    d2 = queries.get_workout_detail(conn, w2, weight_unit="lb")
    assert [(s.set_no, s.weight) for s in exercise(d1, ue).sets] == [(0, 25.0), (1, 35.0)]
    assert [(s.set_no, s.weight) for s in exercise(d2, ue).sets] == [(0, 35.0), (1, 35.0)]

    ls = queries.logged_sets(conn, workout_pks=[w1, w2])
    assert [x.workout_pk for x in ls] == [w2, w2, w1, w1]  # newest workout first
    assert {x.value_pk for x in ls if x.workout_pk == w1} == {old0, s1}
    assert {x.value_pk for x in ls if x.workout_pk == w2} == {new0, s1}
