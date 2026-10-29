"""Body Composition (Weight Scale 12-Metric FIT), Blood Pressure, and Hydration Migrator.

Handles standalone health & biometric services that are stored outside the daily
device monitoring `.fit` archives:
1. Weight & Body Composition (体重、BMI、体脂率、水分率、骨骼肌、骨量、内脏脂肪、代谢年龄、基础代谢等)
   - Fetched from `/weight-service/weight/range/{start}/{end}?includeAll=true`
   - Converted from grams to kilograms where applicable (`weight`, `muscleMass`, `boneMass`, `visceralFatMass`)
   - Encoded into binary `weight_scale` `.fit` files via `FitEncoderWeight` and uploaded
2. Blood Pressure (收缩压、舒张压、脉搏、测量备注)
   - Fetched from `/bloodpressure-service/bloodpressure/range/{start}/{end}?includeAll=true`
   - Written via `set_blood_pressure`
3. Daily Hydration (每日饮水量 ml)
   - Fetched from `/usersummary-service/usersummary/hydration/daily/{cdate}`
   - Written via `add_hydration_data`
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

from garminconnect import Garmin
from garminconnect.fit import FitEncoderWeight

from ..config import MigratorConfig
from ..ledger import MigrationLedger
from ..upload_helper import upload_bytes_to_garmin

logger = logging.getLogger(__name__)


def _grams_to_kg(val: Any) -> float | None:
    if val is None:
        return None
    try:
        fval = float(val)
        return round(fval / 1000.0, 3) if fval > 0 else None
    except (TypeError, ValueError):
        return None


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


class BodyHealthMigrator:
    """Migrates Weight/Body Composition, Blood Pressure, and Hydration records."""

    def __init__(
        self,
        source: Garmin,
        target: Garmin,
        ledger: MigrationLedger,
        config: MigratorConfig,
    ) -> None:
        self.source = source
        self.target = target
        self.ledger = ledger
        self.config = config
        self.direction = config.direction

    # ------------------------------------------------------------------ #
    #  1. Weight & 12-Metric Body Composition                            #
    # ------------------------------------------------------------------ #

    def discover_first_weigh_in_date(self) -> str | None:
        """Query `/weight-service/weight/first` to find the earliest weigh-in date."""
        try:
            first = self.source.get_first_weight()
            if isinstance(first, dict) and first.get("calendarDate"):
                return str(first["calendarDate"])
        except Exception as e:
            logger.debug("Could not fetch first weight date: %s", e)
        return None

    @staticmethod
    def encode_weight_fit_bytes(entry: dict[str, Any]) -> tuple[bytes, float]:
        """Encode a Garmin raw weight dictionary into a valid binary FIT weight_scale file."""
        weight_kg = _grams_to_kg(entry.get("weight"))
        if not weight_kg or weight_kg <= 0:
            raise ValueError(f"Invalid weight value in entry: {entry.get('weight')}")

        # Determine measurement timestamp (UTC)
        ts_gmt_ms = entry.get("timestampGMT") or entry.get("date")
        if ts_gmt_ms:
            dt = datetime.fromtimestamp(float(ts_gmt_ms) / 1000.0, tz=UTC)
        else:
            cal_date = str(entry.get("calendarDate") or date.today().isoformat())
            dt = datetime.fromisoformat(f"{cal_date}T08:00:00+00:00")

        encoder = FitEncoderWeight()
        encoder.write_file_info(time_created=dt)
        encoder.write_file_creator()
        encoder.write_device_info(timestamp=dt)
        encoder.write_weight_scale(
            timestamp=dt,
            weight=weight_kg,
            percent_fat=_safe_float(entry.get("bodyFat")),
            percent_hydration=_safe_float(entry.get("bodyWater")),
            visceral_fat_mass=_grams_to_kg(entry.get("visceralFatMass")),
            bone_mass=_grams_to_kg(entry.get("boneMass")),
            muscle_mass=_grams_to_kg(entry.get("muscleMass")),
            basal_met=_safe_float(entry.get("basalMet")),
            active_met=_safe_float(entry.get("activeMet")),
            physique_rating=_safe_float(entry.get("physiqueRating")),
            metabolic_age=_safe_float(entry.get("metabolicAge")),
            visceral_fat_rating=_safe_float(entry.get("visceralFat")),
            bmi=_safe_float(entry.get("bmi")),
        )
        encoder.finish()
        return encoder.getvalue(), weight_kg

    def migrate_weight_and_body_composition(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        *,
        dry_run: bool = False,
    ) -> dict[str, int]:
        """Fetch all weigh-ins in `[start_date, end_date]` and upload as 12-metric FIT files."""
        end_d = end_date or date.today().isoformat()
        start_d = start_date or self.discover_first_weigh_in_date() or "2015-01-01"

        logger.info("⚖️ 正在拉取体重与身体成分记录 (%s -> %s)...", start_d, end_d)
        raw_entries: list[dict[str, Any]] = []

        # Query using get_body_composition (returns dateWeightList) and fallback to get_weigh_ins
        try:
            body_data = self.source.get_body_composition(start_d, end_d)
            if isinstance(body_data, dict) and isinstance(
                body_data.get("dateWeightList"), list
            ):
                raw_entries = body_data["dateWeightList"]
        except Exception as e:
            logger.debug("get_body_composition failed (%s), trying get_weigh_ins...", e)

        if not raw_entries:
            try:
                weigh_data = self.source.get_weigh_ins(start_d, end_d)
                if isinstance(weigh_data, dict):
                    for summary in weigh_data.get("dailyWeightSummaries") or []:
                        if isinstance(summary, dict):
                            raw_entries.extend(summary.get("allWeightMetrics") or [])
            except Exception as e:
                logger.warning("⚠️ 拉取体重记录失败: %s", e)

        # Sort oldest first by timestampGMT / samplePk
        raw_entries.sort(
            key=lambda x: (
                str(x.get("calendarDate") or ""),
                int(x.get("timestampGMT") or x.get("date") or 0),
            )
        )

        stats = {"total": len(raw_entries), "uploaded": 0, "skipped": 0, "failed": 0}
        if not raw_entries:
            logger.info("✓ 未发现待迁移的体重/体脂记录。")
            return stats

        logger.info("⚖️ 共发现 %d 条体重/体脂记录，开始迁移...", len(raw_entries))
        for idx, entry in enumerate(raw_entries, 1):
            sample_pk = int(
                entry.get("samplePk")
                or entry.get("version")
                or entry.get("timestampGMT")
                or idx
            )
            cal_date = str(entry.get("calendarDate") or "")
            weight_kg = _grams_to_kg(entry.get("weight"))

            if self.ledger.is_weigh_in_done(self.direction, sample_pk):
                stats["skipped"] += 1
                continue

            if dry_run:
                logger.info(
                    "[%d/%d] 🔍 [DRY-RUN] 将迁移体重记录: %s %.2f kg (BMI=%s, 体脂=%s%%)",
                    idx,
                    len(raw_entries),
                    cal_date,
                    weight_kg or 0.0,
                    entry.get("bmi"),
                    entry.get("bodyFat"),
                )
                stats["uploaded"] += 1
                continue

            try:
                fit_bytes, w_kg = self.encode_weight_fit_bytes(entry)
                outcome = upload_bytes_to_garmin(
                    self.target,
                    fit_bytes,
                    f"weight_{cal_date}_{sample_pk}.fit",
                    is_wellness=False,
                )
                if outcome.status in ("uploaded", "duplicate"):
                    stats["uploaded"] += 1
                    self.ledger.record_weigh_in(
                        self.direction,
                        sample_pk,
                        cal_date,
                        w_kg,
                        status="uploaded",
                    )
                    logger.info(
                        "[%d/%d] ✅ 体重/体脂迁移成功: %s %.2f kg (体脂=%s%%)",
                        idx,
                        len(raw_entries),
                        cal_date,
                        w_kg,
                        entry.get("bodyFat") or "-",
                    )
                else:
                    raise RuntimeError(outcome.message or "Weight FIT upload error")
            except Exception as err:
                stats["failed"] += 1
                self.ledger.record_weigh_in(
                    self.direction,
                    sample_pk,
                    cal_date,
                    weight_kg,
                    status="failed",
                    error_msg=str(err),
                )
                logger.error(
                    "[%d/%d] ❌ 体重记录迁移失败 (%s): %s",
                    idx,
                    len(raw_entries),
                    cal_date,
                    err,
                )

            if self.config.upload_delay_seconds > 0 and idx < len(raw_entries):
                time.sleep(min(1.0, self.config.upload_delay_seconds))

        return stats

    # ------------------------------------------------------------------ #
    #  2. Blood Pressure                                                 #
    # ------------------------------------------------------------------ #

    def migrate_blood_pressure(
        self,
        start_date: str = "2018-01-01",
        end_date: str | None = None,
        *,
        dry_run: bool = False,
    ) -> dict[str, int]:
        """Fetch and migrate blood pressure readings in 365-day chunks."""
        end_d = date.fromisoformat(end_date or date.today().isoformat())
        cur_d = date.fromisoformat(start_date)

        measurements: list[dict[str, Any]] = []
        while cur_d <= end_d:
            chunk_end = min(cur_d + timedelta(days=364), end_d)
            try:
                bp_data = self.source.get_blood_pressure(
                    cur_d.isoformat(), chunk_end.isoformat()
                )
                if isinstance(bp_data, dict):
                    for summary in bp_data.get("measurementSummaries") or []:
                        if isinstance(summary, dict):
                            measurements.extend(summary.get("measurements") or [])
            except Exception as e:
                logger.debug(
                    "Blood pressure query %s..%s skipped: %s",
                    cur_d.isoformat(),
                    chunk_end.isoformat(),
                    e,
                )
            cur_d = chunk_end + timedelta(days=1)

        stats = {"total": len(measurements), "uploaded": 0, "skipped": 0, "failed": 0}
        if not measurements:
            logger.info("✓ 未发现待迁移的血压记录。")
            return stats

        logger.info("🩺 共发现 %d 条血压记录，开始迁移...", len(measurements))
        for idx, m in enumerate(measurements, 1):
            ts_local = str(m.get("measurementTimestampLocal") or "")
            cal_date = ts_local[:10] if len(ts_local) >= 10 else date.today().isoformat()
            version = str(m.get("version") or ts_local or idx)
            systolic = int(m.get("systolic") or 0)
            diastolic = int(m.get("diastolic") or 0)
            pulse = int(m["pulse"]) if m.get("pulse") is not None else None
            notes = str(m.get("notes") or "")

            if self.ledger.is_blood_pressure_done(self.direction, version):
                stats["skipped"] += 1
                continue

            if dry_run:
                logger.info(
                    "[%d/%d] 🔍 [DRY-RUN] 将迁移血压记录: %s %d/%d mmHg (脉搏=%s)",
                    idx,
                    len(measurements),
                    ts_local,
                    systolic,
                    diastolic,
                    pulse,
                )
                stats["uploaded"] += 1
                continue

            try:
                self.target.set_blood_pressure(
                    systolic=systolic,
                    diastolic=diastolic,
                    pulse=pulse,
                    timestamp=ts_local,
                    notes=notes,
                )
                stats["uploaded"] += 1
                self.ledger.record_blood_pressure(
                    self.direction, version, cal_date, status="uploaded"
                )
                logger.info(
                    "[%d/%d] ✅ 血压迁移成功: %s %d/%d mmHg",
                    idx,
                    len(measurements),
                    ts_local,
                    systolic,
                    diastolic,
                )
            except Exception as err:
                stats["failed"] += 1
                self.ledger.record_blood_pressure(
                    self.direction,
                    version,
                    cal_date,
                    status="failed",
                    error_msg=str(err),
                )
                logger.error("[%d/%d] ❌ 血压迁移失败 (%s): %s", idx, len(measurements), ts_local, err)

            if self.config.upload_delay_seconds > 0:
                time.sleep(min(0.8, self.config.upload_delay_seconds))

        return stats
