"""Gear (跑鞋/单车装备 + 活动装备关联重建) & Custom Workouts (自定义训练计划) Migrator."""

from __future__ import annotations

import logging
import time
from typing import Any

from garminconnect import Garmin

from ..config import MigratorConfig
from ..ledger import MigrationLedger

logger = logging.getLogger(__name__)


class GearAndWorkoutsMigrator:
    """Migrates Gear (and re-links activities so shoe mileage transfers) & Workout templates."""

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
    #  1. Gear (Shoes / Bikes / Other) + Activity Relinking              #
    # ------------------------------------------------------------------ #

    def migrate_gear(
        self, *, relink_activities: bool = True, dry_run: bool = False
    ) -> dict[str, int]:
        """Migrate all active and retired gear and optionally re-link migrated activities."""
        logger.info("👟 正在拉取源端装备 (跑鞋/单车等) 列表...")
        try:
            src_gears = self.source.get_gear_list(start=0, limit=100) or []
        except Exception as e:
            logger.warning("⚠️ 无法通过 v2 接口获取装备列表: %s", e)
            src_gears = []

        if not isinstance(src_gears, list) or not src_gears:
            logger.info("✓ 源端没有配置任何装备信息。")
            return {"total": 0, "created": 0, "matched": 0, "relinked_activities": 0, "failed": 0}

        try:
            tgt_gears = self.target.get_gear_list(start=0, limit=100) or []
        except Exception:
            tgt_gears = []

        # Index existing target gear by (name.lower(), brand.lower(), model.lower())
        target_by_identity: dict[tuple[str, str, str], str] = {}
        for tg in tgt_gears:
            if isinstance(tg, dict) and tg.get("uuid"):
                key = (
                    str(tg.get("name") or "").strip().lower(),
                    str(tg.get("brand") or "").strip().lower(),
                    str(tg.get("model") or "").strip().lower(),
                )
                target_by_identity[key] = str(tg["uuid"])

        stats = {
            "total": len(src_gears),
            "created": 0,
            "matched": 0,
            "relinked_activities": 0,
            "failed": 0,
        }

        activity_id_map = self.ledger.get_activity_id_map(self.direction)

        for idx, g in enumerate(src_gears, 1):
            if not isinstance(g, dict) or not g.get("uuid"):
                continue
            src_uuid = str(g["uuid"])
            name = str(g.get("name") or g.get("model") or "未命名装备").strip()
            brand = str(g.get("brand") or "Garmin").strip()
            model = str(g.get("model") or name).strip()
            gear_type = str(g.get("gearType") or "SHOES").strip().upper()
            first_use = str(g.get("firstUseDate") or "2024-01-01")[:10]
            usage_type = str(g.get("usageType") or "DISTANCE").strip().upper()
            max_dist_m = float(g.get("maxUsageDistanceMeters") or 0.0)
            max_dur_s = float(g.get("maxUsageDurationSeconds") or 0.0)
            notes = str(g.get("notes") or "")
            gear_status = str(g.get("gearStatus") or "ACTIVE").strip().upper()
            assoc_types = [
                str(a.get("activityTypeKey"))
                for a in (g.get("associatedActivityTypes") or [])
                if isinstance(a, dict) and a.get("activityTypeKey")
            ]

            target_uuid = self.ledger.get_gear_target_uuid(self.direction, src_uuid)
            ident_key = (name.lower(), brand.lower(), model.lower())

            if not target_uuid and ident_key in target_by_identity:
                target_uuid = target_by_identity[ident_key]
                stats["matched"] += 1
                self.ledger.record_gear(
                    self.direction,
                    src_uuid,
                    target_uuid,
                    name,
                    gear_type,
                    status="matched",
                )
                logger.info("[%d/%d] ⏭ 目标端已存在同名装备: 【%s】 (%s %s)", idx, len(src_gears), name, brand, model)
            elif target_uuid:
                stats["matched"] += 1
            elif dry_run:
                logger.info(
                    "[%d/%d] 🔍 [DRY-RUN] 将创建装备: 【%s】 (%s %s, 类型=%s, 状态=%s)",
                    idx,
                    len(src_gears),
                    name,
                    brand,
                    model,
                    gear_type,
                    gear_status,
                )
                stats["created"] += 1
                continue
            else:
                try:
                    created = self.target.create_gear(
                        gear_type=gear_type,
                        brand=brand,
                        model=model,
                        name=name,
                        first_use_date=first_use,
                        usage_type=usage_type,
                        max_usage_distance_km=(max_dist_m / 1000.0) if max_dist_m > 0 else None,
                        max_usage_duration_min=(max_dur_s / 60.0) if max_dur_s > 0 else None,
                        notes=notes,
                        activity_type_keys=assoc_types or None,
                    )
                    if isinstance(created, dict) and created.get("uuid"):
                        target_uuid = str(created["uuid"])
                    if target_uuid and gear_status == "RETIRED":
                        self.target.set_gear_status(target_uuid, "RETIRED")
                    stats["created"] += 1
                    self.ledger.record_gear(
                        self.direction,
                        src_uuid,
                        target_uuid,
                        name,
                        gear_type,
                        status="created",
                    )
                    logger.info(
                        "[%d/%d] ✅ 装备创建成功: 【%s】 (%s %s) -> UUID=%s",
                        idx,
                        len(src_gears),
                        name,
                        brand,
                        model,
                        target_uuid,
                    )
                except Exception as err:
                    stats["failed"] += 1
                    self.ledger.record_gear(
                        self.direction,
                        src_uuid,
                        None,
                        name,
                        gear_type,
                        status="failed",
                        error_msg=str(err),
                    )
                    logger.error("[%d/%d] ❌ 创建装备 【%s】 失败: %s", idx, len(src_gears), name, err)
                    continue

            # Re-link activities that used this gear so cumulative mileage is preserved!
            if relink_activities and target_uuid and not dry_run and activity_id_map:
                try:
                    src_gear_no_dash = src_uuid.replace("-", "")
                    gear_acts = self.source.get_gear_activities(src_gear_no_dash, limit=1000) or []
                    if not gear_acts:
                        gear_acts = self.source.get_gear_activities(src_uuid, limit=1000) or []
                    tgt_gear_no_dash = target_uuid.replace("-", "")
                    for ga in gear_acts:
                        if not isinstance(ga, dict) or not ga.get("activityId"):
                            continue
                        s_aid = int(ga["activityId"])
                        t_aid = activity_id_map.get(s_aid)
                        if not t_aid:
                            continue
                        try:
                            self.target.add_gear_to_activity(tgt_gear_no_dash, t_aid)
                            stats["relinked_activities"] += 1
                        except Exception:
                            try:
                                self.target.add_gear_to_activity(target_uuid, t_aid)
                                stats["relinked_activities"] += 1
                            except Exception:
                                pass
                except Exception as ge:
                    logger.debug("跳过装备活动关联 (%s): %s", name, ge)

        return stats

    # ------------------------------------------------------------------ #
    #  2. Custom Workout Templates (自定义训练计划)                        #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _clean_workout_payload_for_upload(workout_detail: dict[str, Any]) -> dict[str, Any]:
        """Strip account/region-specific primary keys from a workout template before re-uploading."""
        cleaned = dict(workout_detail)
        for drop_key in (
            "workoutId",
            "ownerId",
            "createdDate",
            "updatedDate",
            "author",
            "shared",
            "consumer",
        ):
            cleaned.pop(drop_key, None)

        segments = []
        for seg in cleaned.get("workoutSegments") or []:
            if not isinstance(seg, dict):
                continue
            s_copy = dict(seg)
            steps = []
            for step in s_copy.get("workoutSteps") or []:
                if isinstance(step, dict):
                    st_copy = dict(step)
                    st_copy.pop("stepId", None)
                    if "workoutSteps" in st_copy and isinstance(
                        st_copy["workoutSteps"], list
                    ):
                        sub_steps = []
                        for sub in st_copy["workoutSteps"]:
                            if isinstance(sub, dict):
                                sub_c = dict(sub)
                                sub_c.pop("stepId", None)
                                sub_steps.append(sub_c)
                        st_copy["workoutSteps"] = sub_steps
                    steps.append(st_copy)
            s_copy["workoutSteps"] = steps
            segments.append(s_copy)
        cleaned["workoutSegments"] = segments
        return cleaned

    def migrate_workouts(self, *, dry_run: bool = False) -> dict[str, int]:
        """Migrate user-created custom workouts from source to target."""
        logger.info("📋 正在拉取源端自定义训练计划 (Workouts)...")
        try:
            src_workouts = self.source.get_workouts(start=0, limit=100) or []
        except Exception as e:
            logger.warning("⚠️ 拉取训练计划列表失败: %s", e)
            src_workouts = []

        if not isinstance(src_workouts, list) or not src_workouts:
            logger.info("✓ 源端没有自定义训练计划。")
            return {"total": 0, "uploaded": 0, "skipped": 0, "failed": 0}

        try:
            tgt_workouts = self.target.get_workouts(start=0, limit=100) or []
        except Exception:
            tgt_workouts = []

        existing_names = {
            str(w.get("workoutName") or "").strip().lower()
            for w in tgt_workouts
            if isinstance(w, dict) and w.get("workoutName")
        }

        stats = {"total": len(src_workouts), "uploaded": 0, "skipped": 0, "failed": 0}
        for idx, w in enumerate(src_workouts, 1):
            if not isinstance(w, dict) or not w.get("workoutId"):
                continue
            wid = int(w["workoutId"])
            wname = str(w.get("workoutName") or f"Workout_{wid}").strip()

            if self.ledger.is_workout_done(self.direction, wid) or wname.lower() in existing_names:
                stats["skipped"] += 1
                self.ledger.record_workout(self.direction, wid, None, wname, status="matched")
                continue

            if dry_run:
                logger.info("[%d/%d] 🔍 [DRY-RUN] 将迁移训练计划: 【%s】 (ID=%d)", idx, len(src_workouts), wname, wid)
                stats["uploaded"] += 1
                continue

            try:
                detail = self.source.get_workout_by_id(wid)
                if not isinstance(detail, dict):
                    raise RuntimeError("Empty workout detail")
                payload = self._clean_workout_payload_for_upload(detail)
                created = self.target.upload_workout(payload)
                new_wid = (
                    int(created["workoutId"])
                    if isinstance(created, dict) and created.get("workoutId")
                    else None
                )
                stats["uploaded"] += 1
                existing_names.add(wname.lower())
                self.ledger.record_workout(
                    self.direction, wid, new_wid, wname, status="uploaded"
                )
                logger.info(
                    "[%d/%d] ✅ 训练计划迁移成功: 【%s】 (%d -> %s)",
                    idx,
                    len(src_workouts),
                    wname,
                    wid,
                    new_wid or "?",
                )
            except Exception as err:
                stats["failed"] += 1
                self.ledger.record_workout(
                    self.direction, wid, None, wname, status="failed", error_msg=str(err)
                )
                logger.error("[%d/%d] ❌ 训练计划 【%s】 迁移失败: %s", idx, len(src_workouts), wname, err)

            if self.config.upload_delay_seconds > 0:
                time.sleep(min(0.8, self.config.upload_delay_seconds))

        return stats
