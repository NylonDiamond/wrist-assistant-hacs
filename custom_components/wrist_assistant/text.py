"""Text entities for Wrist Assistant watch naming."""

from __future__ import annotations

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import DeltaCoordinator
from .const import DATA_COORDINATOR, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Wrist Assistant text entities."""
    coordinator: DeltaCoordinator = hass.data[DOMAIN][DATA_COORDINATOR]

    known_watches: set[str] = set()

    @callback
    def _check_new_watches() -> None:
        ent_reg = er.async_get(hass)
        new_entities: list[TextEntity] = []
        for watch_id in coordinator.real_sessions:
            if watch_id in known_watches:
                sentinel = f"wrist_assistant_{watch_id}_name"
                if ent_reg.async_get_entity_id("text", DOMAIN, sentinel) is not None:
                    continue
                known_watches.discard(watch_id)
            known_watches.add(watch_id)
            new_entities.append(
                WatchNameText(coordinator, entry, watch_id)
            )
        if new_entities:
            async_add_entities(new_entities)

    _check_new_watches()
    entry.async_on_unload(
        coordinator.async_add_session_listener(_check_new_watches)
    )


class WatchNameText(TextEntity):
    """Text entity to rename a watch device."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "Name"
    _attr_mode = TextMode.TEXT
    _attr_native_max = 50
    _attr_icon = "mdi:rename"

    def __init__(
        self,
        coordinator: DeltaCoordinator,
        entry: ConfigEntry,
        watch_id: str,
    ) -> None:
        self._coordinator = coordinator
        self._watch_id = watch_id
        self._short_id = watch_id[:8]
        self._attr_unique_id = f"wrist_assistant_{watch_id}_name"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"watch_{watch_id}")},
            name=f"Watch {self._short_id}",
            manufacturer="Wrist Assistant",
            model="Apple Watch",
            via_device=(DOMAIN, entry.entry_id),
        )

    @property
    def native_value(self) -> str:
        dev_reg = dr.async_get(self.hass)
        device = dev_reg.async_get_device(
            identifiers={(DOMAIN, f"watch_{self._watch_id}")}
        )
        if device and device.name_by_user:
            return device.name_by_user
        return f"Watch {self._short_id}"

    async def async_set_value(self, value: str) -> None:
        """Update the device name in the device registry."""
        dev_reg = dr.async_get(self.hass)
        device = dev_reg.async_get_device(
            identifiers={(DOMAIN, f"watch_{self._watch_id}")}
        )
        if device:
            dev_reg.async_update_device(device.id, name_by_user=value)
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._watch_id in self._coordinator._sessions
