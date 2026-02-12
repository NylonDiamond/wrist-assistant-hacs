"""Diagnostic sensors for Wrist Assistant."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import DeltaCoordinator, MAX_EVENTS_BUFFER
from .const import DATA_COORDINATOR, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Wrist Assistant sensors."""
    coordinator: DeltaCoordinator = hass.data[DOMAIN][DATA_COORDINATOR]

    global_sensors: list[SensorEntity] = [
        ActiveWatchesSensor(coordinator, entry),
        MonitoredEntitiesSensor(coordinator, entry),
        EventsProcessedSensor(coordinator, entry),
        EventBufferUsageSensor(coordinator, entry),
    ]
    async_add_entities(global_sensors)

    known_watches: set[str] = set()

    @callback
    def _check_new_watches() -> None:
        new_entities: list[SensorEntity] = []
        for watch_id in coordinator._sessions:
            if watch_id not in known_watches:
                known_watches.add(watch_id)
                new_entities.extend([
                    WatchLastActivitySensor(coordinator, entry, watch_id),
                    WatchSubscribedEntitiesSensor(coordinator, entry, watch_id),
                    WatchEntityListSensor(coordinator, entry, watch_id),
                ])
        if new_entities:
            async_add_entities(new_entities)

    _check_new_watches()
    entry.async_on_unload(
        coordinator.async_add_session_listener(_check_new_watches)
    )


class _WristAssistantSensorBase(SensorEntity):
    """Base for global Wrist Assistant sensors."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: DeltaCoordinator, entry: ConfigEntry
    ) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Wrist Assistant",
            manufacturer="Wrist Assistant",
            model="Delta Coordinator",
            entry_type=DeviceEntryType.SERVICE,
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


class ActiveWatchesSensor(_WristAssistantSensorBase):
    """Number of connected watch sessions."""

    _attr_name = "Active watches"
    _attr_icon = "mdi:watch"
    _attr_native_unit_of_measurement = "watches"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: DeltaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"wrist_assistant_{entry.entry_id}_active_watches"

    @property
    def native_value(self) -> int:
        return len(self._coordinator._sessions)


class MonitoredEntitiesSensor(_WristAssistantSensorBase):
    """Total entity subscriptions across all watches."""

    _attr_name = "Monitored entities"
    _attr_icon = "mdi:eye"
    _attr_native_unit_of_measurement = "entities"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: DeltaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"wrist_assistant_{entry.entry_id}_monitored_entities"

    @property
    def native_value(self) -> int:
        return sum(
            len(s.entities) for s in self._coordinator._sessions.values()
        )


class EventsProcessedSensor(_WristAssistantSensorBase):
    """Monotonic counter of state changes seen."""

    _attr_name = "Events processed"
    _attr_icon = "mdi:counter"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator: DeltaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"wrist_assistant_{entry.entry_id}_events_processed"

    @property
    def native_value(self) -> int:
        return self._coordinator._cursor


class EventBufferUsageSensor(_WristAssistantSensorBase):
    """Ring buffer saturation percentage."""

    _attr_name = "Event buffer usage"
    _attr_icon = "mdi:memory"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: DeltaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"wrist_assistant_{entry.entry_id}_buffer_usage"

    @property
    def native_value(self) -> float:
        return round(len(self._coordinator._events) / MAX_EVENTS_BUFFER * 100, 1)


# --- Per-watch sensors ---


class _WatchSensorBase(SensorEntity):
    """Base for per-watch sensors."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: DeltaCoordinator,
        entry: ConfigEntry,
        watch_id: str,
    ) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._watch_id = watch_id
        short_id = watch_id[:8]
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


class WatchLastActivitySensor(_WatchSensorBase):
    """Timestamp of last poll from this watch."""

    _attr_name = "Last activity"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-outline"

    def __init__(
        self, coordinator: DeltaCoordinator, entry: ConfigEntry, watch_id: str
    ) -> None:
        super().__init__(coordinator, entry, watch_id)
        self._attr_unique_id = f"wrist_assistant_{watch_id}_last_activity"

    @property
    def native_value(self):
        session = self._coordinator._sessions.get(self._watch_id)
        if session is None:
            return None
        return session.last_seen


class WatchSubscribedEntitiesSensor(_WatchSensorBase):
    """Number of entities this watch monitors."""

    _attr_name = "Subscribed entities"
    _attr_icon = "mdi:format-list-bulleted"
    _attr_native_unit_of_measurement = "entities"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self, coordinator: DeltaCoordinator, entry: ConfigEntry, watch_id: str
    ) -> None:
        super().__init__(coordinator, entry, watch_id)
        self._attr_unique_id = f"wrist_assistant_{watch_id}_subscribed_entities"

    @property
    def native_value(self) -> int:
        session = self._coordinator._sessions.get(self._watch_id)
        if session is None:
            return 0
        return len(session.entities)


class WatchEntityListSensor(_WatchSensorBase):
    """Text sensor showing entity IDs this watch monitors."""

    _attr_name = "Watched entities"
    _attr_icon = "mdi:format-list-text"

    def __init__(
        self, coordinator: DeltaCoordinator, entry: ConfigEntry, watch_id: str
    ) -> None:
        super().__init__(coordinator, entry, watch_id)
        self._attr_unique_id = f"wrist_assistant_{watch_id}_entity_list"

    @property
    def native_value(self) -> str:
        session = self._coordinator._sessions.get(self._watch_id)
        if session is None:
            return ""
        count = len(session.entities)
        return f"{count} entities"

    @property
    def extra_state_attributes(self) -> dict:
        session = self._coordinator._sessions.get(self._watch_id)
        if session is None:
            return {}
        return {"entity_ids": sorted(session.entities)}
