"""Wrist Assistant delta API integration."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.typing import ConfigType

from .api import DeltaCoordinator, WatchUpdatesView
from .const import DATA_COORDINATOR, DOMAIN

CONFIG_SCHEMA = vol.Schema({DOMAIN: vol.Schema({})}, extra=vol.ALLOW_EXTRA)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Wrist Assistant integration."""
    coordinator = DeltaCoordinator(hass)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][DATA_COORDINATOR] = coordinator
    hass.http.register_view(WatchUpdatesView(coordinator))

    @callback
    def _handle_stop(_event) -> None:
        coordinator.async_shutdown()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _handle_stop)
    return True
