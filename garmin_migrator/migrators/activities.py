"""Full-Fidelity Activity Migrator with 4-tier format fallback and metadata restoration.

Features:
1. Paginated fetching of all source & target activities.
2. Fast pre-flight deduplication using `startTimeLocal|round(duration)` + SQLite ledger.
3. 4-Tier Download/Upload Fallback:
   - Tier 1: Original `.fit` archive (`GET /download-service/files/activity/{id}`) -> in-memory ZIP extraction
   - Tier 2: `.tcx` export (`GET /download-service/export/tcx/activity/{id}`)
   - Tier 3: `.gpx` export (`GET /download-service/export/gpx/activity/{id}`)
   - Tier 4: Manual JSON activity creation (`POST /activity-service/activity`) for manual/trackless entries
4. Post-Upload Metadata Restoration (`PUT /activity-service/activity/{target_id}`):
   - Restores custom `activityName`, `description` (notes), `favorite` star, `eventTypeDTO`
   - For `strength_training`, restores `exerciseSets` (custom reps/weights/movements)
"""

from __future__ import annotations

import io
import logging
import time
import zipfile
from datetime import datetime
from typing import Any

from garminconnect import Garmin

from ..config import MigratorConfig
from ..ledger import MigrationLedger
from ..upload_helper import upload_bytes_to_garmin

logger = logging.getLogger(__name__)


def _activity_dedup_key(act: dict[str, Any]) -> str:
    start_local = str(act.get("startTimeLocal") or "").strip()
    duration = round(float(act.get("duration") or 0.0))
    return f"{start_local}|{duration}"


