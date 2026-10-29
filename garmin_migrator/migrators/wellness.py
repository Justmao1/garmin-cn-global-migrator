"""Daily 24/7 Wellness & Health Monitoring (.fit) Migrator.

Migrates continuous device-recorded health metrics:
- Steps (每日步数与距离)
- Sleep stages & Sleep Score raw data (睡眠阶段与深浅睡/REM监测)
- All-day Heart Rate & Resting Heart Rate (全天心率与静息心率)
- All-day Stress & Body Battery (全天压力与身体电量原始监测)
- HRV / Respiration / SpO2 (心率变异性、呼吸频率、血氧原始监测)

How it works:
Calls `GET /download-service/files/wellness/{YYYY-MM-DD}` on the source account to
download the daily monitoring `.zip` archive, extracts all `.fit` files in memory,
and uploads each `.fit` file to the target account via `/upload-service/upload`.
"""

from __future__ import annotations

import io
import logging
import time
import zipfile
from datetime import date, datetime, timedelta
from typing import Any

from garminconnect import Garmin, GarminConnectNotFoundError

from ..config import MigratorConfig
from ..ledger import MigrationLedger
from ..upload_helper import (
    GarminInactiveWellnessDeviceError,
    GarminUploadConsentError,
    upload_bytes_to_garmin,
)

logger = logging.getLogger(__name__)


def generate_date_range(
    start_date_str: str,
    end_date_str: str,
    *,
    oldest_first: bool = True,
) -> list[str]:
    start_d = date.fromisoformat(start_date_str)
    end_d = date.fromisoformat(end_date_str)
    if start_d > end_d:
        start_d, end_d = end_d, start_d

    days_count = (end_d - start_d).days + 1
    dates = [
        (start_d + timedelta(days=offset)).isoformat() for offset in range(days_count)
    ]
    if not oldest_first:
        dates.reverse()
    return dates


