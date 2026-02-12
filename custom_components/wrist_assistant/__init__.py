"""Wrist Assistant delta API integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback

from .api import DeltaCoordinator, WatchUpdatesView
from .const import DATA_COORDINATOR, DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Wrist Assistant from a config entry."""
    coordinator = DeltaCoordinator(hass)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][DATA_COORDINATOR] = coordinator
    hass.http.register_view(WatchUpdatesView(coordinator))

    @callback
    def _handle_stop(_event) -> None:
        coordinator.async_shutdown()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _handle_stop)
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    data = hass.data.get(DOMAIN)
    if data and DATA_COORDINATOR in data:
        data[DATA_COORDINATOR].async_shutdown()
        data.pop(DATA_COORDINATOR, None)
    return True