def _extract_fit_from_zip_bytes(raw_bytes: bytes, fallback_id: int) -> tuple[bytes, str]:
    """If `raw_bytes` is a PK ZIP archive, extract the first `.fit` entry; otherwise return as-is."""
    if len(raw_bytes) > 4 and raw_bytes[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            for entry in zf.namelist():
                if entry.lower().endswith(".fit"):
                    return zf.read(entry), Path_basename(entry)
            # If no .fit extension found, take the first file in the archive
            names = zf.namelist()
            if names:
                return zf.read(names[0]), f"{fallback_id}.fit"
    return raw_bytes, f"{fallback_id}.fit"


def Path_basename(p: str) -> str:
    return p.replace("\\", "/").rsplit("/", 1)[-1]


class ActivityMigrator:
    """Migrates all workout/sport activities between Garmin regions."""

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

    def fetch_all_activities(
        self,
        client: Garmin,
        *,
        start_offset: int = 0,
        max_count: int | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch all activities from `client` using pagination (100 per page)."""
        if start_date:
            acts = client.get_activities_by_date(
                startdate=start_date,
                enddate=end_date,
            )
            return acts[:max_count] if max_count else acts

        all_acts: list[dict[str, Any]] = []
        page_size = 100
        offset = max(0, start_offset)

        while True:
            limit = page_size
            if max_count is not None:
                remaining = max_count - len(all_acts)
                if remaining <= 0:
                    break
                limit = min(page_size, remaining)

            batch = client.get_activities(offset, limit)
            if not batch or not isinstance(batch, list):
                break

            all_acts.extend(batch)
            offset += len(batch)
            if len(batch) < limit:
                break

        return all_acts

    def _download_activity_with_fallback(
        self, act: dict[str, Any]
    ) -> tuple[bytes | None, str, str]:
        """Try downloading activity as ORIGINAL (.fit), then .tcx, then .gpx.

        Returns `(file_bytes, filename, format_str)`.
        If all file downloads fail (e.g. manual activity with no file), returns `(None, "", "manual")`.
        """
        act_id = int(act["activityId"])

        # Tier 1: Original .fit (inside ZIP)
        try:
            raw = self.source.download_activity(
                str(act_id), self.source.ActivityDownloadFormat.ORIGINAL
            )
            if raw and len(raw) > 0:
                fit_bytes, fname = _extract_fit_from_zip_bytes(raw, act_id)
                if not fname.lower().endswith(".fit"):
                    fname = f"{act_id}.fit"
                return fit_bytes, fname, "fit"
        except Exception as e:
            logger.debug("活动 %d 原始 FIT 下载不可用 (%s)，尝试 TCX 回退...", act_id, e)

        # Tier 2: TCX export
        try:
            tcx_bytes = self.source.download_activity(
                str(act_id), self.source.ActivityDownloadFormat.TCX
            )
            if tcx_bytes and b"<TotalTimeSeconds" in tcx_bytes:
                return tcx_bytes, f"activity_{act_id}.tcx", "tcx"
        except Exception as e:
            logger.debug("活动 %d TCX 下载不可用 (%s)，尝试 GPX 回退...", act_id, e)

        # Tier 3: GPX export
        try:
            gpx_bytes = self.source.download_activity(
                str(act_id), self.source.ActivityDownloadFormat.GPX
            )
            if gpx_bytes and b"<trkpt" in gpx_bytes:
                return gpx_bytes, f"activity_{act_id}.gpx", "gpx"
        except Exception as e:
            logger.debug("活动 %d GPX 下载不可用 (%s)，将使用手动 JSON 活动重建...", act_id, e)

        return None, "", "manual"

    def _create_manual_fallback(self, act: dict[str, Any]) -> int | None:
        """Create a manual activity on target when no downloadable file exists."""
        type_dto = act.get("activityType") or {}
        type_key = type_dto.get("typeKey") or "other"
        start_local = str(act.get("startTimeLocal") or "")
        # Convert "YYYY-MM-DD HH:MM:SS" -> "YYYY-MM-DDTHH:MM:SS.000"
        if " " in start_local and "T" not in start_local:
            start_iso = start_local.replace(" ", "T") + ".000"
        else:
            start_iso = start_local

        tz_name = self.source.get_user_timezone() or "Asia/Shanghai"
        distance_m = float(act.get("distance") or 0.0)
        duration_s = float(act.get("duration") or 0.0)
        name = str(act.get("activityName") or type_key)

        payload: dict[str, Any] = {
            "activityTypeDTO": {"typeKey": type_key},
            "accessControlRuleDTO": {"typeId": 2, "typeKey": "private"},
            "timeZoneUnitDTO": {"unitKey": tz_name},
            "activityName": name,
            "metadataDTO": {"autoCalcCalories": True},
            "summaryDTO": {
                "startTimeLocal": start_iso,
                "distance": distance_m,
                "duration": duration_s,
            },
        }
        if act.get("calories"):
            payload["summaryDTO"]["calories"] = act.get("calories")
        if act.get("averageHR"):
            payload["summaryDTO"]["averageHR"] = act.get("averageHR")
        if act.get("maxHR"):
            payload["summaryDTO"]["maxHR"] = act.get("maxHR")

        resp = self.target.create_manual_activity_from_json(payload)
        if hasattr(resp, "json"):
            try:
                data = resp.json()
                if isinstance(data, dict) and data.get("activityId"):
                    return int(data["activityId"])
            except Exception:
                pass
        elif isinstance(resp, dict) and resp.get("activityId"):
            return int(resp["activityId"])
        return None

    def _recover_target_activity_id(self, start_time_local: str) -> int | None:
        """Find the newly uploaded activity ID on the target account by startTimeLocal proximity."""
        if not start_time_local:
            return None
        try:
            recent = self.target.get_activities(0, 20)
            if not isinstance(recent, list):
                return None
            target_dt = datetime.fromisoformat(start_time_local.replace(" ", "T"))
            for item in recent:
                if not isinstance(item, dict):
                    continue
                cand_str = str(item.get("startTimeLocal") or "")
                if not cand_str:
                    continue
                cand_dt = datetime.fromisoformat(cand_str.replace(" ", "T"))
                if abs((cand_dt - target_dt).total_seconds()) <= 180:
                    return int(item["activityId"])
        except Exception as e:
            logger.debug("Could not recover target activity ID: %s", e)
        return None

    def _sync_activity_metadata(
        self, source_act: dict[str, Any], target_id: int
    ) -> bool:
        """Sync custom name, description, favorite flag, event type, and strength sets to target activity."""
        source_id = int(source_act["activityId"])
        fields_to_update: dict[str, Any] = {}

        # 1. Activity Name (preserve custom Chinese/user-defined title)
        src_name = str(source_act.get("activityName") or "").strip()
        if src_name:
            fields_to_update["activityName"] = src_name

        # 2. Description / Notes
        src_desc = source_act.get("description")
        if src_desc and isinstance(src_desc, str) and src_desc.strip():
            fields_to_update["description"] = src_desc.strip()

        # 3. Favorite status
        is_fav = bool(source_act.get("favorite"))
        if is_fav:
            fields_to_update["metadataDTO"] = {"favorite": True}

        # 4. Event Type (e.g. race, workout,uncategorized)
        event_type = source_act.get("eventType")
        if (
            isinstance(event_type, dict)
            and event_type.get("typeKey")
            and event_type.get("typeKey") != "uncategorized"
        ):
            fields_to_update["eventTypeDTO"] = {
                "typeId": event_type.get("typeId"),
                "typeKey": event_type.get("typeKey"),
            }

        try:
            if fields_to_update:
                self.target.update_activity(target_id, fields_to_update)

            # 5. If strength_training, also sync exercise sets if customized
            act_type_key = (source_act.get("activityType") or {}).get("typeKey")
            if act_type_key == "strength_training":
                try:
                    sets_data = self.source.get_activity_exercise_sets(source_id)
                    if (
                        isinstance(sets_data, dict)
                        and sets_data.get("exerciseSets")
                    ):
                        payload = {**sets_data, "activityId": target_id}
                        self.target.set_activity_exercise_sets(target_id, payload)
                except Exception as se:
                    logger.debug(
                        "跳过力量训练动作组同步 (source=%d -> target=%d): %s",
                        source_id,
                        target_id,
                        se,
                    )
            return True
        except Exception as e:
            logger.warning(
                "⚠️ 同步活动元数据失败 (source=%d -> target=%d): %s",
                source_id,
                target_id,
                e,
            )
            return False

    def migrate(
        self,
        *,
        start_offset: int = 0,
        max_count: int | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        oldest_first: bool = True,
        dry_run: bool = False,
    ) -> dict[str, int]:
        """Run activity migration with pre-flight deduplication and checkpointing."""
        logger.info("📡 正在拉取源端 (%s) 运动列表...", self.config.source_region.upper())
        source_acts = self.fetch_all_activities(
            self.source,
            start_offset=start_offset,
            max_count=max_count,
            start_date=start_date,
            end_date=end_date,
        )
        total = len(source_acts)
        if total == 0:
            logger.info("✓ 源端没有找到待迁移的运动记录。")
            return {"total": 0, "uploaded": 0, "duplicate": 0, "failed": 0}

        logger.info("📡 正在拉取目标端 (%s) 已有运动列表用于快速查重...", self.config.target_region.upper())
        target_acts = self.fetch_all_activities(self.target, max_count=2000)
        target_lookup: dict[str, int] = {}
        for tact in target_acts:
            if isinstance(tact, dict) and tact.get("activityId"):
                target_lookup[_activity_dedup_key(tact)] = int(tact["activityId"])

        if oldest_first:
            source_acts = list(reversed(source_acts))

        stats = {"total": total, "uploaded": 0, "duplicate": 0, "failed": 0}
        logger.info("🚀 共发现 %d 条运动记录，开始处理 (oldest_first=%s)...", total, oldest_first)

        for idx, act in enumerate(source_acts, 1):
            source_id = int(act["activityId"])
            act_name = str(act.get("activityName") or "未命名运动")
            act_type = str((act.get("activityType") or {}).get("typeKey") or "unknown")
            start_local = str(act.get("startTimeLocal") or "")
            duration = float(act.get("duration") or 0.0)
            dedup_key = _activity_dedup_key(act)

            # 1. Check SQLite Ledger first
            if self.ledger.is_activity_done(
                self.direction,
                source_id,
                require_metadata=self.config.sync_activity_metadata,
            ):
                stats["duplicate"] += 1
                logger.info(
                    "[%d/%d] ⏭ 账本已记录完成，跳过: 【%s】 (%s) ID=%d",
                    idx,
                    total,
                    act_name,
                    start_local,
                    source_id,
                )
                continue

            # 2. Check Target Pre-flight Lookup (startTimeLocal|round(duration))
            if dedup_key in target_lookup:
                existing_target_id = target_lookup[dedup_key]
                meta_ok = False
                if not dry_run and self.config.sync_activity_metadata:
                    meta_ok = self._sync_activity_metadata(act, existing_target_id)
                if not dry_run:
                    self.ledger.record_activity(
                        self.direction,
                        source_id,
                        target_id=existing_target_id,
                        activity_name=act_name,
                        activity_type=act_type,
                        start_time_local=start_local,
                        duration=duration,
                        file_format="existing",
                        status="duplicate",
                        metadata_synced=meta_ok,
                    )
                stats["duplicate"] += 1
                logger.info(
                    "[%d/%d] ⏭ 目标端已存在相同时间运动，跳过上传%s: 【%s】 (%s) %d -> %d",
                    idx,
                    total,
                    " (已补齐名称/备注)" if meta_ok else "",
                    act_name,
                    start_local,
                    source_id,
                    existing_target_id,
                )
                continue

            if dry_run:
                logger.info(
                    "[%d/%d] 🔍 [DRY-RUN] 将迁移: 【%s】 类型=%s 开始时间=%s 时长=%.0fs ID=%d",
                    idx,
                    total,
                    act_name,
                    act_type,
                    start_local,
                    duration,
                    source_id,
                )
                stats["uploaded"] += 1
                continue

            # 3. Download from Source & Upload to Target
            try:
                file_bytes, filename, fmt = self._download_activity_with_fallback(act)

                if fmt == "manual" or file_bytes is None:
                    logger.info(
                        "[%d/%d] 📝 活动无原始轨迹文件，使用 JSON 重建手动活动: 【%s】 (%s)",
                        idx,
                        total,
                        act_name,
                        start_local,
                    )
                    target_id = self._create_manual_fallback(act)
                    status = "uploaded"
                else:
                    outcome = upload_bytes_to_garmin(
                        self.target, file_bytes, filename, is_wellness=False
                    )
                    status = outcome.status
                    target_id = outcome.target_activity_id

                    if status == "error":
                        raise RuntimeError(outcome.message or "Upload failed")

                # Recover target_id if upload succeeded/duplicated without returning internalId
                if not target_id:
                    target_id = self._recover_target_activity_id(start_local)

                # 4. Sync custom metadata (name, description, favorite, exerciseSets)
                meta_ok = False
                if target_id and self.config.sync_activity_metadata:
                    meta_ok = self._sync_activity_metadata(act, target_id)

                self.ledger.record_activity(
                    self.direction,
                    source_id,
                    target_id=target_id,
                    activity_name=act_name,
                    activity_type=act_type,
                    start_time_local=start_local,
                    duration=duration,
                    file_format=fmt,
                    status=status,
                    metadata_synced=meta_ok,
                )
                if target_id:
                    target_lookup[dedup_key] = target_id

                if status == "duplicate":
                    stats["duplicate"] += 1
                    logger.info(
                        "[%d/%d] ⏭ 重复跳过 (%s): 【%s】 (%s) %d -> %s",
                        idx,
                        total,
                        fmt.upper(),
                        act_name,
                        start_local,
                        source_id,
                        target_id or "?",
                    )
                else:
                    stats["uploaded"] += 1
                    logger.info(
                        "[%d/%d] ✅ 迁移成功 (%s%s): 【%s】 (%s) %d -> %s",
                        idx,
                        total,
                        fmt.upper(),
                        "+元数据" if meta_ok else "",
                        act_name,
                        start_local,
                        source_id,
                        target_id or "?",
                    )

            except Exception as err:
                stats["failed"] += 1
                err_msg = str(err)
                self.ledger.record_activity(
                    self.direction,
                    source_id,
                    activity_name=act_name,
                    activity_type=act_type,
                    start_time_local=start_local,
                    duration=duration,
                    status="failed",
                    error_msg=err_msg,
                )
                logger.error(
                    "[%d/%d] ❌ 迁移失败: 【%s】 (%s) ID=%d 错误: %s",
                    idx,
                    total,
                    act_name,
                    start_local,
                    source_id,
                    err_msg,
                )

            if self.config.upload_delay_seconds > 0 and idx < total:
                time.sleep(self.config.upload_delay_seconds)

        logger.info(
            "🏁 运动迁移结束: 总计=%d, 新增上传=%d, 重复跳过=%d, 失败=%d",
            stats["total"],
            stats["uploaded"],
            stats["duplicate"],
            stats["failed"],
        )
        return stats
