"""Dual-region authentication, token persistence, MFA handling, and pre-flight verification."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from .config import MigratorConfig

logger = logging.getLogger(__name__)


class DualRegionAuth:
    """Manages authenticated Garmin Connect clients for China (garmin.cn) and Global (garmin.com)."""

    def __init__(self, config: MigratorConfig) -> None:
        self.config = config
        self._cn_client: Garmin | None = None
        self._global_client: Garmin | None = None

    @staticmethod
    def _prompt_mfa_for_region(region_label: str) -> str:
        print(f"\n🔐 [{region_label}] 触发两步验证 (MFA)！请查看您的邮箱或手机短信。")
        return input(f"👉 请输入 [{region_label}] 收到的 6 位验证码: ").strip()

    def login_region(self, is_cn: bool, force_relogin: bool = False) -> Garmin:
        """Authenticate and return a Garmin client for either CN (`is_cn=True`) or Global (`is_cn=False`)."""
        region_label = "佳明中国区 (garmin.cn)" if is_cn else "佳明国际区 (garmin.com)"
        username = self.config.cn_username if is_cn else self.config.global_username
        password = self.config.cn_password if is_cn else self.config.global_password
        token_dir: Path = self.config.cn_token_dir if is_cn else self.config.global_token_dir
        token_dir.mkdir(parents=True, exist_ok=True)

        token_file = token_dir / "garmin_tokens.json"
        if force_relogin and token_file.exists():
            token_file.unlink()

        client = Garmin(
            email=username or None,
            password=password or None,
            is_cn=is_cn,
            prompt_mfa=lambda: self._prompt_mfa_for_region(region_label),
            return_on_mfa=False,
        )

        # 1. Try resuming from persisted token store first (avoids SSO rate limits)
        if token_file.exists() and not force_relogin:
            try:
                client.login(tokenstore=str(token_dir))
                logger.info(
                    "✓ [%s] 使用本地缓存 Token 登录成功 (用户: %s, DisplayName: %s)",
                    region_label,
                    client.full_name or username,
                    client.display_name,
                )
                return client
            except Exception as e:
                logger.warning(
                    "⚠️ [%s] 缓存 Token 已过期或失效 (%s)，将使用账号密码重新登录...",
                    region_label,
                    e,
                )

        # 2. Fresh credential login + save token store
        if not username or not password:
            env_hint = (
                "GARMIN_CN_USERNAME / GARMIN_CN_PASSWORD"
                if is_cn
                else "GARMIN_GLOBAL_USERNAME / GARMIN_GLOBAL_PASSWORD"
            )
            raise GarminConnectAuthenticationError(
                f"[{region_label}] 未找到有效缓存 Token，且未在 .env 中配置 {env_hint}"
            )

        logger.info("⏳ [%s] 正在通过 SSO 認證登录 (%s)...", region_label, username)
        client.login(tokenstore=str(token_dir))
        client.client.dump(str(token_dir))
        logger.info(
            "✓ [%s] 登录成功并已缓存 Token 至 %s (用户: %s, DisplayName: %s)",
            region_label,
            token_dir,
            client.full_name or username,
            client.display_name,
        )
        return client

    @property
    def cn_client(self) -> Garmin:
        if self._cn_client is None:
            self._cn_client = self.login_region(is_cn=True)
        return self._cn_client

    @property
    def global_client(self) -> Garmin:
        if self._global_client is None:
            self._global_client = self.login_region(is_cn=False)
        return self._global_client

    @property
    def source_client(self) -> Garmin:
        return (
            self.cn_client
            if self.config.direction == "cn_to_global"
            else self.global_client
        )

    @property
    def target_client(self) -> Garmin:
        return (
            self.global_client
            if self.config.direction == "cn_to_global"
            else self.cn_client
        )

    @staticmethod
    def inspect_account_readiness(client: Garmin) -> dict[str, Any]:
        """Inspect account readiness (activity count, bound devices, privacy settings)."""
        info: dict[str, Any] = {
            "domain": client.client.domain,
            "display_name": client.display_name,
            "full_name": client.full_name,
            "profile_id": client.profile_id,
            "unit_system": client.unit_system,
            "time_zone": client.get_user_timezone(),
            "activity_count": 0,
            "devices": [],
            "has_wellness_device": False,
        }
        try:
            info["activity_count"] = client.count_activities()
        except Exception as e:
            logger.debug("Could not count activities: %s", e)

        try:
            devices = client.get_devices() or []
            if isinstance(devices, list):
                info["devices"] = [
                    {
                        "deviceId": d.get("deviceId"),
                        "productDisplayName": d.get("productDisplayName"),
                        "partNumber": d.get("partNumber"),
                    }
                    for d in devices
                    if isinstance(d, dict)
                ]
                info["has_wellness_device"] = len(info["devices"]) > 0
        except Exception as e:
            logger.debug("Could not fetch devices: %s", e)

        return info
