"""Diagnostics support for Wrist Assistant."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import MAX_EVENTS_BUFFER
from .const import (
    DATA_APNS_CONFIG_STORE,
    DATA_APNS_CLIENT,
    DATA_COORDINATOR,
    DATA_NOTIFICATION_TOKEN_STORE,
    DOMAIN,
)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data = hass.data[DOMAIN]
    coordinator = data[DATA_COORDINATOR]

    sessions = {}
    for watch_id, session in coordinator._sessions.items():
        sessions[watch_id] = {
            "config_hash": session.config_hash,
            "entities_synced": session.entities_synced,
            "entity_count": len(session.entities),
            "entities": sorted(session.entities),
            "last_seen": session.last_seen.isoformat(),
        }

    notification_store = data.get(DATA_NOTIFICATION_TOKEN_STORE)
    notification_tokens = {}
    if notification_store:
        for watch_id, token_entry in notification_store.all_tokens.items():
            notification_tokens[watch_id] = {
                "token_prefix": token_entry.device_token[:8] + "…",
                "platform": token_entry.platform,
                "environment": token_entry.environment,
            }

    return {
        "coordinator": {
            "cursor": coordinator._cursor,
            "generation": coordinator._generation,
            "event_buffer_size": len(coordinator._events),
            "event_buffer_capacity": MAX_EVENTS_BUFFER,
            "event_buffer_usage_pct": round(
                len(coordinator._events) / MAX_EVENTS_BUFFER * 100, 1
            ),
            "session_count": len(coordinator._sessions),
        },
        "sessions": sessions,
        "notifications": {
            "token_count": len(notification_tokens),
            "tokens": notification_tokens,
            "apns_configured": DATA_APNS_CLIENT in data,
            "apns_config_managed": bool(
                data.get(DATA_APNS_CONFIG_STORE)
                and data[DATA_APNS_CONFIG_STORE].is_configured
            ),
        },
    }
