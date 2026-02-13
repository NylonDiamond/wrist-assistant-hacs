"""Wrist Assistant delta API integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .api import DeltaCoordinator, WatchUpdatesView
from .const import DATA_COORDINATOR, DOMAIN, PLATFORMS, SERVICE_FORCE_RESYNC

_LOGGER = logging.getLogger(__name__)

# Unique ID suffixes from removed entity classes (cleanup on upgrade)
_ORPHANED_SUFFIXES = ("_entity_list",)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Wrist Assistant from a config entry."""
    _cleanup_orphaned_entities(hass, entry)

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

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _handle_force_resync(call: ServiceCall) -> None:
        coordinator.async_force_resync()

    hass.services.async_register(DOMAIN, SERVICE_FORCE_RESYNC, _handle_force_resync)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data = hass.data.get(DOMAIN)
        if data and DATA_COORDINATOR in data:
            data[DATA_COORDINATOR].async_shutdown()
            data.pop(DATA_COORDINATOR, None)
        hass.services.async_remove(DOMAIN, SERVICE_FORCE_RESYNC)
    return unload_ok


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Allow removal of a device from the UI."""
    return True


def _cleanup_orphaned_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove entities from previous versions that no longer exist in code."""
    ent_reg = er.async_get(hass)
    removed = []
    for entity_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if any(entity_entry.unique_id.endswith(suffix) for suffix in _ORPHANED_SUFFIXES):
            ent_reg.async_remove(entity_entry.entity_id)
            removed.append(entity_entry.entity_id)
    if removed:
        _LOGGER.info("Cleaned up %d orphaned entities: %s", len(removed), removed)
