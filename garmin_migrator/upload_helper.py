"""Unified file & bytes uploader for Garmin Connect (.fit, .tcx, .gpx) with error classification.

Handles:
- Activity file uploads (FIT, TCX, GPX)
- Wellness monitoring .fit file uploads (Steps, Sleep, HR, Stress, Body Battery, HRV)
- Parsing `detailedImportResult` to extract `internalId` (the newly created target activity ID)
- Automatic detection of HTTP 409 / Code 202 ("Duplicate Activity" / "Duplicate Wellness File")
- Actionable diagnostics for HTTP 412 (Privacy Settings consent missing) and HTTP 419 (No active Wellness device)
- Automatic exponential backoff for transient 408 / 429 / 5xx errors
"""

from __future__ import annotations

import io
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from garminconnect import Garmin

logger = logging.getLogger(__name__)


class GarminUploadConsentError(RuntimeError):
    """Raised on HTTP 412 when target account has not enabled 'Device Upload' / 'Storage & Processing'."""


class GarminInactiveWellnessDeviceError(RuntimeError):
    """Raised on HTTP 419 when target account has no bound Garmin device for wellness data."""


@dataclass
class UploadOutcome:
    status: str  # 'uploaded' | 'duplicate' | 'error'
    target_activity_id: int | None = None
    message: str = ""
    raw_response: dict[str, Any] | None = None


_FILENAME_SANITIZE_RE = re.compile(r'[\r\n\t"\\/?#%&=\s;:]+')


def sanitize_upload_filename(filename: str) -> str:
    cleaned = _FILENAME_SANITIZE_RE.sub("_", filename.strip())
    return cleaned or "upload.fit"


def _content_type_for_ext(ext: str) -> str:
    ext_lower = ext.lower().lstrip(".")
    if ext_lower == "gpx":
        return "application/gpx+xml"
    if ext_lower == "tcx":
        return "application/vnd.garmin.tcx+xml"
    return "application/octet-stream"


def _parse_detailed_import_result(data: Any) -> tuple[int | None, bool, str]:
    """Extract (first_activity_id, is_duplicate, failure_message) from Garmin upload JSON."""
    if not isinstance(data, dict):
        return None, False, ""

    detail = data.get("detailedImportResult")
    if not isinstance(detail, dict):
        return None, False, ""

    activity_id: int | None = None
    is_dup = False
    messages: list[str] = []

    successes = detail.get("successes") or []
    if isinstance(successes, list) and successes:
        first = successes[0]
        if isinstance(first, dict) and first.get("internalId"):
            try:
                activity_id = int(first["internalId"])
            except (TypeError, ValueError):
                pass

    failures = detail.get("failures") or []
    if isinstance(failures, list):
        for fail in failures:
            if not isinstance(fail, dict):
                continue
            if activity_id is None and fail.get("internalId"):
                try:
                    activity_id = int(fail["internalId"])
                except (TypeError, ValueError):
                    pass
            reason = str(fail.get("failureReason") or "")
            if "duplicate" in reason.lower():
                is_dup = True
            if reason:
                messages.append(reason)
            for msg_obj in fail.get("messages") or []:
                if isinstance(msg_obj, dict):
                    code = msg_obj.get("code")
                    content = str(msg_obj.get("content") or "")
                    if code == 202 or "duplicate" in content.lower():
                        is_dup = True
                    if content:
                        messages.append(content)

    return activity_id, is_dup, "; ".join(messages)


