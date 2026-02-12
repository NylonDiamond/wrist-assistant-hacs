"""Binary sensors for Wrist Assistant per-watch sync status."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import DeltaCoordinator
from .const import DATA_COORDINATOR, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Wrist Assistant binary sensors."""
    coordinator: DeltaCoordinator = hass.data[DOMAIN][DATA_COORDINATOR]

    known_watches: set[str] = set()

    @callback
    def _check_new_watches() -> None:
        new_entities: list[BinarySensorEntity] = []
        for watch_id in coordinator._sessions:
            if watch_id not in known_watches:
                known_watches.add(watch_id)
                new_entities.append(
                    WatchSyncStatusSensor(coordinator, entry, watch_id)
                )
        if new_entities:
            async_add_entities(new_entities)

    _check_new_watches()
    entry.async_on_unload(
        coordinator.async_add_session_listener(_check_new_watches)
    )


class WatchSyncStatusSensor(BinarySensorEntity):
    """Binary sensor showing whether a watch has synced its entity list."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Sync status"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_icon = "mdi:sync"

    def __init__(
        self,
        coordinator: DeltaCoordinator,
        entry: ConfigEntry,
        watch_id: str,
    ) -> None:
        self._coordinator = coordinator
        self._watch_id = watch_id
        short_id = watch_id[:8]
        self._attr_unique_id = f"wrist_assistant_{watch_id}_sync_status"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"watch_{watch_id}")},
            name=f"Watch {short_id}",
            manufacturer="Wrist Assistant",
            model="Apple Watch",
            via_device=(DOMAIN, entry.entry_id),
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self._coordinator.async_add_session_listener(
                self._handle_update
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._watch_id in self._coordinator._sessions

    @property
    def is_on(self) -> bool | None:
        session = self._coordinator._sessions.get(self._watch_id)
        if session is None:
            return None
        return session.entities_synced
