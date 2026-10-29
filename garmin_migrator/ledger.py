"""SQLite checkpoint ledger for idempotent, resumable cross-region migration.

Tracks every migrated record across all domains:
- activities (with source_id -> target_id mapping and metadata_synced flag)
- wellness_days (daily 24/7 monitoring .fit archives: steps, sleep, HR, HRV, stress, Body Battery)
- weigh_ins (weight & 12-metric body composition records)
- blood_pressure (systolic/diastolic/pulse readings)
- hydration (daily water intake logs)
- gear (shoes/bikes UUID mapping across regions)
- workouts (structured workout templates)
- offline_files (files imported from Garmin Official GDPR Data Export archive)
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any


class MigrationLedger:
    """Persistent SQLite state store for断点续传 (checkpoint & resume) and ID mapping."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self._connect()

    def _connect(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA busy_timeout=15000;")
        self._init_schema()

    def _init_schema(self) -> None:
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS activities (
                    direction TEXT NOT NULL,
                    source_id INTEGER NOT NULL,
                    target_id INTEGER,
                    activity_name TEXT,
                    activity_type TEXT,
                    start_time_local TEXT,
                    duration REAL,
                    file_format TEXT,
                    status TEXT NOT NULL,          -- 'uploaded', 'duplicate', 'unavailable', 'failed'
                    metadata_synced INTEGER DEFAULT 0,
                    error_msg TEXT,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (direction, source_id)
                );

                CREATE TABLE IF NOT EXISTS wellness_days (
                    direction TEXT NOT NULL,
                    calendar_date TEXT NOT NULL,   -- YYYY-MM-DD
                    total_files INTEGER DEFAULT 0,
                    uploaded_files INTEGER DEFAULT 0,
                    duplicate_files INTEGER DEFAULT 0,
                    failed_files INTEGER DEFAULT 0,
                    status TEXT NOT NULL,          -- 'done', 'empty', 'partial', 'failed'
                    error_msg TEXT,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (direction, calendar_date)
                );

                CREATE TABLE IF NOT EXISTS weigh_ins (
                    direction TEXT NOT NULL,
                    sample_pk INTEGER NOT NULL,
                    calendar_date TEXT NOT NULL,
                    weight_kg REAL,
                    status TEXT NOT NULL,          -- 'uploaded', 'failed'
                    error_msg TEXT,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (direction, sample_pk)
                );

                CREATE TABLE IF NOT EXISTS blood_pressure (
                    direction TEXT NOT NULL,
                    version TEXT NOT NULL,
                    calendar_date TEXT NOT NULL,
                    status TEXT NOT NULL,          -- 'uploaded', 'failed'
                    error_msg TEXT,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (direction, version)
                );

                CREATE TABLE IF NOT EXISTS hydration (
                    direction TEXT NOT NULL,
                    calendar_date TEXT NOT NULL,
                    value_ml REAL NOT NULL,
                    status TEXT NOT NULL,          -- 'uploaded', 'failed'
                    error_msg TEXT,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (direction, calendar_date)
                );

                CREATE TABLE IF NOT EXISTS gear (
                    direction TEXT NOT NULL,
                    source_uuid TEXT NOT NULL,
                    target_uuid TEXT,
                    display_name TEXT,
                    gear_type TEXT,
                    status TEXT NOT NULL,          -- 'created', 'matched', 'failed'
                    error_msg TEXT,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (direction, source_uuid)
                );

                CREATE TABLE IF NOT EXISTS workouts (
                    direction TEXT NOT NULL,
                    source_workout_id INTEGER NOT NULL,
                    target_workout_id INTEGER,
                    workout_name TEXT,
                    status TEXT NOT NULL,          -- 'uploaded', 'matched', 'failed'
                    error_msg TEXT,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (direction, source_workout_id)
                );

                CREATE TABLE IF NOT EXISTS offline_files (
                    file_key TEXT PRIMARY KEY,     -- relative path or SHA/filename key
                    file_type TEXT,
                    status TEXT NOT NULL,          -- 'uploaded', 'duplicate', 'failed'
                    target_activity_id INTEGER,
                    error_msg TEXT,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS intervals_activities (
                    source_region TEXT NOT NULL,
                    source_id INTEGER NOT NULL,
                    intervals_id TEXT,
                    activity_name TEXT,
                    start_time_local TEXT,
                    file_format TEXT,
                    status TEXT NOT NULL,          -- 'uploaded', 'duplicate', 'unavailable', 'failed'
                    error_msg TEXT,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (source_region, source_id)
                );

                CREATE TABLE IF NOT EXISTS intervals_wellness (
                    source_region TEXT NOT NULL,
                    calendar_date TEXT NOT NULL,   -- YYYY-MM-DD
                    metrics_count INTEGER DEFAULT 0,
                    status TEXT NOT NULL,          -- 'uploaded', 'empty', 'failed'
                    error_msg TEXT,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (source_region, calendar_date)
                );
                """
            )

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ #
    #  Activities                                                        #
    # ------------------------------------------------------------------ #

    def get_activity(self, direction: str, source_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM activities WHERE direction = ? AND source_id = ?",
            (direction, int(source_id)),
        ).fetchone()
        return dict(row) if row else None

    def is_activity_done(
        self, direction: str, source_id: int, require_metadata: bool = False
    ) -> bool:
        row = self.get_activity(direction, source_id)
        if not row:
            return False
        if row["status"] in ("uploaded", "duplicate"):
            if require_metadata and not row["metadata_synced"]:
                return False
            return True
        # Unavailable on source side: skip for 7 days
        if row["status"] == "unavailable" and (time.time() - row["updated_at"]) < 7 * 86400:
            return True
        return False

    def record_activity(
        self,
        direction: str,
        source_id: int,
        *,
        target_id: int | None = None,
        activity_name: str = "",
        activity_type: str = "",
        start_time_local: str = "",
        duration: float = 0.0,
        file_format: str = "fit",
        status: str = "uploaded",
        metadata_synced: bool = False,
        error_msg: str = "",
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO activities (
                    direction, source_id, target_id, activity_name, activity_type,
                    start_time_local, duration, file_format, status, metadata_synced,
                    error_msg, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(direction, source_id) DO UPDATE SET
                    target_id = COALESCE(excluded.target_id, activities.target_id),
                    activity_name = excluded.activity_name,
                    activity_type = excluded.activity_type,
                    start_time_local = excluded.start_time_local,
                    duration = excluded.duration,
                    file_format = excluded.file_format,
                    status = excluded.status,
                    metadata_synced = excluded.metadata_synced,
                    error_msg = excluded.error_msg,
                    updated_at = excluded.updated_at
                """,
                (
                    direction,
                    int(source_id),
                    int(target_id) if target_id else None,
                    activity_name,
                    activity_type,
                    start_time_local,
                    float(duration or 0.0),
                    file_format,
                    status,
                    1 if metadata_synced else 0,
                    error_msg,
                    int(time.time()),
                ),
            )

    def get_activity_id_map(self, direction: str) -> dict[int, int]:
        """Return {source_activity_id: target_activity_id} for all migrated activities."""
        rows = self.conn.execute(
            "SELECT source_id, target_id FROM activities WHERE direction = ? AND target_id IS NOT NULL",
            (direction,),
        ).fetchall()
        return {int(r["source_id"]): int(r["target_id"]) for r in rows}

    # ------------------------------------------------------------------ #
    #  Wellness Days                                                     #
    # ------------------------------------------------------------------ #

    def is_wellness_day_done(self, direction: str, calendar_date: str) -> bool:
        row = self.conn.execute(
            "SELECT status FROM wellness_days WHERE direction = ? AND calendar_date = ?",
            (direction, calendar_date),
        ).fetchone()
        return bool(row and row["status"] in ("done", "empty"))

    def record_wellness_day(
        self,
        direction: str,
        calendar_date: str,
        *,
        total_files: int = 0,
        uploaded_files: int = 0,
        duplicate_files: int = 0,
        failed_files: int = 0,
        status: str = "done",
        error_msg: str = "",
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO wellness_days (
                    direction, calendar_date, total_files, uploaded_files,
                    duplicate_files, failed_files, status, error_msg, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(direction, calendar_date) DO UPDATE SET
                    total_files = excluded.total_files,
                    uploaded_files = excluded.uploaded_files,
                    duplicate_files = excluded.duplicate_files,
                    failed_files = excluded.failed_files,
                    status = excluded.status,
                    error_msg = excluded.error_msg,
                    updated_at = excluded.updated_at
                """,
                (
                    direction,
                    calendar_date,
                    total_files,
                    uploaded_files,
                    duplicate_files,
                    failed_files,
                    status,
                    error_msg,
                    int(time.time()),
                ),
            )

    # ------------------------------------------------------------------ #
    #  Weigh-ins & Body Composition                                      #
    # ------------------------------------------------------------------ #

    def is_weigh_in_done(self, direction: str, sample_pk: int) -> bool:
        row = self.conn.execute(
            "SELECT status FROM weigh_ins WHERE direction = ? AND sample_pk = ?",
            (direction, int(sample_pk)),
        ).fetchone()
        return bool(row and row["status"] == "uploaded")

    def record_weigh_in(
        self,
        direction: str,
        sample_pk: int,
        calendar_date: str,
        weight_kg: float | None,
        status: str = "uploaded",
        error_msg: str = "",
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO weigh_ins (
                    direction, sample_pk, calendar_date, weight_kg, status, error_msg, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(direction, sample_pk) DO UPDATE SET
                    status = excluded.status,
                    error_msg = excluded.error_msg,
                    updated_at = excluded.updated_at
                """,
                (
                    direction,
                    int(sample_pk),
                    calendar_date,
                    weight_kg,
                    status,
                    error_msg,
                    int(time.time()),
                ),
            )

    # ------------------------------------------------------------------ #
    #  Blood Pressure & Hydration                                        #
    # ------------------------------------------------------------------ #

    def is_blood_pressure_done(self, direction: str, version: str | int) -> bool:
        row = self.conn.execute(
            "SELECT status FROM blood_pressure WHERE direction = ? AND version = ?",
            (direction, str(version)),
        ).fetchone()
        return bool(row and row["status"] == "uploaded")

    def record_blood_pressure(
        self,
        direction: str,
        version: str | int,
        calendar_date: str,
        status: str = "uploaded",
        error_msg: str = "",
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO blood_pressure (
                    direction, version, calendar_date, status, error_msg, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(direction, version) DO UPDATE SET
                    status = excluded.status,
                    error_msg = excluded.error_msg,
                    updated_at = excluded.updated_at
                """,
                (direction, str(version), calendar_date, status, error_msg, int(time.time())),
            )

    def is_hydration_done(self, direction: str, calendar_date: str) -> bool:
        row = self.conn.execute(
            "SELECT status FROM hydration WHERE direction = ? AND calendar_date = ?",
            (direction, calendar_date),
        ).fetchone()
        return bool(row and row["status"] == "uploaded")

    def record_hydration(
        self,
        direction: str,
        calendar_date: str,
        value_ml: float,
        status: str = "uploaded",
        error_msg: str = "",
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO hydration (
                    direction, calendar_date, value_ml, status, error_msg, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(direction, calendar_date) DO UPDATE SET
                    value_ml = excluded.value_ml,
                    status = excluded.status,
                    error_msg = excluded.error_msg,
                    updated_at = excluded.updated_at
                """,
                (direction, calendar_date, float(value_ml), status, error_msg, int(time.time())),
            )

    # ------------------------------------------------------------------ #
    #  Gear & Workouts                                                   #
    # ------------------------------------------------------------------ #

    def get_gear_target_uuid(self, direction: str, source_uuid: str) -> str | None:
        row = self.conn.execute(
            "SELECT target_uuid, status FROM gear WHERE direction = ? AND source_uuid = ?",
            (direction, source_uuid),
        ).fetchone()
        if row and row["status"] in ("created", "matched") and row["target_uuid"]:
            return str(row["target_uuid"])
        return None

    def record_gear(
        self,
        direction: str,
        source_uuid: str,
        target_uuid: str | None,
        display_name: str,
        gear_type: str,
        status: str = "created",
        error_msg: str = "",
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO gear (
                    direction, source_uuid, target_uuid, display_name, gear_type, status, error_msg, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(direction, source_uuid) DO UPDATE SET
                    target_uuid = COALESCE(excluded.target_uuid, gear.target_uuid),
                    display_name = excluded.display_name,
                    status = excluded.status,
                    error_msg = excluded.error_msg,
                    updated_at = excluded.updated_at
                """,
                (
                    direction,
                    source_uuid,
                    target_uuid,
                    display_name,
                    gear_type,
                    status,
                    error_msg,
                    int(time.time()),
                ),
            )

    def is_workout_done(self, direction: str, source_workout_id: int) -> bool:
        row = self.conn.execute(
            "SELECT status FROM workouts WHERE direction = ? AND source_workout_id = ?",
            (direction, int(source_workout_id)),
        ).fetchone()
        return bool(row and row["status"] in ("uploaded", "matched"))

    def record_workout(
        self,
        direction: str,
        source_workout_id: int,
        target_workout_id: int | None,
        workout_name: str,
        status: str = "uploaded",
        error_msg: str = "",
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO workouts (
                    direction, source_workout_id, target_workout_id, workout_name, status, error_msg, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(direction, source_workout_id) DO UPDATE SET
                    target_workout_id = COALESCE(excluded.target_workout_id, workouts.target_workout_id),
                    workout_name = excluded.workout_name,
                    status = excluded.status,
                    error_msg = excluded.error_msg,
                    updated_at = excluded.updated_at
                """,
                (
                    direction,
                    int(source_workout_id),
                    int(target_workout_id) if target_workout_id else None,
                    workout_name,
                    status,
                    error_msg,
                    int(time.time()),
                ),
            )

    # ------------------------------------------------------------------ #
    #  Offline Files                                                     #
    # ------------------------------------------------------------------ #

    def is_offline_file_done(self, file_key: str) -> bool:
        row = self.conn.execute(
            "SELECT status FROM offline_files WHERE file_key = ?",
            (file_key,),
        ).fetchone()
        return bool(row and row["status"] in ("uploaded", "duplicate"))

    def record_offline_file(
        self,
        file_key: str,
        file_type: str,
        status: str,
        target_activity_id: int | None = None,
        error_msg: str = "",
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO offline_files (
                    file_key, file_type, status, target_activity_id, error_msg, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_key) DO UPDATE SET
                    status = excluded.status,
                    target_activity_id = COALESCE(excluded.target_activity_id, offline_files.target_activity_id),
                    error_msg = excluded.error_msg,
                    updated_at = excluded.updated_at
                """,
                (
                    file_key,
                    file_type,
                    status,
                    int(target_activity_id) if target_activity_id else None,
                    error_msg,
                    int(time.time()),
                ),
            )

    # ------------------------------------------------------------------ #
    #  Intervals.icu Sync                                                #
    # ------------------------------------------------------------------ #

    def is_intervals_activity_done(self, source_region: str, source_id: int) -> bool:
        row = self.conn.execute(
            "SELECT status, updated_at FROM intervals_activities WHERE source_region = ? AND source_id = ?",
            (source_region, int(source_id)),
        ).fetchone()
        if not row:
            return False
        if row["status"] in ("uploaded", "duplicate"):
            return True
        if row["status"] == "unavailable" and (time.time() - row["updated_at"]) < 7 * 86400:
            return True
        return False

    def record_intervals_activity(
        self,
        source_region: str,
        source_id: int,
        *,
        intervals_id: str | None = None,
        activity_name: str = "",
        start_time_local: str = "",
        file_format: str = "fit",
        status: str = "uploaded",
        error_msg: str = "",
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO intervals_activities (
                    source_region, source_id, intervals_id, activity_name,
                    start_time_local, file_format, status, error_msg, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_region, source_id) DO UPDATE SET
                    intervals_id = COALESCE(excluded.intervals_id, intervals_activities.intervals_id),
                    activity_name = excluded.activity_name,
                    start_time_local = excluded.start_time_local,
                    file_format = excluded.file_format,
                    status = excluded.status,
                    error_msg = excluded.error_msg,
                    updated_at = excluded.updated_at
                """,
                (
                    source_region,
                    int(source_id),
                    str(intervals_id) if intervals_id else None,
                    activity_name,
                    start_time_local,
                    file_format,
                    status,
                    error_msg,
                    int(time.time()),
                ),
            )

    def is_intervals_wellness_done(self, source_region: str, calendar_date: str) -> bool:
        row = self.conn.execute(
            "SELECT status FROM intervals_wellness WHERE source_region = ? AND calendar_date = ?",
            (source_region, calendar_date),
        ).fetchone()
        return bool(row and row["status"] in ("uploaded", "empty"))

    def record_intervals_wellness(
        self,
        source_region: str,
        calendar_date: str,
        *,
        metrics_count: int = 0,
        status: str = "uploaded",
        error_msg: str = "",
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO intervals_wellness (
                    source_region, calendar_date, metrics_count, status, error_msg, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_region, calendar_date) DO UPDATE SET
                    metrics_count = excluded.metrics_count,
                    status = excluded.status,
                    error_msg = excluded.error_msg,
                    updated_at = excluded.updated_at
                """,
                (
                    source_region,
                    calendar_date,
                    int(metrics_count),
                    status,
                    error_msg,
                    int(time.time()),
                ),
            )

    # ------------------------------------------------------------------ #
    #  Summary Statistics                                                #
    # ------------------------------------------------------------------ #

    def get_summary_stats(self, direction: str) -> dict[str, dict[str, int]]:
        """Return aggregated counts per table and status for the given direction."""
        summary: dict[str, dict[str, int]] = {}
        for table in (
            "activities",
            "wellness_days",
            "weigh_ins",
            "blood_pressure",
            "hydration",
            "gear",
            "workouts",
        ):
            rows = self.conn.execute(
                f"SELECT status, COUNT(*) as cnt FROM {table} WHERE direction = ? GROUP BY status",  # noqa: S608
                (direction,),
            ).fetchall()
            summary[table] = {str(r["status"]): int(r["cnt"]) for r in rows}

        offline_rows = self.conn.execute(
            "SELECT status, COUNT(*) as cnt FROM offline_files GROUP BY status"
        ).fetchall()
        summary["offline_files"] = {str(r["status"]): int(r["cnt"]) for r in offline_rows}

        for iv_table in ("intervals_activities", "intervals_wellness"):
            iv_rows = self.conn.execute(
                f"SELECT status, COUNT(*) as cnt FROM {iv_table} GROUP BY status"  # noqa: S608
            ).fetchall()
            summary[iv_table] = {str(r["status"]): int(r["cnt"]) for r in iv_rows}

        return summary