def upload_bytes_to_garmin(
    client: Garmin,
    data: bytes,
    filename: str,
    *,
    is_wellness: bool = False,
    max_attempts: int = 3,
) -> UploadOutcome:
    """Upload raw .fit / .tcx / .gpx bytes to a Garmin account with retry and error classification."""
    safe_name = sanitize_upload_filename(filename)
    ext = safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else "fit"
    content_type = _content_type_for_ext(ext)

    # Garmin upload endpoint accepts /upload-service/upload (or /upload-service/upload/.fit)
    upload_path = "/upload-service/upload"

    last_err_str = ""
    for attempt in range(1, max_attempts + 1):
        bio = io.BytesIO(data)
        files = {"file": (safe_name, bio, content_type)}
        try:
            resp = client.client.post("connectapi", upload_path, files=files, api=False)
            resp_json: dict[str, Any] = {}
            if hasattr(resp, "json"):
                try:
                    resp_json = resp.json()
                except Exception:
                    resp_json = {}

            act_id, is_dup, msg = _parse_detailed_import_result(resp_json)
            if is_dup:
                return UploadOutcome(
                    status="duplicate",
                    target_activity_id=act_id,
                    message=msg or "Duplicate file",
                    raw_response=resp_json,
                )
            return UploadOutcome(
                status="uploaded",
                target_activity_id=act_id,
                message=msg,
                raw_response=resp_json,
            )

        except Exception as err:
            err_text = str(err)
            last_err_str = err_text
            resp_obj = getattr(err, "response", None)
            status_code = getattr(resp_obj, "status_code", None)

            # Extract JSON body from error response if present
            err_json: dict[str, Any] = {}
            if resp_obj is not None and hasattr(resp_obj, "json"):
                try:
                    err_json = resp_obj.json()
                except Exception:
                    err_json = {}

            act_id, is_dup, parsed_msg = _parse_detailed_import_result(err_json)

            # 1. Duplicate check (HTTP 409 or Code 202 / Duplicate text)
            if (
                status_code == 409
                or is_dup
                or "409" in err_text
                or "duplicate activity" in err_text.lower()
                or "duplicate wellness file" in err_text.lower()
            ):
                return UploadOutcome(
                    status="duplicate",
                    target_activity_id=act_id,
                    message=parsed_msg or "Already exists on target account (HTTP 409)",
                    raw_response=err_json,
                )

            # 2. HTTP 412: Missing Privacy Consent ("Device Upload" / "Storage & Processing")
            if status_code == 412 or "API Error 412" in err_text:
                raise GarminUploadConsentError(
                    f"目标账号拒绝上传 (HTTP 412 Precondition Failed)！\n"
                    f"请浏览器登录目标区网页端 (https://connect.{client.client.domain}/modern/settings/privacySettings)，\n"
                    f"将【存储和处理 (Storage and Processing)】设为同意，并将【设备上传 (Device Upload)】设为启用后再试。"
                ) from err

            # 3. HTTP 419: Inactive Wellness Device on Target Account
            if (
                status_code == 419
                or "API Error 419" in err_text
                or "wellness device is not active" in err_text.lower()
            ):
                if is_wellness:
                    raise GarminInactiveWellnessDeviceError(
                        f"目标账号无法导入健康监测数据 (HTTP 419: Wellness device is not active for this user)！\n"
                        f"原因：目标账号 ({client.client.domain}) 名下尚未绑定过任何支持健康监测的佳明物理设备。\n"
                        f"解决方法：请先在手机 Garmin Connect App 登录国际区账号，将您的佳明手表配对绑定并同步一次，\n"
                        f"然后再重新运行健康数据迁移。"
                    ) from err

            # 4. Transient / Rate-limit errors (408, 429, 500, 502, 503, 504) -> Retry with backoff
            if status_code in (408, 429, 500, 502, 503, 504) or any(
                code in err_text for code in ("429", "500", "502", "503", "504", "timeout")
            ):
                if attempt < max_attempts:
                    wait_s = 3.0 * (2 ** (attempt - 1))
                    if status_code == 429 or "429" in err_text:
                        wait_s = max(wait_s, 15.0 * attempt)
                    logger.warning(
                        "⚠️ 上传 %s 遇到临时错误 (%s)，等待 %.1f 秒后进行第 %d/%d 次重试...",
                        safe_name,
                        err_text,
                        wait_s,
                        attempt + 1,
                        max_attempts,
                    )
                    time.sleep(wait_s)
                    continue

            return UploadOutcome(
                status="error",
                target_activity_id=act_id,
                message=parsed_msg or err_text,
                raw_response=err_json,
            )

    return UploadOutcome(status="error", message=last_err_str)
