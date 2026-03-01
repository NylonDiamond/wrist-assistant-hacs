"""APNs credential storage and management endpoints."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import logging
from typing import Awaitable, Callable

from aiohttp.web import Request, Response

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import APNS_CONFIG_STORAGE_KEY, APNS_CONFIG_STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class APNsConfig:
    """Persisted APNs credentials."""

    key_id: str
    team_id: str
    topic: str
    private_key: str


class APNsConfigStore:
    """Persistent store for APNs credentials."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._config: APNsConfig | None = None
        self._store: Store = Store(
            hass,
            APNS_CONFIG_STORAGE_VERSION,
            APNS_CONFIG_STORAGE_KEY,
        )

    async def async_load(self) -> None:
        """Load APNs credentials from disk."""
        data = await self._store.async_load()
        if not data or not isinstance(data, dict):
            return
        key_id = data.get("key_id")
        team_id = data.get("team_id")
        topic = data.get("topic")
        private_key = data.get("private_key")
        if all(isinstance(v, str) and v for v in (key_id, team_id, topic, private_key)):
            self._config = APNsConfig(
                key_id=key_id,
                team_id=team_id,
                topic=topic,
                private_key=private_key,
            )

    async def async_save(self, config: APNsConfig) -> None:
        """Persist APNs credentials."""
        self._config = config
        await self._store.async_save(asdict(config))

    @property
    def config(self) -> APNsConfig | None:
        """Return stored credentials if present."""
        return self._config

    @property
    def is_configured(self) -> bool:
        """Return whether credentials are available."""
        return self._config is not None


class APNsConfigView(HomeAssistantView):
    """Authenticated endpoint to configure APNs credentials."""

    url = "/api/wrist_assistant/apns/config"
    name = "api:wrist_assistant_apns_config"
    requires_auth = True

    def __init__(
        self,
        store: APNsConfigStore,
        reload_cb: Callable[[], Awaitable[None]],
    ) -> None:
        self._store = store
        self._reload_cb = reload_cb

    async def post(self, request: Request) -> Response:
        """Persist APNs credentials and reload APNs client."""
        try:
            payload = await request.json()
        except (ValueError, UnicodeDecodeError):
            return self.json_message("Invalid JSON body", status_code=400)

        if not isinstance(payload, dict):
            return self.json_message("Expected JSON object", status_code=400)

        key_id = payload.get("key_id")
        team_id = payload.get("team_id")
        topic = payload.get("topic")
        private_key = payload.get("private_key")

        if not isinstance(key_id, str) or not key_id.strip():
            return self.json_message("key_id is required", status_code=400)
        if not isinstance(team_id, str) or not team_id.strip():
            return self.json_message("team_id is required", status_code=400)
        if not isinstance(topic, str) or not topic.strip():
            return self.json_message("topic is required", status_code=400)
        if not isinstance(private_key, str) or "BEGIN PRIVATE KEY" not in private_key:
            return self.json_message("private_key must be a valid .p8 key string", status_code=400)

        config = APNsConfig(
            key_id=key_id.strip(),
            team_id=team_id.strip(),
            topic=topic.strip(),
            private_key=private_key.strip(),
        )
        await self._store.async_save(config)
        await self._reload_cb()
        _LOGGER.info("APNs credentials updated via API")
        return self.json({"status": "ok"})
