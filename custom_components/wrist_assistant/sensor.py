"""Diagnostic sensors for Wrist Assistant."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import DeltaCoordinator, MAX_EVENTS_BUFFER
from .const import DATA_COORDINATOR, DOMAIN

SCAN_INTERVAL = timedelta(seconds=30)


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
        EventsPerMinuteSensor(coordinator, entry),
    ]
    async_add_entities(global_sensors)

    known_watches: set[str] = set()

    @callback
    def _check_new_watches() -> None:
        ent_reg = er.async_get(hass)
        new_entities: list[SensorEntity] = []
        for watch_id in coordinator.real_sessions:
            if watch_id in known_watches:
                # Verify entities still exist in registry (user may have deleted device)
                sentinel = f"wrist_assistant_{watch_id}_last_activity"
                if ent_reg.async_get_entity_id("sensor", DOMAIN, sentinel) is not None:
                    continue
                known_watches.discard(watch_id)
            known_watches.add(watch_id)
            new_entities.extend([
                WatchLastActivitySensor(coordinator, entry, watch_id),
                WatchSubscribedEntitiesSensor(coordinator, entry, watch_id),
                WatchPollIntervalSensor(coordinator, entry, watch_id),
                WatchConnectedSinceSensor(coordinator, entry, watch_id),
            ])
        if new_entities:
            async_add_entities(new_entities)

    _check_new_watches()
    entry.async_on_unload(
        coordinator.async_add_session_listener(_check_new_watches)
    )


# --- Global sensors ---


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
        return len(self._coordinator.real_sessions)


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
            len(s.entities) for s in self._coordinator.real_sessions.values()
        )

    @property
    def extra_state_attributes(self) -> dict:
        dev_reg = dr.async_get(self.hass)
        per_watch: dict[str, int] = {}
        for wid, session in self._coordinator.real_sessions.items():
            device = dev_reg.async_get_device(
                identifiers={(DOMAIN, f"watch_{wid}")}
            )
            name = device.name if device else f"Watch {wid[:8]}"
            per_watch[name] = len(session.entities)
        return {"per_watch": per_watch}


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


class EventsPerMinuteSensor(_WristAssistantSensorBase):
    """Rolling count of state change events in the last 60 seconds."""

    _attr_name = "Events per minute"
    _attr_icon = "mdi:chart-line"
    _attr_native_unit_of_measurement = "events/min"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_should_poll = True

    def __init__(self, coordinator: DeltaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"wrist_assistant_{entry.entry_id}_events_per_minute"

    @property
    def native_value(self) -> float:
        return self._coordinator.events_per_minute


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
    """Number of entities this watch monitors, with entity list in attributes."""

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

    @property
    def extra_state_attributes(self) -> dict:
        session = self._coordinator._sessions.get(self._watch_id)
        if session is None:
            return {}
        entities: dict[str, str] = {}
        for eid in sorted(session.entities):
            state = self.hass.states.get(eid)
            entities[eid] = state.name if state else eid
        return {"entities": entities}


class WatchPollIntervalSensor(_WatchSensorBase):
    """Time between consecutive polls from this watch."""

    _attr_name = "Poll interval"
    _attr_icon = "mdi:timer-outline"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(
        self, coordinator: DeltaCoordinator, entry: ConfigEntry, watch_id: str
    ) -> None:
        super().__init__(coordinator, entry, watch_id)
        self._attr_unique_id = f"wrist_assistant_{watch_id}_poll_interval"

    @property
    def native_value(self) -> float | None:
        session = self._coordinator._sessions.get(self._watch_id)
        if session is None or session.last_poll_interval is None:
            return None
        return round(session.last_poll_interval.total_seconds(), 1)


class WatchConnectedSinceSensor(_WatchSensorBase):
    """Timestamp of when this watch first connected in the current session."""

    _attr_name = "Connected since"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:connection"

    def __init__(
        self, coordinator: DeltaCoordinator, entry: ConfigEntry, watch_id: str
    ) -> None:
        super().__init__(coordinator, entry, watch_id)
        self._attr_unique_id = f"wrist_assistant_{watch_id}_connected_since"

    @property
    def native_value(self):
        session = self._coordinator._sessions.get(self._watch_id)
        if session is None:
            return None
        return session.first_seen
