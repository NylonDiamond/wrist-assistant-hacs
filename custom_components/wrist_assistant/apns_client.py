"""APNs push notification client for Wrist Assistant."""

from __future__ import annotations

import logging
import ssl
from pathlib import Path

from aioapns import APNs, NotificationRequest, PushType

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

    APNs instances are created lazily because their constructor requires
    an active asyncio event loop.
    """

    def __init__(
        self,
        key_content: str,
        *,
        key_id: str,
        team_id: str,
        topic: str,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        if not key_id or not team_id:
            raise ValueError("APNs key_id and team_id are required")
        if not topic:
            raise ValueError("APNs topic is required")
        self._key_content = key_content
        self._key_id = key_id
        self._team_id = team_id
        self._topic = topic
        self._ssl_context = ssl_context
        self._production: APNs | None = None
        self._sandbox: APNs | None = None

    def _get_client(self, environment: str) -> APNs:
        """Return the APNs client for the given environment, creating lazily."""
        ssl_kwargs = {"ssl_context": self._ssl_context} if self._ssl_context else {}
        if environment == "development":
            if self._sandbox is None:
                self._sandbox = APNs(
                    key=self._key_content,
                    key_id=self._key_id,
                    team_id=self._team_id,
                    topic=self._topic,
                    use_sandbox=True,
                    **ssl_kwargs,
                )
            return self._sandbox
        if self._production is None:
            self._production = APNs(
                key=self._key_content,
                key_id=self._key_id,
                team_id=self._team_id,
                topic=self._topic,
                use_sandbox=False,
                **ssl_kwargs,
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
    ) -> tuple[bool, str | None, str]:
        """Send a push notification.

        Returns (success, reason, used_environment). On BadDeviceToken the
        opposite environment is tried automatically — if it succeeds,
        ``used_environment`` will differ from the requested one so the
        caller can update its records.
        """
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

        # Extract grouping/priority fields from data before merging
        collapse_key: str | None = None
        if data:
            data = dict(data)  # Don't mutate caller's dict
            if group := data.pop("group", None):
                aps["thread-id"] = group
            if tag := data.pop("tag", None):
                collapse_key = tag
            if priority := data.pop("priority", None):
                valid_levels = ("passive", "active", "time-sensitive", "critical")
                if priority in valid_levels:
                    aps["interruption-level"] = priority
                else:
                    _LOGGER.warning(
                        "Ignoring invalid interruption-level '%s' (valid: %s)",
                        priority,
                        ", ".join(valid_levels),
                    )

        message: dict = {"aps": aps}
        if data:
            for key, value in data.items():
                message[key] = value

        apns_push_type = PushType.BACKGROUND if push_type == "background" else PushType.ALERT

        request = NotificationRequest(
            device_token=device_token,
            message=message,
            push_type=apns_push_type,
            collapse_key=collapse_key,
        )

        success, reason = await self._send_once(request, device_token, environment)
        if success:
            return (True, None, environment)

        # On BadDeviceToken, try the other environment before giving up.
        if reason == "BadDeviceToken":
            alt = "development" if environment == "production" else "production"
            alt_success, alt_reason = await self._send_once(request, device_token, alt)
            if alt_success:
                _LOGGER.info(
                    "APNs push for %s… succeeded on %s (was registered as %s) — correcting",
                    device_token[:8], alt, environment,
                )
                return (True, None, alt)
            # Both failed — return the original reason (more relevant).

        return (False, reason, environment)

    async def _send_once(
        self,
        request: NotificationRequest,
        device_token: str,
        environment: str,
    ) -> tuple[bool, str | None]:
        """Try a single send attempt. Returns (success, reason)."""
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