class WellnessMigrator:
    """Migrates daily 24/7 wellness monitoring .fit archives between Garmin regions."""

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

    @staticmethod
    def extract_fit_entries_from_zip(raw_zip_bytes: bytes) -> list[tuple[str, bytes]]:
        """Return list of `(filename, fit_bytes)` from a daily wellness `.zip` payload."""
        if not raw_zip_bytes or len(raw_zip_bytes) < 4 or raw_zip_bytes[:2] != b"PK":
            return []

        results: list[tuple[str, bytes]] = []
        try:
            with zipfile.ZipFile(io.BytesIO(raw_zip_bytes)) as zf:
                for member in zf.infolist():
                    if member.is_dir():
                        continue
                    if member.filename.lower().endswith(".fit"):
                        base_name = member.filename.replace("\\", "/").rsplit("/", 1)[-1]
                        fit_data = zf.read(member.filename)
                        if fit_data:
                            results.append((base_name, fit_data))
        except zipfile.BadZipFile:
            logger.debug("Invalid ZIP payload when extracting wellness FIT files")
        return results

    def migrate_single_day(
        self, calendar_date: str, *, dry_run: bool = False
    ) -> dict[str, Any]:
        """Download and upload all wellness `.fit` files for `calendar_date` (YYYY-MM-DD)."""
        if self.ledger.is_wellness_day_done(self.direction, calendar_date):
            return {
                "date": calendar_date,
                "status": "skipped",
                "total": 0,
                "uploaded": 0,
                "duplicate": 0,
                "failed": 0,
            }

        if dry_run:
            return {
                "date": calendar_date,
                "status": "dry_run",
                "total": 0,
                "uploaded": 0,
                "duplicate": 0,
                "failed": 0,
            }

        try:
            raw_zip = self.source.download_health_snapshot(calendar_date)
        except GarminConnectNotFoundError:
            self.ledger.record_wellness_day(
                self.direction, calendar_date, status="empty"
            )
            return {
                "date": calendar_date,
                "status": "empty",
                "total": 0,
                "uploaded": 0,
                "duplicate": 0,
                "failed": 0,
            }
        except Exception as err:
            err_str = str(err)
            if "404" in err_str or "204" in err_str:
                self.ledger.record_wellness_day(
                    self.direction, calendar_date, status="empty"
                )
                return {
                    "date": calendar_date,
                    "status": "empty",
                    "total": 0,
                    "uploaded": 0,
                    "duplicate": 0,
                    "failed": 0,
                }
            self.ledger.record_wellness_day(
                self.direction,
                calendar_date,
                status="failed",
                error_msg=err_str,
            )
            return {
                "date": calendar_date,
                "status": "failed",
                "total": 0,
                "uploaded": 0,
                "duplicate": 0,
                "failed": 1,
                "error": err_str,
            }

        fit_entries = self.extract_fit_entries_from_zip(raw_zip)
        if not fit_entries:
            self.ledger.record_wellness_day(
                self.direction, calendar_date, status="empty"
            )
            return {
                "date": calendar_date,
                "status": "empty",
                "total": 0,
                "uploaded": 0,
                "duplicate": 0,
                "failed": 0,
            }

        uploaded = 0
        duplicate = 0
        failed = 0
        last_err = ""

        for fname, fit_bytes in fit_entries:
            # Prefix with calendar_date so filenames are distinct in logs
            upload_name = f"wellness_{calendar_date}_{fname}"
            try:
                outcome = upload_bytes_to_garmin(
                    self.target,
                    fit_bytes,
                    upload_name,
                    is_wellness=True,
                )
                if outcome.status == "uploaded":
                    uploaded += 1
                elif outcome.status == "duplicate":
                    duplicate += 1
                else:
                    failed += 1
                    last_err = outcome.message
            except (GarminInactiveWellnessDeviceError, GarminUploadConsentError):
                # Fatal account precondition errors: re-raise immediately to stop batch
                raise
            except Exception as err:
                failed += 1
                last_err = str(err)

            if self.config.upload_delay_seconds > 0:
                time.sleep(min(0.8, self.config.upload_delay_seconds))

        day_status = "done" if failed == 0 else ("partial" if (uploaded + duplicate) > 0 else "failed")
        self.ledger.record_wellness_day(
            self.direction,
            calendar_date,
            total_files=len(fit_entries),
            uploaded_files=uploaded,
            duplicate_files=duplicate,
            failed_files=failed,
            status=day_status,
            error_msg=last_err,
        )
        return {
            "date": calendar_date,
            "status": day_status,
            "total": len(fit_entries),
            "uploaded": uploaded,
            "duplicate": duplicate,
            "failed": failed,
            "error": last_err,
        }

    def migrate_date_range(
        self,
        start_date: str,
        end_date: str | None = None,
        *,
        oldest_first: bool = True,
        dry_run: bool = False,
    ) -> dict[str, int]:
        """Migrate daily wellness monitoring files across `[start_date, end_date]`."""
        end_date_str = end_date or datetime.now().date().isoformat()
        dates = generate_date_range(start_date, end_date_str, oldest_first=oldest_first)
        total_days = len(dates)

        stats = {
            "total_days": total_days,
            "synced_days": 0,
            "empty_days": 0,
            "skipped_days": 0,
            "failed_days": 0,
            "uploaded_files": 0,
            "duplicate_files": 0,
        }

        logger.info(
            "💓 开始迁移每日健康监测数据 (Wellness .fit): %s -> %s (共 %d 天)",
            dates[0] if dates else start_date,
            dates[-1] if dates else end_date_str,
            total_days,
        )

        for idx, cdate in enumerate(dates, 1):
            res = self.migrate_single_day(cdate, dry_run=dry_run)
            st = res["status"]

            if st == "skipped":
                stats["skipped_days"] += 1
                logger.debug("[%d/%d] ⏭ %s 已迁移，跳过", idx, total_days, cdate)
                continue
            elif st == "dry_run":
                stats["synced_days"] += 1
                logger.info("[%d/%d] 🔍 [DRY-RUN] 将迁移日期: %s", idx, total_days, cdate)
                continue
            elif st == "empty":
                stats["empty_days"] += 1
                logger.info("[%d/%d] ⚪ %s 无设备健康监测文件", idx, total_days, cdate)
            elif st in ("done", "partial"):
                stats["synced_days"] += 1
                stats["uploaded_files"] += res["uploaded"]
                stats["duplicate_files"] += res["duplicate"]
                if res["failed"] > 0:
                    stats["failed_days"] += 1
                logger.info(
                    "[%d/%d] ✅ %s 健康数据包完成: 共 %d 个文件 (新增=%d, 重复=%d, 失败=%d)",
                    idx,
                    total_days,
                    cdate,
                    res["total"],
                    res["uploaded"],
                    res["duplicate"],
                    res["failed"],
                )
            else:
                stats["failed_days"] += 1
                logger.error(
                    "[%d/%d] ❌ %s 健康数据迁移失败: %s",
                    idx,
                    total_days,
                    cdate,
                    res.get("error", ""),
                )

            if self.config.wellness_day_delay_seconds > 0 and idx < total_days:
                time.sleep(self.config.wellness_day_delay_seconds)

        logger.info(
            "🏁 每日健康数据迁移结束: 总天数=%d, 有效天数=%d (新增文件=%d, 重复文件=%d), 空白天数=%d, 跳过天数=%d, 失败天数=%d",
            stats["total_days"],
            stats["synced_days"],
            stats["uploaded_files"],
            stats["duplicate_files"],
            stats["empty_days"],
            stats["skipped_days"],
            stats["failed_days"],
        )
        return stats
