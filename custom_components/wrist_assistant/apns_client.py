"""APNs push notification client for Wrist Assistant."""

from __future__ import annotations

import logging
from pathlib import Path

from aioapns import APNs, NotificationRequest, PushType

from .const import APNS_KEY_ID, APNS_TEAM_ID, APNS_TOPIC

_LOGGER = logging.getLogger(__name__)

# Bundled .p8 key lives alongside this file
_BUNDLED_KEY_PATH = Path(__file__).parent / "apns_key.p8"

_DEAD_TOKEN_REASONS = frozenset({
    "BadDeviceToken",
    "Unregistered",
    "DeviceTokenNotForTopic",
})


class APNsClient:
    """Wrapper around aioapns for sending push notifications.

    Maintains both a production and sandbox client internally so pushes
    are routed to the correct APNs gateway based on each token's environment.

    APNs instances are created lazily on first send to avoid blocking
    SSL setup during HA startup.
    """

    def __init__(self) -> None:
        if not _BUNDLED_KEY_PATH.is_file():
            raise FileNotFoundError(f"Bundled APNs key not found at {_BUNDLED_KEY_PATH}")
        if not APNS_KEY_ID or not APNS_TEAM_ID:
            raise ValueError("APNS_KEY_ID and APNS_TEAM_ID must be set in const.py")
        self._key_content: str = _BUNDLED_KEY_PATH.read_text()
        self._production: APNs | None = None
        self._sandbox: APNs | None = None

    def _get_client(self, environment: str) -> APNs:
        """Return the APNs client for the given environment, creating lazily."""
        if environment == "development":
            if self._sandbox is None:
                self._sandbox = APNs(
                    key=self._key_content,
                    key_id=APNS_KEY_ID,
                    team_id=APNS_TEAM_ID,
                    topic=APNS_TOPIC,
                    use_sandbox=True,
                )
            return self._sandbox
        if self._production is None:
            self._production = APNs(
                key=self._key_content,
                key_id=APNS_KEY_ID,
                team_id=APNS_TEAM_ID,
                topic=APNS_TOPIC,
                use_sandbox=False,
            )
        return self._production

    async def send_push(
        self,
        device_token: str,
        title: str | None = None,
        body: str | None = None,
        category: str | None = None,
        data: dict | None = None,
        sound: str | None = None,
        push_type: str = "alert",
        environment: str = "production",
    ) -> tuple[bool, str | None]:
        """Send a push notification. Returns (success, reason)."""
        alert: dict | None = None
        if title or body:
            alert = {}
            if title:
                alert["title"] = title
            if body:
                alert["body"] = body

        aps: dict = {}
        if alert:
            aps["alert"] = alert
        if sound:
            aps["sound"] = sound
        if category:
            aps["category"] = category
        if push_type == "background":
            aps["content-available"] = 1

        message: dict = {"aps": aps}
        if data:
            for key, value in data.items():
                message[key] = value

        apns_push_type = PushType.BACKGROUND if push_type == "background" else PushType.ALERT

        request = NotificationRequest(
            device_token=device_token,
            message=message,
            push_type=apns_push_type,
        )

        client = self._get_client(environment)

        try:
            response = await client.send_notification(request)
        except Exception:
            _LOGGER.exception("APNs send failed for token %s… (%s)", device_token[:8], environment)
            return (False, "connection_error")

        if response.is_successful:
            _LOGGER.debug("APNs push sent to %s… (%s)", device_token[:8], environment)
            return (True, None)

        reason = response.description
        _LOGGER.warning(
            "APNs rejected push for token %s… (%s): %s",
            device_token[:8],
            environment,
            reason,
        )
        return (False, reason)

    @staticmethod
    def is_dead_token(reason: str | None) -> bool:
        """Return True if the APNs reason indicates the token is permanently invalid."""
        return reason in _DEAD_TOKEN_REASONS
