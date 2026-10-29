"""Configuration management for Garmin CN <-> Global Migrator."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    # Minimal built-in .env parser fallback if python-dotenv is not installed
    env_file = Path.cwd() / ".env"
    if env_file.is_file():
        for raw_line in env_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip("'\"")
            os.environ.setdefault(key, val)


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(0.0, float(raw.strip()))
    except ValueError:
        return default


@dataclass
class MigratorConfig:
    """Runtime configuration for Garmin cross-region migration."""

    cn_username: str = ""
    cn_password: str = ""
    global_username: str = ""
    global_password: str = ""
    direction: str = "cn_to_global"  # "cn_to_global" or "global_to_cn"
    upload_delay_seconds: float = 1.5
    wellness_day_delay_seconds: float = 1.0
    sync_activity_metadata: bool = True
    data_dir: Path = Path("./data")
    token_dir: Path = Path("./data/tokens")
    ledger_db_path: Path = Path("./data/migration_ledger.db")

    @classmethod
    def from_env(cls) -> MigratorConfig:
        data_dir = Path(os.getenv("DATA_DIR", "./data")).expanduser()
        token_dir = Path(
            os.getenv("TOKEN_DIR", str(data_dir / "tokens"))
        ).expanduser()
        ledger_db_path = Path(
            os.getenv("LEDGER_DB_PATH", str(data_dir / "migration_ledger.db"))
        ).expanduser()

        direction = os.getenv("MIGRATE_DIRECTION", "cn_to_global").strip().lower()
        if direction not in ("cn_to_global", "global_to_cn"):
            direction = "cn_to_global"

        return cls(
            cn_username=os.getenv(
                "GARMIN_CN_USERNAME", os.getenv("GARMIN_USERNAME", "")
            ).strip(),
            cn_password=os.getenv(
                "GARMIN_CN_PASSWORD", os.getenv("GARMIN_PASSWORD", "")
            ).strip(),
            global_username=os.getenv("GARMIN_GLOBAL_USERNAME", "").strip(),
            global_password=os.getenv("GARMIN_GLOBAL_PASSWORD", "").strip(),
            direction=direction,
            upload_delay_seconds=_env_float("UPLOAD_DELAY_SECONDS", 1.5),
            wellness_day_delay_seconds=_env_float("WELLNESS_DAY_DELAY_SECONDS", 1.0),
            sync_activity_metadata=_env_bool("SYNC_ACTIVITY_METADATA", True),
            data_dir=data_dir,
            token_dir=token_dir,
            ledger_db_path=ledger_db_path,
        )

    @property
    def source_region(self) -> str:
        return "cn" if self.direction == "cn_to_global" else "global"

    @property
    def target_region(self) -> str:
        return "global" if self.direction == "cn_to_global" else "cn"

    @property
    def cn_token_dir(self) -> Path:
        return self.token_dir / "cn"

    @property
    def global_token_dir(self) -> Path:
        return self.token_dir / "global"
