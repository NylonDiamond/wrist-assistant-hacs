"""Diagnostics support for Wrist Assistant."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import MAX_EVENTS_BUFFER
from .const import DATA_COORDINATOR, DOMAIN


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = hass.data[DOMAIN][DATA_COORDINATOR]

    sessions = {}
    for watch_id, session in coordinator._sessions.items():
        sessions[watch_id] = {
            "config_hash": session.config_hash,
            "entities_synced": session.entities_synced,
            "entity_count": len(session.entities),
            "entities": sorted(session.entities),
            "last_seen": session.last_seen.isoformat(),
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
    }
