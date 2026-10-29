"""Unit & integration tests for garmin-cn-global-migrator."""

from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

from garmin_migrator.config import MigratorConfig
from garmin_migrator.ledger import MigrationLedger
from garmin_migrator.migrators.activities import ActivityMigrator, _activity_dedup_key
from garmin_migrator.migrators.body_health import BodyHealthMigrator
from garmin_migrator.migrators.gear_workouts import GearAndWorkoutsMigrator
from garmin_migrator.migrators.offline_export import (
    OfflineExportImporter,
    iter_fit_files_from_path,
)
from garmin_migrator.migrators.wellness import WellnessMigrator, generate_date_range
from garmin_migrator.upload_helper import _parse_detailed_import_result


def _make_zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


class TestGarminMigratorSuite(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.tmp_dir.name)
        self.ledger = MigrationLedger(self.base_path / "test_ledger.db")
        self.config = MigratorConfig(
            cn_username="cn@test.com",
            cn_password="pw",
            global_username="global@test.com",
            global_password="pw",
            direction="cn_to_global",
            upload_delay_seconds=0.0,
            wellness_day_delay_seconds=0.0,
            sync_activity_metadata=True,
            data_dir=self.base_path,
            token_dir=self.base_path / "tokens",
            ledger_db_path=self.base_path / "test_ledger.db",
        )

    def tearDown(self) -> None:
        self.ledger.close()
        self.tmp_dir.cleanup()

    def test_ledger_checkpointing(self) -> None:
        self.assertFalse(self.ledger.is_activity_done("cn_to_global", 1001))
        self.ledger.record_activity(
            "cn_to_global",
            1001,
            target_id=2001,
            activity_name="晨跑 10km",
            activity_type="running",
            start_time_local="2026-10-01 07:00:00",
            duration=3000.0,
            status="uploaded",
            metadata_synced=True,
        )
        self.assertTrue(
            self.ledger.is_activity_done("cn_to_global", 1001, require_metadata=True)
        )
        self.assertEqual(self.ledger.get_activity_id_map("cn_to_global"), {1001: 2001})

        # Wellness day
        self.assertFalse(self.ledger.is_wellness_day_done("cn_to_global", "2026-10-01"))
        self.ledger.record_wellness_day(
            "cn_to_global", "2026-10-01", total_files=3, uploaded_files=3, status="done"
        )
        self.assertTrue(self.ledger.is_wellness_day_done("cn_to_global", "2026-10-01"))

    def test_detailed_import_result_parser(self) -> None:
        payload = {
            "detailedImportResult": {
                "uploadId": 999,
                "successes": [{"internalId": 888777666, "externalId": "abc.fit"}],
                "failures": [],
            }
        }
        act_id, is_dup, msg = _parse_detailed_import_result(payload)
        self.assertEqual(act_id, 888777666)
        self.assertFalse(is_dup)

        dup_payload = {
            "detailedImportResult": {
                "uploadId": "",
                "successes": [],
                "failures": [
                    {
                        "internalId": 555444333,
                        "messages": [{"code": 202, "content": "Duplicate Activity."}],
                    }
                ],
            }
        }
        act_id2, is_dup2, msg2 = _parse_detailed_import_result(dup_payload)
        self.assertEqual(act_id2, 555444333)
        self.assertTrue(is_dup2)

    def test_weight_fit_binary_encoder(self) -> None:
        raw_entry = {
            "samplePk": 12345,
            "calendarDate": "2026-10-20",
            "timestampGMT": 1760947200000,
            "weight": 68500.0,  # 68.5 kg in grams
            "bmi": 21.8,
            "bodyFat": 14.2,
            "bodyWater": 61.5,
            "boneMass": 3100.0,  # 3.1 kg in grams
            "muscleMass": 32400.0,  # 32.4 kg in grams
            "visceralFat": 4,
            "metabolicAge": 24,
            "physiqueRating": 5,
            "basalMet": 1680.0,
        }
        fit_bytes, weight_kg = BodyHealthMigrator.encode_weight_fit_bytes(raw_entry)
        self.assertAlmostEqual(weight_kg, 68.5)
        self.assertTrue(len(fit_bytes) > 40)
        # Verify FIT magic bytes '.FIT' at offset 8..12
        self.assertEqual(fit_bytes[8:12], b".FIT")

    def test_wellness_zip_extraction_and_migration(self) -> None:
        src = MagicMock()
        tgt = MagicMock()
        zip_payload = _make_zip_bytes(
            {
                "1001_WELLNESS.fit": b"FAKE_FIT_DATA_1",
                "1002_SLEEP.fit": b"FAKE_FIT_DATA_2",
                "readme.txt": b"ignore me",
            }
        )
        src.download_health_snapshot.return_value = zip_payload

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "detailedImportResult": {"successes": [{"internalId": 1}], "failures": []}
        }
        tgt.client.post.return_value = mock_resp

        migrator = WellnessMigrator(src, tgt, self.ledger, self.config)
        res = migrator.migrate_single_day("2026-10-15")
        self.assertEqual(res["status"], "done")
        self.assertEqual(res["total"], 2)
        self.assertEqual(res["uploaded"], 2)
        self.assertTrue(self.ledger.is_wellness_day_done("cn_to_global", "2026-10-15"))

    def test_activity_migration_with_metadata_sync(self) -> None:
        src = MagicMock()
        tgt = MagicMock()
        src.ActivityDownloadFormat = MagicMock()
        src.ActivityDownloadFormat.ORIGINAL = "ORIGINAL"

        cn_act = {
            "activityId": 111,
            "activityName": "奥森公园节奏跑",
            "description": "配速稳定，体感轻松",
            "startTimeLocal": "2026-10-18 07:30:00",
            "duration": 2700.4,
            "distance": 10000.0,
            "favorite": True,
            "activityType": {"typeId": 1, "typeKey": "running"},
            "eventType": {"typeId": 1, "typeKey": "race"},
        }
        src.get_activities.side_effect = [[cn_act], []]
        tgt.get_activities.return_value = []
        src.download_activity.return_value = _make_zip_bytes({"111_ACTIVITY.fit": b"FITDATA"})

        upload_resp = MagicMock()
        upload_resp.json.return_value = {
            "detailedImportResult": {
                "successes": [{"internalId": 999}],
                "failures": [],
            }
        }
        tgt.client.post.return_value = upload_resp

        migrator = ActivityMigrator(src, tgt, self.ledger, self.config)
        stats = migrator.migrate()
        self.assertEqual(stats["uploaded"], 1)
        self.assertEqual(stats["failed"], 0)

        # Verify custom Chinese name, description, favorite, and eventType were synced to target ID 999
        tgt.update_activity.assert_called_once_with(
            999,
            {
                "activityName": "奥森公园节奏跑",
                "description": "配速稳定，体感轻松",
                "metadataDTO": {"favorite": True},
                "eventTypeDTO": {"typeId": 1, "typeKey": "race"},
            },
        )
        self.assertEqual(self.ledger.get_activity_id_map("cn_to_global"), {111: 999})

    def test_nested_offline_export_scanner(self) -> None:
        inner_zip = _make_zip_bytes(
            {
                "DI-Connect-Uploaded-Files/act1.fit": b"FIT_1",
                "DI-Connect-Uploaded-Files/act2.tcx": b"TCX_2",
            }
        )
        outer_zip_path = self.base_path / "Garmin_Full_Export.zip"
        outer_zip_path.write_bytes(
            _make_zip_bytes(
                {
                    "DI_CONNECT/DI-Connect-Uploaded-Files/UploadedFiles_0-_Part1.zip": inner_zip,
                    "DI_CONNECT/direct_activity.fit": b"FIT_DIRECT",
                }
            )
        )

        extracted = list(iter_fit_files_from_path(outer_zip_path))
        self.assertEqual(len(extracted), 3)
        filenames = [item[1] for item in extracted]
        self.assertEqual(sorted(filenames), ["act1.fit", "act2.tcx", "direct_activity.fit"])

    def test_workout_cleaner(self) -> None:
        raw_workout = {
            "workoutId": 123,
            "ownerId": 456,
            "workoutName": "800m 间歇跑",
            "workoutSegments": [
                {
                    "segmentOrder": 1,
                    "workoutSteps": [
                        {"stepId": 999, "type": "ExecutableStepDTO"}
                    ],
                }
            ],
        }
        cleaned = GearAndWorkoutsMigrator._clean_workout_payload_for_upload(raw_workout)
        self.assertNotIn("workoutId", cleaned)
        self.assertNotIn("ownerId", cleaned)
        self.assertNotIn("stepId", cleaned["workoutSegments"][0]["workoutSteps"][0])
        self.assertEqual(cleaned["workoutName"], "800m 间歇跑")

    def test_date_range_generator(self) -> None:
        dates = generate_date_range("2026-10-01", "2026-10-03", oldest_first=True)
        self.assertEqual(dates, ["2026-10-01", "2026-10-02", "2026-10-03"])


if __name__ == "__main__":
    unittest.main()
