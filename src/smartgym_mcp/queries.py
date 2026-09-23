"""Read queries — the canonical SQL with the critical join baked in.

All functions take a read-only sqlite3.Connection and return Pydantic models.
The join `ZVALUES.ZEXERCISE → ZUNIQEXERCISE.Z_PK` and CoreData epoch math live
here so callers never re-derive them.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from . import db
from .models import (
    EquipmentItem,
    EquipmentListResult,
    ExerciseEntry,
    RoutineDetail,
    RoutineListResult,
    RoutineSummary,
    SessionEntry,
    SetEntry,
    WeightUnit,
    WorkoutDetail,
    WorkoutExercise,
    WorkoutHistoryResult,
    WorkoutSession,
)

ALLOWED_RANGE_DAYS = (7, 14, 30)
_KG_PER_LB = 0.45359237


class RoutineNotFound(ValueError):
    pass


class AmbiguousRoutine(ValueError):
    pass


class WorkoutNotFound(ValueError):
    pass


def _iso_local(ts: float | None) -> str | None:
    if ts is None:
        return None
    return db.coredata_to_datetime(ts).astimezone().strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- #
# Routine resolution (name OR z_pk)
# --------------------------------------------------------------------------- #
def resolve_routine(conn: sqlite3.Connection, routine: str | int) -> int:
    """Resolve a routine reference to a Z_PK. Numeric → Z_PK; else exact (then
    substring) case-insensitive name match. Raises actionable errors."""
    s = str(routine).strip()
    if s.isdigit():
        row = conn.execute(
            "SELECT Z_PK FROM ZROUTINE WHERE Z_PK = ? AND ZDATEREMOVED IS NULL",
            (int(s),),
        ).fetchone()
        if row:
            return int(row[0])
        raise RoutineNotFound(f"No active routine with z_pk={s}.")

    rows = conn.execute(
        "SELECT Z_PK, ZNAME FROM ZROUTINE "
        "WHERE ZDATEREMOVED IS NULL AND LOWER(ZNAME) = LOWER(?)",
        (s,),
    ).fetchall()
    if not rows:
        rows = conn.execute(
            "SELECT Z_PK, ZNAME FROM ZROUTINE "
            "WHERE ZDATEREMOVED IS NULL AND LOWER(ZNAME) LIKE '%' || LOWER(?) || '%'",
            (s,),
        ).fetchall()
    if not rows:
        raise RoutineNotFound(
            f"No routine matching {routine!r}. Use smartgym_list_routines to see names."
        )
    if len(rows) > 1:
        listed = ", ".join(f"{r['ZNAME']} (z_pk={r['Z_PK']})" for r in rows)
        raise AmbiguousRoutine(
            f"Multiple routines match {routine!r}: {listed}. Pass a z_pk to disambiguate."
        )
    return int(rows[0]["Z_PK"])


# --------------------------------------------------------------------------- #
# Logged sets — the ONLY place a set is resolved to a workout
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LoggedSet:
    workout_pk: int
    workout_start: float  # ZWORKOUT.ZSTARTDATE, Core Data seconds
    history_pk: int
    history_routine_pk: int | None  # ZHISTORY.ZROUTINE
    ue_pk: int
    slot_routine_pk: int | None  # ZUNIQEXERCISE.ZROUTINE
    slot_index: int | None  # ZUNIQEXERCISE.ZINDEX
    slot_removed: bool  # ZUNIQEXERCISE.ZDATEREMOVED IS NOT NULL
    exercise_z_pk: int  # catalog ZEXERCISE.Z_PK
    exercise_name: str
    value_pk: int  # ZVALUES.Z_PK
    set_no: int
    reps: int
    weight_kg: float


# "Logged" = linked to a history through Z_11SETSDONE. Deliberately NO timestamp
# filter and NO ZVALUES.ZDATEREMOVED filter: a linked soft-deleted row is history
# (the app replaces a changed set and soft-deletes the old row); unlinked rows are
# the routine's prescription. A value linked to several histories of ONE workout is
# kept once, preferring the history in the slot's own routine.
_LOGGED_SETS_SQL = """
SELECT * FROM (
  SELECT w.Z_PK AS workout_pk, w.ZSTARTDATE AS workout_start,
         h.Z_PK AS history_pk, h.ZROUTINE AS history_routine_pk,
         ue.Z_PK AS ue_pk, ue.ZROUTINE AS slot_routine_pk, ue.ZINDEX AS slot_index,
         ue.ZDATEREMOVED IS NOT NULL AS slot_removed,
         e.Z_PK AS exercise_z_pk, e.ZNAME AS exercise_name,
         v.Z_PK AS value_pk, v.ZINDEX AS set_no,
         CAST(v.ZSECONDVALUE AS INTEGER) AS reps, v.ZTHIRDVALUE AS weight_kg,
         ROW_NUMBER() OVER (
           PARTITION BY w.Z_PK, v.Z_PK
           ORDER BY (h.ZROUTINE IS ue.ZROUTINE) DESC, h.Z_PK
         ) AS dup_rank
  FROM Z_11SETSDONE sd
  JOIN ZHISTORY h ON h.Z_PK = sd.Z_11HISTORIES1
  JOIN ZWORKOUT w ON h.ZWORKOUT = w.Z_PK
  JOIN ZVALUES v ON v.Z_PK = sd.Z_21SETSDONE
  JOIN ZUNIQEXERCISE ue ON v.ZEXERCISE = ue.Z_PK
  JOIN ZEXERCISE e ON ue.ZEXERCISE = e.Z_PK
  WHERE {where}
  -- pending R1 (issue #7): a rule excluding sets deleted mid-workout would go here.
)
WHERE dup_rank = 1
ORDER BY workout_start DESC, workout_pk DESC, slot_index, ue_pk, set_no, value_pk
"""


def logged_sets(
    conn: sqlite3.Connection,
    *,
    workout_pks: Sequence[int] | None = None,
    ue_pks: Sequence[int] | None = None,
) -> list[LoggedSet]:
    """Every (workout, set) pair via Z_11SETSDONE. Ordered newest workout first
    (ZSTARTDATE DESC, workout_pk DESC), then slot_index, ue_pk, set_no, value_pk."""
    # A workout without a start date isn't a finished session and has no date to report;
    # get_workout_history and get_workout_detail skip it too, so all three agree.
    where = ["w.ZSTARTDATE IS NOT NULL"]
    params: list[int] = []
    for column, keys in (("w.Z_PK", workout_pks), ("ue.Z_PK", ue_pks)):
        if keys is None:
            continue
        if not keys:
            return []
        where.append(f"{column} IN ({','.join('?' * len(keys))})")
        params.extend(int(k) for k in keys)

    rows = conn.execute(_LOGGED_SETS_SQL.format(where=" AND ".join(where)), params)
    return [
        LoggedSet(
            workout_pk=r["workout_pk"],
            workout_start=r["workout_start"],
            history_pk=r["history_pk"],
            history_routine_pk=r["history_routine_pk"],
            ue_pk=r["ue_pk"],
            slot_routine_pk=r["slot_routine_pk"],
            slot_index=r["slot_index"],
            slot_removed=bool(r["slot_removed"]),
            exercise_z_pk=r["exercise_z_pk"],
            exercise_name=r["exercise_name"],
            value_pk=r["value_pk"],
            set_no=r["set_no"],
            reps=r["reps"],
            weight_kg=r["weight_kg"],
        )
        for r in rows
    ]


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
def list_routines(conn: sqlite3.Connection, include_hidden: bool = False) -> RoutineListResult:
    sql = (
        "SELECT r.Z_PK, r.ZNAME, r.ZDAYS, r.ZHIDDEN, r.ZHASSYNCED, r.ZDATELASTUPDATEDBYAI, "
        "(SELECT COUNT(*) FROM ZUNIQEXERCISE ue "
        " WHERE ue.ZROUTINE = r.Z_PK AND ue.ZDATEREMOVED IS NULL) AS exercise_count "
        "FROM ZROUTINE r WHERE r.ZDATEREMOVED IS NULL"
    )
    if not include_hidden:
        sql += " AND r.ZHIDDEN = 0"
    sql += " ORDER BY r.ZNAME"

    routines = [
        RoutineSummary(
            z_pk=row["Z_PK"],
            name=row["ZNAME"],
            days=row["ZDAYS"],
            hidden=bool(row["ZHIDDEN"]),
            has_synced=bool(row["ZHASSYNCED"]),
            last_updated_by_ai=_iso_local(row["ZDATELASTUPDATEDBYAI"]),
            exercise_count=row["exercise_count"],
        )
        for row in conn.execute(sql)
    ]
    return RoutineListResult(count=len(routines), routines=routines)


def _to_unit(weight_kg: float, weight_unit: WeightUnit) -> float:
    if weight_unit == "lb":
        return round(weight_kg / _KG_PER_LB, 1)
    return round(weight_kg, 1)


def _set_entry(s: LoggedSet, weight_unit: WeightUnit) -> SetEntry:
    return SetEntry(
        set_no=s.set_no,
        reps=s.reps,
        weight_kg=s.weight_kg,
        weight=_to_unit(s.weight_kg, weight_unit),
    )


def _local_date(ts: float) -> str:
    return db.coredata_to_datetime(ts).astimezone().strftime("%Y-%m-%d")


def get_routine(
    conn: sqlite3.Connection,
    routine: str | int,
    history_depth: int = 5,
    weight_unit: WeightUnit = "kg",
) -> RoutineDetail:
    z_pk = resolve_routine(conn, routine)
    rinfo = conn.execute(
        "SELECT ZNAME, ZDAYS, ZHIDDEN, ZHASSYNCED, ZDATELASTUPDATEDBYAI "
        "FROM ZROUTINE WHERE Z_PK = ?",
        (z_pk,),
    ).fetchone()
    if rinfo is None:  # resolved a moment ago — only None on a concurrent delete
        raise RoutineNotFound(f"Routine z_pk={z_pk} disappeared mid-read.")

    exercises = conn.execute(
        "SELECT ue.Z_PK AS ue_pk, ue.ZINDEX AS idx, e.ZNAME AS exercise_name, "
        "ue.ZPAUSE AS rest_seconds, ue.ZNOTE AS note "
        "FROM ZUNIQEXERCISE ue JOIN ZEXERCISE e ON ue.ZEXERCISE = e.Z_PK "
        "WHERE ue.ZROUTINE = ? AND ue.ZDATEREMOVED IS NULL ORDER BY ue.ZINDEX",
        (z_pk,),
    ).fetchall()

    # Group logged sets → one session per workout per slot. logged_sets is ordered
    # newest workout first, so dict insertion order is newest first. Sets logged in
    # another routine's workout (history routine != slot routine) are not this
    # routine's history.
    grouped: dict[int, dict[int, tuple[float, list[LoggedSet]]]] = {}
    for s in logged_sets(conn, ue_pks=[ex["ue_pk"] for ex in exercises]):
        if s.history_routine_pk != s.slot_routine_pk:
            continue
        sessions = grouped.setdefault(s.ue_pk, {})
        if s.workout_pk not in sessions:
            if len(sessions) >= history_depth:
                continue  # older workout beyond the requested depth
            sessions[s.workout_pk] = (s.workout_start, [])
        sessions[s.workout_pk][1].append(s)

    out = []
    for ex in exercises:
        ex_sessions = [
            SessionEntry(
                workout_pk=workout_pk,
                date=_local_date(start),
                sets=[
                    _set_entry(s, weight_unit)
                    for s in sorted(sets, key=lambda s: (s.set_no, s.value_pk))
                ],
            )
            for workout_pk, (start, sets) in grouped.get(ex["ue_pk"], {}).items()
        ]
        top_set: SetEntry | None = None
        total_volume = 0.0
        if ex_sessions:
            latest = ex_sessions[0].sets
            top_set = max(latest, key=lambda s: (s.weight_kg, s.reps))
            total_volume = sum(s.reps * s.weight for s in latest)
        out.append(
            ExerciseEntry(
                ue_pk=ex["ue_pk"],
                index=ex["idx"],
                exercise_name=ex["exercise_name"],
                rest_seconds=ex["rest_seconds"],
                note=ex["note"],
                sessions=ex_sessions,
                top_set=top_set,
                total_volume=total_volume,
            )
        )

    return RoutineDetail(
        z_pk=z_pk,
        name=rinfo["ZNAME"],
        days=rinfo["ZDAYS"],
        hidden=bool(rinfo["ZHIDDEN"]),
        has_synced=bool(rinfo["ZHASSYNCED"]),
        last_updated_by_ai=_iso_local(rinfo["ZDATELASTUPDATEDBYAI"]),
        weight_unit=weight_unit,
        exercises=out,
    )


def _local_midnight_coredata(date_str: str) -> float:
    return db.datetime_to_coredata(datetime.strptime(date_str, "%Y-%m-%d"))


# --------------------------------------------------------------------------- #
# Workout metadata — ONE derivation shared by get_workout_history and
# get_workout_detail, so the two tools never disagree about a workout.
# The routine is the one of the history ZWORKOUT.ZHISTORY points at (the
# "primary history"), falling back to the lowest-pk linked history with a routine.
# --------------------------------------------------------------------------- #
_WORKOUT_META_SELECT = (
    "SELECT m.*, r.ZNAME AS routine_name FROM ("
    "SELECT w.Z_PK AS workout_pk, "
    f"datetime(w.ZSTARTDATE {db.EPOCH_SQL}) AS start_local, "
    f"date(w.ZSTARTDATE {db.EPOCH_SQL}) AS date, "
    "CAST(w.ZDURATION / 60 AS INTEGER) AS duration_min, "
    "CAST(w.ZCALORIES AS INTEGER) AS calories, "
    "CAST(w.ZAVERAGEHEARTRATE AS INTEGER) AS avg_hr, "
    "CAST(w.ZMAXHEARTRATE AS INTEGER) AS max_hr, "
    "COALESCE("
    " (SELECT ph.ZROUTINE FROM ZHISTORY ph WHERE ph.Z_PK = w.ZHISTORY),"
    " (SELECT ah.ZROUTINE FROM ZHISTORY ah WHERE ah.ZWORKOUT = w.Z_PK"
    "  AND ah.ZROUTINE IS NOT NULL ORDER BY ah.Z_PK LIMIT 1)"
    ") AS routine_pk, "
    "w.ZSTARTDATE AS start_ts "
    "FROM ZWORKOUT w WHERE {where}"
    ") m LEFT JOIN ZROUTINE r ON r.Z_PK = m.routine_pk"
)


def _workout_meta_rows(
    conn: sqlite3.Connection, where: str, params: Sequence[object], tail: str = ""
) -> list[sqlite3.Row]:
    sql = _WORKOUT_META_SELECT.format(where=where) + tail
    return conn.execute(sql, list(params)).fetchall()


def get_workout_history(
    conn: sqlite3.Connection,
    *,
    days: int = 7,
    date_from: str | None = None,
    date_to: str | None = None,
    routine: str | int | None = None,
    limit: int = 20,
    offset: int = 0,
) -> WorkoutHistoryResult:
    where = [
        "w.ZSTARTDATE IS NOT NULL",
        "EXISTS (SELECT 1 FROM ZHISTORY h WHERE h.ZWORKOUT = w.Z_PK)",
    ]
    params: list[float | int] = []

    if date_from or date_to:
        if date_from:
            where.append("w.ZSTARTDATE >= ?")
            params.append(_local_midnight_coredata(date_from))
        if date_to:  # inclusive end-of-day → strictly before next midnight
            where.append("w.ZSTARTDATE < ?")
            params.append(_local_midnight_coredata(date_to) + 86400)
    else:
        if days not in ALLOWED_RANGE_DAYS:
            raise ValueError(f"days must be one of {ALLOWED_RANGE_DAYS}, got {days}")
        where.append("w.ZSTARTDATE >= ?")
        params.append(db.now_coredata() - days * 86400)

    if routine is not None:
        where.append(
            "EXISTS (SELECT 1 FROM ZHISTORY h WHERE h.ZWORKOUT = w.Z_PK AND h.ZROUTINE = ?)"
        )
        params.append(resolve_routine(conn, routine))

    clause = " AND ".join(where)

    total = conn.execute(f"SELECT COUNT(*) FROM ZWORKOUT w WHERE {clause}", params).fetchone()[
        0
    ]

    rows = _workout_meta_rows(
        conn,
        clause,
        [*params, limit, offset],
        " ORDER BY m.start_ts DESC, m.workout_pk DESC LIMIT ? OFFSET ?",
    )

    sessions = [
        WorkoutSession(
            workout_pk=r["workout_pk"],
            date=r["date"],
            routine=r["routine_name"],
            routine_z_pk=r["routine_pk"],
            duration_min=r["duration_min"] or 0,
            calories=r["calories"],
            avg_hr=r["avg_hr"],
            max_hr=r["max_hr"],
        )
        for r in rows
    ]
    has_more = offset + len(sessions) < total
    return WorkoutHistoryResult(
        total=total,
        count=len(sessions),
        offset=offset,
        has_more=has_more,
        next_offset=offset + limit if has_more else None,
        sessions=sessions,
    )


def get_workout_detail(
    conn: sqlite3.Connection, workout_pk: int, weight_unit: WeightUnit = "kg"
) -> WorkoutDetail:
    rows = _workout_meta_rows(
        conn, "w.Z_PK = ? AND w.ZSTARTDATE IS NOT NULL", [int(workout_pk)]
    )
    if not rows:
        exists = conn.execute(
            "SELECT 1 FROM ZWORKOUT WHERE Z_PK = ?", (int(workout_pk),)
        ).fetchone()
        reason = (
            f"Workout {workout_pk} has no start date, so it isn't a completed session."
            if exists
            else f"No workout with workout_pk={workout_pk}."
        )
        raise WorkoutNotFound(
            f"{reason} Use smartgym_get_workout_history to list workouts and their workout_pk."
        )
    meta = rows[0]

    # Group by slot, keeping logged_sets' order (slot ZINDEX, ue_pk, set_no).
    by_slot: dict[int, list[LoggedSet]] = {}
    for s in logged_sets(conn, workout_pks=[int(workout_pk)]):
        by_slot.setdefault(s.ue_pk, []).append(s)

    exercises = [
        WorkoutExercise(
            ue_pk=ue_pk,
            exercise_z_pk=sets[0].exercise_z_pk,
            exercise_name=sets[0].exercise_name,
            index=sets[0].slot_index,
            slot_removed=sets[0].slot_removed,
            sets=[_set_entry(s, weight_unit) for s in sets],
        )
        for ue_pk, sets in by_slot.items()
    ]
    # No per-workout order is stored (Z_11EXERCISES has no ordering column), so use the
    # current slot order: live slots first, then soft-deleted slots by their last ZINDEX.
    exercises.sort(
        key=lambda e: (e.slot_removed, e.index if e.index is not None else 1 << 30, e.ue_pk)
    )
    warnings: list[str] = []
    if not exercises:
        warnings.append(
            f"Workout {workout_pk} has no logged sets (nothing linked to its histories)."
        )

    return WorkoutDetail(
        workout_pk=meta["workout_pk"],
        routine_z_pk=meta["routine_pk"],
        routine_name=meta["routine_name"],
        start_local=meta["start_local"],
        date=meta["date"],
        duration_min=meta["duration_min"] or 0,
        calories=meta["calories"],
        avg_hr=meta["avg_hr"],
        max_hr=meta["max_hr"],
        weight_unit=weight_unit,
        exercises=exercises,
        warnings=warnings,
    )


def get_equipment(conn: sqlite3.Connection, owned_only: bool = True) -> EquipmentListResult:
    sql = (
        "SELECT ZNAME AS name, ZCATEGORY AS category_id, ZSELECTED AS owned, "
        "ZSELECTEDWEIGHTS AS selected_weights FROM ZEQUIPMENT"
    )
    if owned_only:
        sql += " WHERE ZSELECTED = 1"
    sql += " ORDER BY ZCATEGORY, ZNAME"

    items = [
        EquipmentItem(
            name=row["name"],
            category_id=row["category_id"],
            owned=bool(row["owned"]),
            selected_weights=row["selected_weights"] or None,
        )
        for row in conn.execute(sql)
    ]
    return EquipmentListResult(count=len(items), equipment=items)
