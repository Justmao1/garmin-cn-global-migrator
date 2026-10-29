"""Offline Importer for Garmin Official GDPR Data Export Archive (`DI_CONNECT`).

When a user requests an official full data export at:
  https://www.garmin.cn/zh-CN/account/datamanagement/ -> Export Your Data
Garmin emails a master `.zip` archive containing `DI_CONNECT/DI-Connect-Uploaded-Files/`
(which holds `.zip` or `.fit` files for every historical activity and device upload).

This module scans any local directory or `.zip` archive, extracts all `.fit` / `.tcx` / `.gpx`
files (including nested `.zip` archives inside `DI-Connect-Uploaded-Files`), and uploads
them to the target Garmin account with SQLite checkpointing and rate limiting.
"""

from __future__ import annotations

import io
import logging
import time
import zipfile
from pathlib import Path
from typing import Iterator

from garminconnect import Garmin

from ..config import MigratorConfig
from ..ledger import MigrationLedger
from ..upload_helper import (
    GarminInactiveWellnessDeviceError,
    GarminUploadConsentError,
    upload_bytes_to_garmin,
)

logger = logging.getLogger(__name__)

_SUPPORTED_EXTS = (".fit", ".tcx", ".gpx")


def iter_fit_files_from_path(root_path: Path) -> Iterator[tuple[str, str, bytes]]:
    """Yield `(unique_file_key, filename, raw_bytes)` from a directory or `.zip` file.

    Recursively inspects directories AND nested `.zip` archives (as found inside
    `DI_CONNECT/DI-Connect-Uploaded-Files/UploadedFiles_0-_Part1.zip`).
    """
    if root_path.is_file():
        if root_path.suffix.lower() in _SUPPORTED_EXTS:
            yield (root_path.name, root_path.name, root_path.read_bytes())
        elif root_path.suffix.lower() == ".zip":
            yield from _iter_from_zip_bytes(root_path.name, root_path.read_bytes())
        return

    all_files = sorted(root_path.rglob("*"))
    for p in all_files:
        if not p.is_file():
            continue
        rel_key = str(p.relative_to(root_path))
        ext = p.suffix.lower()
        if ext in _SUPPORTED_EXTS:
            yield (rel_key, p.name, p.read_bytes())
        elif ext == ".zip":
            try:
                yield from _iter_from_zip_bytes(rel_key, p.read_bytes())
            except Exception as e:
                logger.warning("⚠️ 无法读取压缩包 %s: %s", rel_key, e)


def _iter_from_zip_bytes(
    parent_key: str, zip_bytes: bytes, depth: int = 0
) -> Iterator[tuple[str, str, bytes]]:
    if depth > 3 or len(zip_bytes) < 4 or zip_bytes[:2] != b"PK":
        return

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in sorted(zf.infolist(), key=lambda x: x.filename):
            if info.is_dir():
                continue
            inner_name = info.filename
            base_name = inner_name.replace("\\", "/").rsplit("/", 1)[-1]
            if base_name.startswith(".") or "__MACOSX" in inner_name:
                continue
            lower_name = base_name.lower()
            entry_key = f"{parent_key}::{inner_name}"

            if lower_name.endswith(_SUPPORTED_EXTS):
                data = zf.read(inner_name)
                if data:
                    yield (entry_key, base_name, data)
            elif lower_name.endswith(".zip"):
                nested_bytes = zf.read(inner_name)
                yield from _iter_from_zip_bytes(entry_key, nested_bytes, depth=depth + 1)


class OfflineExportImporter:
    """Batch-imports `.fit`/`.tcx`/`.gpx` files from a local Garmin data export folder or ZIP."""

    def __init__(
        self,
        target: Garmin,
        ledger: MigrationLedger,
        config: MigratorConfig,
    ) -> None:
        self.target = target
        self.ledger = ledger
        self.config = config

    def import_from_path(
        self,
        export_path: str | Path,
        *,
        max_files: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, int]:
        p = Path(export_path).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(f"指定的本地路径不存在: {p}")

        logger.info("📂 正在扫描本地导出包/目录: %s ...", p)
        items = list(iter_fit_files_from_path(p))
        if max_files is not None and max_files > 0:
            items = items[:max_files]

        total = len(items)
        stats = {"total": total, "uploaded": 0, "duplicate": 0, "skipped": 0, "failed": 0}
        if total == 0:
            logger.info("✓ 在指定路径下未找到任何 .fit / .tcx / .gpx 文件。")
            return stats

        logger.info("📦 共扫描到 %d 个运动/健康数据文件，开始批量导入目标端 (%s)...", total, self.target.client.domain)

        for idx, (file_key, fname, raw_bytes) in enumerate(items, 1):
            if self.ledger.is_offline_file_done(file_key):
                stats["skipped"] += 1
                continue

            ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else "fit"
            if dry_run:
                logger.info("[%d/%d] 🔍 [DRY-RUN] 将导入本地文件: %s (%d bytes)", idx, total, file_key, len(raw_bytes))
                stats["uploaded"] += 1
                continue

            try:
                outcome = upload_bytes_to_garmin(
                    self.target,
                    raw_bytes,
                    fname,
                    is_wellness=False,
                )
                if outcome.status == "uploaded":
                    stats["uploaded"] += 1
                    self.ledger.record_offline_file(
                        file_key, ext, "uploaded", target_activity_id=outcome.target_activity_id
                    )
                    logger.info(
                        "[%d/%d] ✅ 导入成功: %s -> ActivityID=%s",
                        idx,
                        total,
                        file_key,
                        outcome.target_activity_id or "Wellness/OK",
                    )
                elif outcome.status == "duplicate":
                    stats["duplicate"] += 1
                    self.ledger.record_offline_file(
                        file_key, ext, "duplicate", target_activity_id=outcome.target_activity_id
                    )
                    logger.info("[%d/%d] ⏭ 重复跳过: %s", idx, total, file_key)
                else:
                    stats["failed"] += 1
                    self.ledger.record_offline_file(
                        file_key, ext, "failed", error_msg=outcome.message
                    )
                    logger.warning(
                        "[%d/%d] ⚠️ 导入被拒 (%s): %s",
                        idx,
                        total,
                        file_key,
                        outcome.message,
                    )
            except (GarminUploadConsentError, GarminInactiveWellnessDeviceError):
                raise
            except Exception as err:
                stats["failed"] += 1
                self.ledger.record_offline_file(
                    file_key, ext, "failed", error_msg=str(err)
                )
                logger.error("[%d/%d] ❌ 导入异常 (%s): %s", idx, total, file_key, err)

            if self.config.upload_delay_seconds > 0 and idx < total:
                time.sleep(self.config.upload_delay_seconds)

        logger.info(
            "🏁 本地数据包导入完成: 总计=%d, 新增=%d, 重复=%d, 已跳过=%d, 失败=%d",
            stats["total"],
            stats["uploaded"],
            stats["duplicate"],
            stats["skipped"],
            stats["failed"],
        )
        return stats
