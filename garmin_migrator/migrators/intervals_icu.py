"""Intervals.icu Direct Sync & Historical Importer (Activities + Full Wellness).

Directly pushes historical and incremental data from Garmin (China `garmin.cn` or
Global `garmin.com`) into Intervals.icu (`https://intervals.icu`) via its official
REST API using Personal API Key authentication (`Basic Auth: API_KEY:<key>`).

Why direct sync to Intervals.icu?
1. Garmin's official third-party webhook only pushes *newly recorded* device data
   to Intervals.icu, ignoring historical `.fit` files uploaded/migrated via web/API.
2. Intervals.icu's `/wellness-bulk` endpoint natively accepts structured JSON for
   `restingHR`, `hrv` (rMSSD), `sleepSecs`, `sleepScore`, `sleepQuality`,
   `avgSleepingHR`, `spO2`, `respiration`, `weight`, `bodyFat`, `steps`,
   `systolic`, `diastolic`, `hydrationVolume`, `stress`, and `vo2max` — bypassing
   Garmin's binary FIT restrictions and populating all Intervals.icu charts directly!
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Any

import requests
from garminconnect import Garmin

from ..config import MigratorConfig
from ..ledger import MigrationLedger
from .activities import ActivityMigrator, _extract_fit_from_zip_bytes

logger = logging.getLogger(__name__)

INTERVALS_API_BASE = "https://intervals.icu/api/v1"

# Garmin sleep qualifierKey -> Intervals.icu 1..4 scale (1=Great, 2=Good, 3=Avg, 4=Poor)
SLEEP_QUALIFIER_MAP: dict[str, int] = {
    "EXCELLENT": 1,
    "GOOD": 2,
    "FAIR": 3,
    "AVERAGE": 3,
    "POOR": 4,
}


def _garmin_stress_to_intervals(stress_val: Any) -> int | None:
    """Map Garmin 1..100 stress score to Intervals.icu 1..4 scale (1=Low, 2=Avg, 3=High, 4=Extreme)."""
    try:
        s = float(stress_val)
    except (TypeError, ValueError):
        return None
    if s <= 0 or s > 100:
        return None
    if s <= 25:
        return 1
    if s <= 50:
        return 2
    if s <= 75:
        return 3
    return 4


def _safe_float(val: Any, ndigits: int = 2) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
        return round(f, ndigits) if f > 0 else None
    except (TypeError, ValueError):
        return None


def _safe_int(val: Any) -> int | None:
    if val is None:
        return None
    try:
        i = int(round(float(val)))
        return i if i > 0 else None
    except (TypeError, ValueError):
        return None


class IntervalsIcuMigrator:
    """Syncs Garmin activities (`.fit`/`.tcx`/`.gpx`) and daily wellness metrics to Intervals.icu."""

    def __init__(
        self,
        source: Garmin,
        ledger: MigrationLedger,
        config: MigratorConfig,
        *,
        source_region: str | None = None,
        athlete_id: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.source = source
        self.ledger = ledger
        self.config = config
        self.source_region = (source_region or config.intervals_source_region or "cn").lower()
        self.athlete_id = (athlete_id or config.intervals_athlete_id or "0").strip() or "0"
        self.api_key = (api_key or config.intervals_api_key or "").strip()

        if not self.api_key:
            raise ValueError(
                "未配置 INTERVALS_API_KEY！请在 .env 文件中设置 INTERVALS_API_KEY，"
                "或通过命令行参数 --api-key 传入（可在 https://intervals.icu -> Settings -> Developer Settings 获取）。"
            )

        self.session = requests.Session()
        self.session.auth = ("API_KEY", self.api_key)
        self.session.headers.update({"Accept": "application/json"})

    # ------------------------------------------------------------------ #
    #  HTTP Helper with Rate-Limit Retry                                 #
    # ------------------------------------------------------------------ #

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_data: Any = None,
        files: dict[str, Any] | None = None,
        timeout: int = 60,
    ) -> requests.Response:
        url = f"{INTERVALS_API_BASE}{path}"
        backoff = [3.0, 8.0, 20.0]

        for attempt in range(len(backoff) + 1):
            resp = self.session.request(
                method,
                url,
                params=params,
                json=json_data,
                files=files,
                timeout=timeout,
            )
            if resp.status_code == 429 and attempt < len(backoff):
                wait_s = float(resp.headers.get("Retry-After") or backoff[attempt])
                logger.warning("⏳ Intervals.icu 触发速率限制 (429)，等待 %.1f 秒后重试...", wait_s)
                time.sleep(wait_s)
                continue
            return resp
        return resp

    def verify_connection(self) -> dict[str, Any]:
        """Verify Intervals.icu API Key and resolve Athlete ID & profile info."""
        resp = self._request("GET", f"/athlete/{self.athlete_id}")
        if resp.status_code in (401, 403):
            raise RuntimeError(
                f"Intervals.icu API Key 认证失败 (HTTP {resp.status_code})，请检查 INTERVALS_API_KEY 是否正确。"
            )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("id"):
            self.athlete_id = str(data["id"])
        return data if isinstance(data, dict) else {"id": self.athlete_id}

    # ------------------------------------------------------------------ #
    #  1. Activities Sync (.fit / .tcx / .gpx)                           #
    # ------------------------------------------------------------------ #

    def _fetch_existing_intervals_activities(
        self, oldest_date: str, newest_date: str
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Return `(by_external_id, by_start_minute)` lookups from Intervals.icu."""
        by_ext: dict[str, str] = {}
        by_time: dict[str, str] = {}
        try:
            resp = self._request(
                "GET",
                f"/athlete/{self.athlete_id}/activities",
                params={"oldest": oldest_date, "newest": newest_date},
            )
            if resp.status_code == 200:
                items = resp.json()
                if isinstance(items, list):
                    for item in items:
                        if not isinstance(item, dict) or not item.get("id"):
                            continue
                        iv_id = str(item["id"])
                        ext_id = str(item.get("external_id") or "").strip()
                        if ext_id:
                            by_ext[ext_id] = iv_id
                        start_local = str(item.get("start_date_local") or "").replace("T", " ")
                        if len(start_local) >= 16:
                            by_time[start_local[:16]] = iv_id
        except Exception as e:
            logger.debug("Could not pre-fetch existing Intervals.icu activities: %s", e)
        return by_ext, by_time

    def _download_garmin_activity_file(
        self, act: dict[str, Any]
    ) -> tuple[bytes | None, str, str]:
        """Download `.fit` -> `.tcx` -> `.gpx` from Garmin source account."""
        act_id = int(act["activityId"])

        # Tier 1: Original .fit inside ZIP
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
            logger.debug("Garmin 活动 %d FIT 下载不可用 (%s)，尝试 TCX...", act_id, e)

        # Tier 2: TCX
        try:
            tcx_bytes = self.source.download_activity(
                str(act_id), self.source.ActivityDownloadFormat.TCX
            )
            if tcx_bytes and b"<TotalTimeSeconds" in tcx_bytes:
                return tcx_bytes, f"activity_{act_id}.tcx", "tcx"
        except Exception as e:
            logger.debug("Garmin 活动 %d TCX 下载不可用 (%s)，尝试 GPX...", act_id, e)

        # Tier 3: GPX
        try:
            gpx_bytes = self.source.download_activity(
                str(act_id), self.source.ActivityDownloadFormat.GPX
            )
            if gpx_bytes and b"<trkpt" in gpx_bytes:
                return gpx_bytes, f"activity_{act_id}.gpx", "gpx"
        except Exception:
            pass

        return None, "", "unavailable"

    def sync_activities(
        self,
        *,
        start_offset: int = 0,
        max_count: int | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        oldest_first: bool = True,
        force: bool = False,
        dry_run: bool = False,
    ) -> dict[str, int]:
        """Download activities from Garmin and upload `.fit`/`.tcx`/`.gpx` to Intervals.icu."""
        logger.info("📡 正在从佳明 (%s) 拉取运动记录列表...", self.source_region.upper())
        helper = ActivityMigrator(self.source, self.source, self.ledger, self.config)
        source_acts = helper.fetch_all_activities(
            self.source,
            start_offset=start_offset,
            max_count=max_count,
            start_date=start_date,
            end_date=end_date,
        )
        total = len(source_acts)
        stats = {"total": total, "uploaded": 0, "duplicate": 0, "failed": 0}
        if total == 0:
            logger.info("✓ 未找到待同步的运动记录。")
            return stats

        if oldest_first:
            source_acts = list(reversed(source_acts))

        # Determine date range for Intervals.icu pre-flight deduplication
        dates = [
            str(a.get("startTimeLocal") or "")[:10]
            for a in source_acts
            if len(str(a.get("startTimeLocal") or "")) >= 10
        ]
        oldest_d = min(dates) if dates else "2010-01-01"
        newest_d = max(dates) if dates else date.today().isoformat()

        logger.info("📡 正在查询 Intervals.icu (%s ~ %s) 已有运动记录用于查重...", oldest_d, newest_d)
        by_ext, by_time = self._fetch_existing_intervals_activities(oldest_d, newest_d)

        for idx, act in enumerate(source_acts, 1):
            source_id = int(act["activityId"])
            act_name = str(act.get("activityName") or "").strip()
            act_desc = str(act.get("description") or "").strip()
            start_local = str(act.get("startTimeLocal") or "").strip()
            ext_id = f"garmin_{self.source_region}_{source_id}"

            if not force and self.ledger.is_intervals_activity_done(self.source_region, source_id):
                stats["duplicate"] += 1
                logger.info(
                    "[%d/%d] ⏭ 账本已记录同步，跳过: 【%s】 (%s) ID=%d",
                    idx,
                    total,
                    act_name or "未命名",
                    start_local,
                    source_id,
                )
                continue

            existing_iv_id = by_ext.get(ext_id) or (
                by_time.get(start_local[:16]) if len(start_local) >= 16 else None
            )
            if not force and existing_iv_id:
                stats["duplicate"] += 1
                if not dry_run:
                    self.ledger.record_intervals_activity(
                        self.source_region,
                        source_id,
                        intervals_id=existing_iv_id,
                        activity_name=act_name,
                        start_time_local=start_local,
                        file_format="existing",
                        status="duplicate",
                    )
                logger.info(
                    "[%d/%d] ⏭ Intervals.icu 已存在相同时间活动 (%s)，跳过: 【%s】 (%s)",
                    idx,
                    total,
                    existing_iv_id,
                    act_name or "未命名",
                    start_local,
                )
                continue

            if dry_run:
                stats["uploaded"] += 1
                logger.info(
                    "[%d/%d] 🔍 [DRY-RUN] 将上传活动至 Intervals.icu: 【%s】 (%s) ID=%d",
                    idx,
                    total,
                    act_name or "未命名",
                    start_local,
                    source_id,
                )
                continue

            try:
                file_bytes, filename, fmt = self._download_garmin_activity_file(act)
                if not file_bytes or fmt == "unavailable":
                    self.ledger.record_intervals_activity(
                        self.source_region,
                        source_id,
                        activity_name=act_name,
                        start_time_local=start_local,
                        file_format="none",
                        status="unavailable",
                        error_msg="No downloadable FIT/TCX/GPX file",
                    )
                    stats["duplicate"] += 1
                    logger.info(
                        "[%d/%d] ⏭ 无轨迹文件 (手动记录)，跳过文件上传: 【%s】 (%s)",
                        idx,
                        total,
                        act_name or "未命名",
                        start_local,
                    )
                    continue

                query_params: dict[str, str] = {"external_id": ext_id}
                if act_name:
                    query_params["name"] = act_name
                if act_desc:
                    query_params["description"] = act_desc

                resp = self._request(
                    "POST",
                    f"/athlete/{self.athlete_id}/activities",
                    params=query_params,
                    files={"file": (filename, file_bytes, "application/octet-stream")},
                )

                if resp.status_code in (200, 201):
                    body = resp.json() if resp.text else {}
                    iv_id = None
                    if isinstance(body, dict):
                        if body.get("id"):
                            iv_id = str(body["id"])
                        elif isinstance(body.get("activities"), list) and body["activities"]:
                            iv_id = str(body["activities"][0].get("id") or "")
                    self.ledger.record_intervals_activity(
                        self.source_region,
                        source_id,
                        intervals_id=iv_id,
                        activity_name=act_name,
                        start_time_local=start_local,
                        file_format=fmt,
                        status="uploaded",
                    )
                    stats["uploaded"] += 1
                    logger.info(
                        "[%d/%d] ✅ 已上传至 Intervals.icu (%s): 【%s】 (%s) -> %s",
                        idx,
                        total,
                        fmt.upper(),
                        act_name or "未命名",
                        start_local,
                        iv_id or "OK",
                    )
                elif resp.status_code == 409 or "duplicate" in resp.text.lower():
                    self.ledger.record_intervals_activity(
                        self.source_region,
                        source_id,
                        activity_name=act_name,
                        start_time_local=start_local,
                        file_format=fmt,
                        status="duplicate",
                    )
                    stats["duplicate"] += 1
                    logger.info(
                        "[%d/%d] ⏭ Intervals.icu 判定重复，跳过: 【%s】 (%s)",
                        idx,
                        total,
                        act_name or "未命名",
                        start_local,
                    )
                else:
                    raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:240]}")

            except Exception as err:
                stats["failed"] += 1
                self.ledger.record_intervals_activity(
                    self.source_region,
                    source_id,
                    activity_name=act_name,
                    start_time_local=start_local,
                    status="failed",
                    error_msg=str(err),
                )
                logger.error(
                    "[%d/%d] ❌ 上传活动至 Intervals.icu 失败: 【%s】 (%s) 错误: %s",
                    idx,
                    total,
                    act_name or "未命名",
                    start_local,
                    err,
                )

            if self.config.upload_delay_seconds > 0 and idx < total:
                time.sleep(min(1.0, self.config.upload_delay_seconds))

        logger.info(
            "🏁 Intervals.icu 运动同步完成: 总计=%d, 成功上传=%d, 重复/跳过=%d, 失败=%d",
            stats["total"],
            stats["uploaded"],
            stats["duplicate"],
            stats["failed"],
        )
        return stats

    # ------------------------------------------------------------------ #
    #  2. Daily Wellness Bulk Sync (HR, HRV, Sleep, Weight, SpO2, BP)    #
    # ------------------------------------------------------------------ #

    def collect_window_wellness(
        self,
        chunk_start: str,
        chunk_end: str,
        pending_dates: list[str],
        *,
        fast_mode: bool = False,
    ) -> dict[str, dict[str, Any]]:
        """Collect all Garmin wellness metrics for `[chunk_start, chunk_end]` keyed by `YYYY-MM-DD`."""
        by_date: dict[str, dict[str, Any]] = {d: {"id": d} for d in pending_dates}

        # 1. Range Query: Resting Heart Rate (RHR)
        try:
            rhr_rows = self.source.get_rhr_daily(chunk_start, chunk_end)
            for row in rhr_rows or []:
                cdate = str(row.get("calendarDate") or "")
                rhr = _safe_int(row.get("value"))
                if cdate in by_date and rhr:
                    by_date[cdate]["restingHR"] = rhr
        except Exception as e:
            logger.debug("RHR range query %s..%s skipped: %s", chunk_start, chunk_end, e)

        # 2. Range Query: Overnight HRV (rMSSD ms)
        try:
            hrv_data = self.source.get_hrv_data_range(chunk_start, chunk_end)
            summaries = (hrv_data or {}).get("hrvSummaries") if isinstance(hrv_data, dict) else []
            for item in summaries or []:
                if not isinstance(item, dict):
                    continue
                cdate = str(item.get("calendarDate") or "")
                hrv_val = _safe_float(item.get("lastNightAvg") or item.get("weeklyAvg"), 1)
                if cdate in by_date and hrv_val:
                    by_date[cdate]["hrv"] = hrv_val
        except Exception as e:
            logger.debug("HRV range query %s..%s skipped: %s", chunk_start, chunk_end, e)

        # 3. Range Query: Daily Steps
        try:
            step_rows = self.source.get_daily_steps(chunk_start, chunk_end)
            for row in step_rows or []:
                if not isinstance(row, dict):
                    continue
                cdate = str(row.get("calendarDate") or "")
                steps = _safe_int(row.get("totalSteps"))
                if cdate in by_date and steps is not None:
                    by_date[cdate]["steps"] = steps
        except Exception as e:
            logger.debug("Steps range query %s..%s skipped: %s", chunk_start, chunk_end, e)

        # 4. Range Query: Sleep Daily Summaries (Sleep Score & Duration)
        try:
            sleep_rows = self.source.get_sleep_daily(chunk_start, chunk_end)
            for row in sleep_rows or []:
                if not isinstance(row, dict):
                    continue
                cdate = str(row.get("calendarDate") or "")
                if cdate not in by_date:
                    continue
                values = row.get("values") if isinstance(row.get("values"), dict) else row
                score = _safe_int(
                    values.get("overallSleepScore")
                    or values.get("sleepScore")
                    or row.get("overallSleepScore")
                )
                if score:
                    by_date[cdate]["sleepScore"] = score
                sleep_secs = _safe_int(
                    values.get("totalSleepSeconds")
                    or values.get("sleepTimeSeconds")
                    or row.get("sleepTimeSeconds")
                )
                if sleep_secs:
                    by_date[cdate]["sleepSecs"] = sleep_secs
                rem_sp = _safe_float(values.get("spO2") or values.get("avgSpO2"), 1)
                if rem_sp:
                    by_date[cdate]["spO2"] = rem_sp
                resp_val = _safe_float(
                    values.get("respiration") or values.get("averageRespirationValue"), 1
                )
                if resp_val:
                    by_date[cdate]["respiration"] = resp_val
        except Exception as e:
            logger.debug("Sleep daily range query %s..%s skipped: %s", chunk_start, chunk_end, e)

        # 5. Range Query: Weight & Body Composition
        try:
            body_data = self.source.get_body_composition(chunk_start, chunk_end)
            weight_list = (
                (body_data or {}).get("dateWeightList")
                if isinstance(body_data, dict)
                else []
            )
            for entry in weight_list or []:
                if not isinstance(entry, dict):
                    continue
                cdate = str(entry.get("calendarDate") or "")
                if cdate not in by_date:
                    continue
                w_grams = _safe_float(entry.get("weight"), 1)
                if w_grams and w_grams > 0:
                    by_date[cdate]["weight"] = round(w_grams / 1000.0, 2)
                bf = _safe_float(entry.get("bodyFat"), 2)
                if bf:
                    by_date[cdate]["bodyFat"] = bf
        except Exception as e:
            logger.debug("Body composition range query %s..%s skipped: %s", chunk_start, chunk_end, e)

        # 6. Range Query: Blood Pressure
        try:
            bp_data = self.source.get_blood_pressure(chunk_start, chunk_end)
            if isinstance(bp_data, dict):
                for summary in bp_data.get("measurementSummaries") or []:
                    if not isinstance(summary, dict):
                        continue
                    for m in summary.get("measurements") or []:
                        if not isinstance(m, dict):
                            continue
                        ts_local = str(m.get("measurementTimestampLocal") or "")
                        cdate = ts_local[:10]
                        if cdate not in by_date:
                            continue
                        sys_v = _safe_int(m.get("systolic"))
                        dia_v = _safe_int(m.get("diastolic"))
                        if sys_v and dia_v:
                            by_date[cdate]["systolic"] = sys_v
                            by_date[cdate]["diastolic"] = dia_v
        except Exception as e:
            logger.debug("Blood pressure range query %s..%s skipped: %s", chunk_start, chunk_end, e)

        # 7. Range Query: VO2Max (Max Metrics)
        try:
            mm_data = self.source.get_max_metrics_range(chunk_start, chunk_end)
            mm_list = mm_data if isinstance(mm_data, list) else []
            for item in mm_list:
                if not isinstance(item, dict):
                    continue
                generic = item.get("generic") if isinstance(item.get("generic"), dict) else item
                cdate = str(generic.get("calendarDate") or item.get("calendarDate") or "")
                vo2 = _safe_float(
                    generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue"), 1
                )
                if cdate in by_date and vo2:
                    by_date[cdate]["vo2max"] = vo2
        except Exception as e:
            logger.debug("Max metrics range query %s..%s skipped: %s", chunk_start, chunk_end, e)

        # 8. Per-Day Deep Sleep & Summary Enrichment (unless fast_mode=True)
        if not fast_mode:
            for cdate in pending_dates:
                rec = by_date[cdate]
                # Fetch detailed dailySleepDTO for exact sleepSecs, sleepScore, sleepQuality, avgSleepHRV, spO2, respiration
                try:
                    s_data = self.source.get_sleep_data(cdate)
                    dto = (s_data or {}).get("dailySleepDTO") if isinstance(s_data, dict) else None
                    if isinstance(dto, dict):
                        sleep_secs = _safe_int(dto.get("sleepTimeSeconds"))
                        if sleep_secs:
                            rec["sleepSecs"] = sleep_secs

                        scores = dto.get("sleepScores")
                        if isinstance(scores, dict):
                            overall = scores.get("overall")
                            if isinstance(overall, dict):
                                sc_val = _safe_int(overall.get("value"))
                                if sc_val:
                                    rec["sleepScore"] = sc_val
                                q_key = str(overall.get("qualifierKey") or "").upper()
                                if q_key in SLEEP_QUALIFIER_MAP:
                                    rec["sleepQuality"] = SLEEP_QUALIFIER_MAP[q_key]

                        if "hrv" not in rec:
                            s_hrv = _safe_float(
                                dto.get("avgSleepHRV") or (s_data or {}).get("avgOvernightHrv"),
                                1,
                            )
                            if s_hrv:
                                rec["hrv"] = s_hrv

                        sp_val = _safe_float(
                            dto.get("avgSpO2") or dto.get("averageSpO2HRSleep"), 1
                        )
                        if sp_val:
                            rec["spO2"] = sp_val

                        resp_val = _safe_float(dto.get("averageRespirationValue"), 1)
                        if resp_val:
                            rec["respiration"] = resp_val

                        sleep_hr = _safe_float(dto.get("avgHeartRate"), 1)
                        if sleep_hr:
                            rec["avgSleepingHR"] = sleep_hr
                except Exception as e:
                    logger.debug("Per-day sleep query %s skipped: %s", cdate, e)

                # If RHR or steps or sleepSecs still missing, query user_summary
                if "restingHR" not in rec or "steps" not in rec:
                    try:
                        u_sum = self.source.get_user_summary(cdate)
                        if isinstance(u_sum, dict):
                            if "restingHR" not in rec:
                                rhr = _safe_int(u_sum.get("restingHeartRate"))
                                if rhr:
                                    rec["restingHR"] = rhr
                            if "steps" not in rec:
                                stp = _safe_int(u_sum.get("totalSteps"))
                                if stp is not None:
                                    rec["steps"] = stp
                            if "sleepSecs" not in rec:
                                slp_s = _safe_int(u_sum.get("sleepingSeconds"))
                                if slp_s:
                                    rec["sleepSecs"] = slp_s
                            stress_iv = _garmin_stress_to_intervals(
                                u_sum.get("averageStressLevel")
                            )
                            if stress_iv:
                                rec["stress"] = stress_iv
                    except Exception as e:
                        logger.debug("Per-day user summary %s skipped: %s", cdate, e)

                if self.config.wellness_day_delay_seconds > 0:
                    time.sleep(min(0.25, self.config.wellness_day_delay_seconds))

        return by_date

    def sync_wellness(
        self,
        start_date: str,
        end_date: str | None = None,
        *,
        fast_mode: bool = False,
        force: bool = False,
        dry_run: bool = False,
    ) -> dict[str, int]:
        """Collect Garmin daily health metrics and bulk-update Intervals.icu via `/wellness-bulk`."""
        start_d = date.fromisoformat(start_date)
        end_d = date.fromisoformat(end_date) if end_date else date.today()
        if start_d > end_d:
            start_d, end_d = end_d, start_d

        total_days = (end_d - start_d).days + 1
        stats = {"total_days": total_days, "uploaded_days": 0, "empty_days": 0, "skipped_days": 0, "failed_days": 0}

        logger.info(
            "💓 开始同步佳明 (%s) 每日健康数据至 Intervals.icu (%s -> %s, 共 %d 天, 模式=%s)...",
            self.source_region.upper(),
            start_d.isoformat(),
            end_d.isoformat(),
            total_days,
            "快速区间批量模式" if fast_mode else "深度睡眠+HRV全量模式",
        )

        cur_start = start_d
        while cur_start <= end_d:
            chunk_end = min(cur_start + timedelta(days=27), end_d)
            window_dates: list[str] = []
            d_cursor = cur_start
            while d_cursor <= chunk_end:
                window_dates.append(d_cursor.isoformat())
                d_cursor += timedelta(days=1)

            pending_dates = [
                d
                for d in window_dates
                if force or not self.ledger.is_intervals_wellness_done(self.source_region, d)
            ]
            skipped_in_win = len(window_dates) - len(pending_dates)
            stats["skipped_days"] += skipped_in_win

            if not pending_dates:
                logger.info(
                    "⏭ 区间 %s ~ %s (%d 天) 账本均已同步，跳过。",
                    cur_start.isoformat(),
                    chunk_end.isoformat(),
                    len(window_dates),
                )
                cur_start = chunk_end + timedelta(days=1)
                continue

            logger.info(
                "📡 正在聚合区间 %s ~ %s 的健康指标 (待处理 %d 天)...",
                cur_start.isoformat(),
                chunk_end.isoformat(),
                len(pending_dates),
            )
            by_date = self.collect_window_wellness(
                cur_start.isoformat(),
                chunk_end.isoformat(),
                pending_dates,
                fast_mode=fast_mode,
            )

            payload_batch: list[dict[str, Any]] = []
            empty_dates: list[str] = []
            for d_str in pending_dates:
                rec = by_date.get(d_str) or {"id": d_str}
                if len(rec) > 1:
                    payload_batch.append(rec)
                else:
                    empty_dates.append(d_str)

            if dry_run:
                for rec in payload_batch:
                    metrics_preview = ", ".join(
                        f"{k}={v}" for k, v in rec.items() if k != "id"
                    )
                    logger.info("   🔍 [DRY-RUN] %s: %s", rec["id"], metrics_preview)
                stats["uploaded_days"] += len(payload_batch)
                stats["empty_days"] += len(empty_dates)
                cur_start = chunk_end + timedelta(days=1)
                continue

            # Record empty days in ledger so we don't re-query them next time
            for d_str in empty_dates:
                self.ledger.record_intervals_wellness(
                    self.source_region, d_str, metrics_count=0, status="empty"
                )
            stats["empty_days"] += len(empty_dates)

            if payload_batch:
                try:
                    resp = self._request(
                        "PUT",
                        f"/athlete/{self.athlete_id}/wellness-bulk",
                        json_data=payload_batch,
                    )
                    if resp.status_code in (200, 201, 204):
                        for rec in payload_batch:
                            self.ledger.record_intervals_wellness(
                                self.source_region,
                                rec["id"],
                                metrics_count=len(rec) - 1,
                                status="uploaded",
                            )
                        stats["uploaded_days"] += len(payload_batch)
                        sample = payload_batch[-1]
                        sample_str = ", ".join(
                            f"{k}={v}" for k, v in sample.items() if k != "id"
                        )
                        logger.info(
                            "✅ 区间 %s ~ %s 批量写入 Intervals.icu 成功 (%d 天有效数据，示例 %s: %s)",
                            cur_start.isoformat(),
                            chunk_end.isoformat(),
                            len(payload_batch),
                            sample["id"],
                            sample_str,
                        )
                    else:
                        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:240]}")
                except Exception as err:
                    stats["failed_days"] += len(payload_batch)
                    for rec in payload_batch:
                        self.ledger.record_intervals_wellness(
                            self.source_region,
                            rec["id"],
                            metrics_count=len(rec) - 1,
                            status="failed",
                            error_msg=str(err),
                        )
                    logger.error(
                        "❌ 区间 %s ~ %s 批量写入 Intervals.icu 失败: %s",
                        cur_start.isoformat(),
                        chunk_end.isoformat(),
                        err,
                    )

            cur_start = chunk_end + timedelta(days=1)

        logger.info(
            "🏁 Intervals.icu 健康数据同步完成: 总天数=%d, 成功写入=%d, 无数据空日=%d, 账本跳过=%d, 失败=%d",
            stats["total_days"],
            stats["uploaded_days"],
            stats["empty_days"],
            stats["skipped_days"],
            stats["failed_days"],
        )
        return stats
