"""Push notification token registration for Wrist Assistant."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from aiohttp.web import Request, Response

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import NOTIFICATION_TOKEN_STORAGE_KEY, NOTIFICATION_TOKEN_STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class TokenEntry:
    """Stored device token for a watch."""

    device_token: str
    platform: str
    environment: str  # "development" or "production"


class NotificationTokenStore:
    """Persistent store of watch_id → APNs device token."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._tokens: dict[str, TokenEntry] = {}
        self._store: Store = Store(
            hass,
            NOTIFICATION_TOKEN_STORAGE_VERSION,
            NOTIFICATION_TOKEN_STORAGE_KEY,
        )

    async def async_load(self) -> None:
        """Load persisted tokens from disk."""
        data = await self._store.async_load()
        if not data or not isinstance(data, dict):
            return
        tokens = data.get("tokens", {})
        for watch_id, entry in tokens.items():
            if isinstance(entry, dict) and "device_token" in entry:
                self._tokens[watch_id] = TokenEntry(
                    device_token=entry["device_token"],
                    platform=entry.get("platform", "watchos"),
                    environment=entry.get("environment", "production"),
                )
        _LOGGER.debug("Loaded %d notification tokens from storage", len(self._tokens))

    def _serialize(self) -> dict:
        """Serialize tokens for storage."""
        return {
            "tokens": {
                watch_id: {
                    "device_token": entry.device_token,
                    "platform": entry.platform,
                    "environment": entry.environment,
                }
                for watch_id, entry in self._tokens.items()
            }
        }

    def register(
        self,
        watch_id: str,
        device_token: str,
        platform: str = "watchos",
        environment: str = "production",
    ) -> None:
        """Store or update a device token for a watch."""
        existing = self._tokens.get(watch_id)
        if (
            existing
            and existing.device_token == device_token
            and existing.environment == environment
        ):
            return
        self._tokens[watch_id] = TokenEntry(
            device_token=device_token, platform=platform, environment=environment
        )
        _LOGGER.info(
            "Registered push token for watch_id=%s (platform=%s, environment=%s)",
            watch_id,
            platform,
            environment,
        )
        self._store.async_delay_save(self._serialize, 5)

    def get_token(self, watch_id: str) -> str | None:
        """Return the device token for a watch, or None."""
        entry = self._tokens.get(watch_id)
        return entry.device_token if entry else None

    def get_entry(self, watch_id: str) -> TokenEntry | None:
        """Return the full token entry for a watch, or None."""
        return self._tokens.get(watch_id)

    @property
    def all_tokens(self) -> dict[str, TokenEntry]:
        """Return all registered tokens."""
        return dict(self._tokens)

    def remove(self, watch_id: str) -> None:
        """Remove a watch's token."""
        if self._tokens.pop(watch_id, None) is not None:
            self._store.async_delay_save(self._serialize, 5)


class NotificationRegisterView(HomeAssistantView):
    """POST endpoint to explicitly register a push notification token."""

    url = "/api/wrist_assistant/notifications/register"
    name = "api:wrist_assistant_notification_register"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: Request) -> Response:
        """Register a device token."""
        from .const import DATA_NOTIFICATION_TOKEN_STORE, DOMAIN

        store = self._hass.data.get(DOMAIN, {}).get(DATA_NOTIFICATION_TOKEN_STORE)
        if store is None:
            return self.json_message("Integration not loaded", status_code=503)

        try:
            payload = await request.json()
        except (ValueError, UnicodeDecodeError):
            return self.json_message("Invalid JSON body", status_code=400)

        if not isinstance(payload, dict):
            return self.json_message("Expected JSON object", status_code=400)

        watch_id = payload.get("watch_id")
        device_token = payload.get("device_token")
        platform = payload.get("platform", "watchos")
        environment = payload.get("environment", "production")

        if not isinstance(watch_id, str) or not watch_id:
            return self.json_message("watch_id is required", status_code=400)
        if not isinstance(device_token, str) or not device_token:
            return self.json_message("device_token is required", status_code=400)

        store.register(
            watch_id, device_token, platform=platform, environment=environment
        )
        return self.json({"status": "ok"})
