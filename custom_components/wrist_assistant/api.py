"""HTTP API for Wrist Assistant long-poll delta updates."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import gzip
import json as _json
import logging
import secrets
from typing import Any
from urllib.parse import urlencode

from aiohttp.web import Request, Response

from homeassistant.auth import models as auth_models
from homeassistant.components.http import HomeAssistantView
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util


DEFAULT_TIMEOUT_SECONDS = 45
MIN_TIMEOUT_SECONDS = 5
MAX_TIMEOUT_SECONDS = 55
MAX_EVENTS_BUFFER = 5000
MAX_EVENTS_PER_RESPONSE = 250
SESSION_TTL = timedelta(minutes=5)
PAIRING_CODE_TTL = timedelta(minutes=10)
PAIRING_CLIENT_ID = "https://home-assistant.io/iOS/dev-auth"
PAIRING_CLIENT_NAME = "Wrist Assistant Pairing"
PAIRING_DEFAULT_LIFESPAN_DAYS = 3650

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class WatchSession:
    """Per-watch subscription data."""

    watch_id: str
    config_hash: str = ""
    entities: set[str] = field(default_factory=set)
    entities_synced: bool = False
    last_seen: datetime = field(default_factory=dt_util.utcnow)
    first_seen: datetime = field(default_factory=dt_util.utcnow)
    last_poll_interval: timedelta | None = None


@dataclass(slots=True)
class DeltaEvent:
    """Single tracked entity update."""

    cursor: int
    entity_id: str
    payload: dict[str, Any]


@dataclass(slots=True)
class PairingSession:
    """Single-use pairing payload."""

    code: str
    refresh_token_id: str
    home_assistant_url: str
    local_url: str
    remote_url: str
    expires_at: datetime
    lifespan_days: int


_SLIM_ATTRIBUTES: dict[str, set[str]] = {
    "light": {
        "friendly_name", "brightness", "color_temp", "color_temp_kelvin",
        "rgb_color", "hs_color", "xy_color", "color_mode", "supported_color_modes",
        "min_mireds", "max_mireds", "min_color_temp_kelvin", "max_color_temp_kelvin",
        "effect", "effect_list", "supported_features", "rgbw_color",
        "icon", "entity_picture",
    },
    "switch": {
        "friendly_name", "device_class", "icon", "entity_picture",
    },
    "cover": {
        "friendly_name", "device_class", "current_position", "current_tilt_position",
        "supported_features", "icon", "entity_picture",
    },
    "climate": {
        "friendly_name", "hvac_modes", "hvac_action", "current_temperature",
        "temperature", "target_temp_high", "target_temp_low", "fan_mode", "fan_modes",
        "preset_mode", "preset_modes", "humidity", "current_humidity", "target_temp_step",
        "supported_features",
        "min_temp", "max_temp", "icon", "entity_picture",
    },
    "fan": {
        "friendly_name", "percentage", "preset_mode", "preset_modes",
        "oscillating", "direction", "percentage_step", "supported_features",
        "icon", "entity_picture",
    },
    "lock": {
        "friendly_name", "icon", "entity_picture",
    },
    "media_player": {
        "friendly_name", "media_title", "media_artist", "media_album_name",
        "media_content_type", "media_duration", "media_position",
        "media_position_updated_at", "app_name", "group_members",
        "volume_level", "is_volume_muted", "source", "source_list",
        "sound_mode", "sound_mode_list", "shuffle", "repeat",
        "supported_features", "entity_picture",
        "icon", "device_class",
    },
    "camera": {
        "friendly_name", "entity_picture", "frontend_stream_type", "icon",
    },
    "binary_sensor": {
        "friendly_name", "device_class", "icon", "entity_picture",
    },
    "sensor": {
        "friendly_name", "device_class", "unit_of_measurement", "state_class",
        "supported_features", "icon", "entity_picture",
    },
    "person": {
        "friendly_name", "entity_picture", "gps_accuracy", "latitude", "longitude",
        "source", "icon",
    },
    "alarm_control_panel": {
        "friendly_name", "code_arm_required", "code_format", "changed_by",
        "supported_features", "icon",
        "entity_picture",
    },
    "vacuum": {
        "friendly_name", "battery_level", "fan_speed", "fan_speed_list",
        "status", "icon", "entity_picture",
    },
    "input_boolean": {
        "friendly_name", "icon", "entity_picture",
    },
    "input_number": {
        "friendly_name", "min", "max", "step", "mode",
        "unit_of_measurement", "icon", "entity_picture",
    },
    "number": {
        "friendly_name", "min", "max", "step", "mode",
        "unit_of_measurement", "icon", "entity_picture",
    },
    "input_select": {
        "friendly_name", "options", "icon", "entity_picture",
    },
    "select": {
        "friendly_name", "options", "icon", "entity_picture",
    },
    "scene": {
        "friendly_name", "icon", "entity_picture",
    },
    "script": {
        "friendly_name", "icon", "entity_picture",
    },
    "automation": {
        "friendly_name", "last_triggered", "mode", "icon", "entity_picture",
    },
    "timer": {
        "friendly_name", "duration", "remaining", "finishes_at", "icon",
    },
    "remote": {
        "friendly_name", "activity_list", "current_activity", "icon",
        "entity_picture",
    },
    "button": {
        "friendly_name", "device_class", "icon", "entity_picture",
    },
    "input_button": {
        "friendly_name", "icon", "entity_picture",
    },
}


class DeltaCoordinator:
    """Tracks state changes and serves filtered long-poll responses."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._sessions: dict[str, WatchSession] = {}
        self._events: deque[DeltaEvent] = deque(maxlen=MAX_EVENTS_BUFFER)
        self._cursor = 0
        self._generation = 0
        self._generation_event: asyncio.Event = asyncio.Event()
        self._event_times: deque[float] = deque(maxlen=MAX_EVENTS_BUFFER)
        self._session_callbacks: list[callback] = []
        self._capabilities: set[str] = {"smart_camera_stream"}
        self._unsub_state_changed = hass.bus.async_listen(
            EVENT_STATE_CHANGED, self._handle_state_changed
        )

    def register_capability(self, cap: str) -> None:
        """Register a server capability advertised to clients."""
        self._capabilities.add(cap)

    @callback
    def async_add_session_listener(self, cb: callback) -> callback:
        """Register a callback fired when sessions change. Returns unsubscribe."""
        self._session_callbacks.append(cb)

        @callback
        def _unsub() -> None:
            self._session_callbacks.remove(cb)

        return _unsub

    @callback
    def _fire_session_callbacks(self) -> None:
        """Notify all session listeners."""
        for cb in self._session_callbacks:
            cb()

    @callback
    def async_shutdown(self) -> None:
        """Clean up listeners."""
        if self._unsub_state_changed is not None:
            self._unsub_state_changed()
            self._unsub_state_changed = None

    @property
    def events_per_minute(self) -> float:
        """Return the number of state change events in the last 60 seconds."""
        if not self._event_times:
            return 0.0
        cutoff = self.hass.loop.time() - 60
        count = 0
        for t in reversed(self._event_times):
            if t < cutoff:
                break
            count += 1
        return float(count)

    @callback
    def async_force_resync(self) -> None:
        """Clear all sessions, forcing watches to do a full state refresh."""
        self._sessions.clear()
        self._fire_session_callbacks()

    async def handle_poll(
        self,
        watch_id: str,
        since: str | None,
        config_hash: str,
        entities: list[str] | None,
        timeout: int,
        force_delta: bool = False,
        battery_threshold: int = 20,
        summary_entities: dict[str, list[str]] | None = None,
        slim: bool = False,
        include_summary: bool = False,
    ) -> tuple[int, dict[str, Any] | None]:
        """Handle a single long-poll request."""
        self._prune_sessions()
        session = self._sessions.get(watch_id)
        is_new_session = session is None
        if is_new_session:
            session = WatchSession(watch_id=watch_id)
            self._sessions[watch_id] = session

        now = dt_util.utcnow()
        if not is_new_session:
            session.last_poll_interval = now - session.last_seen
        session.last_seen = now

        if entities is not None:
            session.entities = {entity_id for entity_id in entities if isinstance(entity_id, str)}
            session.config_hash = config_hash
            session.entities_synced = True
        elif session.config_hash != config_hash:
            # Watch config changed, ask client to send the latest entity list.
            session.config_hash = config_hash
            session.entities.clear()
            session.entities_synced = False

        self._fire_session_callbacks()

        if not session.entities_synced:
            return 200, self._response_payload(
                events=[],
                next_cursor=str(self._cursor),
                need_entities=True,
                resync_required=False,
                battery_threshold=battery_threshold,
                summary_entities=summary_entities,
                include_summary=include_summary,
            )

        # When since is nil, the client is requesting a full state snapshot.
        # Fetch current state directly from HA's state machine (in-memory, instant).
        if since is None or since == "":
            snapshot_events = self._snapshot_current_state(session.entities, slim=slim)
            return 200, self._response_payload(
                events=snapshot_events,
                next_cursor=str(self._cursor),
                need_entities=False,
                resync_required=False,
                battery_threshold=battery_threshold,
                summary_entities=summary_entities,
                include_summary=include_summary,
            )

        since_cursor, invalid_since = self._parse_since(
            since=since, default_cursor=self._cursor
        )
        if invalid_since:
            return 410, self._response_payload(
                events=[],
                next_cursor=str(self._cursor),
                need_entities=False,
                resync_required=True,
                battery_threshold=battery_threshold,
                summary_entities=summary_entities,
                include_summary=include_summary,
            )

        if self._is_stale_cursor(since_cursor):
            return 410, self._response_payload(
                events=[],
                next_cursor=str(self._cursor),
                need_entities=False,
                resync_required=True,
                battery_threshold=battery_threshold,
                summary_entities=summary_entities,
                include_summary=include_summary,
            )

        events, next_cursor = self._collect_events(
            since_cursor=since_cursor,
            entities=session.entities,
            limit=MAX_EVENTS_PER_RESPONSE,
            slim=slim,
        )
        if events:
            return 200, self._response_payload(
                events=events,
                next_cursor=str(next_cursor),
                need_entities=False,
                resync_required=False,
                include_details=force_delta,
                battery_threshold=battery_threshold,
                summary_entities=summary_entities,
                include_summary=include_summary,
            )

        # Force delta: skip long-poll wait, return immediately with detailed info_summary
        if force_delta:
            return 200, self._response_payload(
                events=[],
                next_cursor=str(next_cursor),
                need_entities=False,
                resync_required=False,
                include_details=True,
                battery_threshold=battery_threshold,
                summary_entities=summary_entities,
                include_summary=include_summary,
            )

        deadline = self.hass.loop.time() + timeout
        observed_generation = self._generation
        try:
            while True:
                remaining = deadline - self.hass.loop.time()
                if remaining <= 0:
                    return 204, None

                if self._generation != observed_generation:
                    observed_generation = self._generation
                    events, next_cursor = self._collect_events(
                        since_cursor=since_cursor,
                        entities=session.entities,
                        limit=MAX_EVENTS_PER_RESPONSE,
                        slim=slim,
                    )
                    if events:
                        return 200, self._response_payload(
                            events=events,
                            next_cursor=str(next_cursor),
                            need_entities=False,
                            resync_required=False,
                            battery_threshold=battery_threshold,
                            summary_entities=summary_entities,
                            include_summary=include_summary,
                        )
                    since_cursor = next_cursor
                    continue

                try:
                    await asyncio.wait_for(self._generation_event.wait(), timeout=remaining)
                except TimeoutError:
                    return 204, None

                self._generation_event.clear()
                observed_generation = self._generation

                events, next_cursor = self._collect_events(
                    since_cursor=since_cursor,
                    entities=session.entities,
                    limit=MAX_EVENTS_PER_RESPONSE,
                    slim=slim,
                )
                if events:
                    return 200, self._response_payload(
                        events=events,
                        next_cursor=str(next_cursor),
                        need_entities=False,
                        resync_required=False,
                        battery_threshold=battery_threshold,
                        summary_entities=summary_entities,
                        include_summary=include_summary,
                    )
                since_cursor = next_cursor
        except asyncio.CancelledError:
            self._sessions.pop(watch_id, None)
            self._fire_session_callbacks()
            raise

    @callback
    def _handle_state_changed(self, event: Event) -> None:
        """Track every state change in a bounded in-memory ring buffer."""
        new_state: State | None = event.data.get("new_state")
        if new_state is None:
            return

        self._cursor += 1
        payload = {
            "entity_id": new_state.entity_id,
            "state": new_state.state,
            "new_state": self._state_to_payload(new_state),
            "context_id": new_state.context.id if new_state.context is not None else None,
            "last_updated": new_state.last_updated.isoformat(),
        }
        self._events.append(
            DeltaEvent(
                cursor=self._cursor,
                entity_id=new_state.entity_id,
                payload=payload,
            )
        )
        self._event_times.append(self.hass.loop.time())
        self._generation += 1
        self._generation_event.set()

    def _snapshot_current_state(
        self, entities: set[str], *, slim: bool = False
    ) -> list[dict[str, Any]]:
        """Build a full state snapshot from HA's state machine for the given entities."""
        to_payload = self._slim_state_to_payload if slim else self._state_to_payload
        snapshot: list[dict[str, Any]] = []
        for entity_id in entities:
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            snapshot.append(
                {
                    "entity_id": state.entity_id,
                    "state": state.state,
                    "new_state": to_payload(state),
                    "context_id": (
                        state.context.id if state.context is not None else None
                    ),
                    "last_updated": state.last_updated.isoformat(),
                }
            )
        return snapshot

    def _collect_events(
        self, since_cursor: int, entities: set[str], limit: int, *, slim: bool = False
    ) -> tuple[list[dict[str, Any]], int]:
        """Collect filtered events after the provided cursor."""
        matched: list[dict[str, Any]] = []
        last_sent_cursor = since_cursor
        for event in self._events:
            if event.cursor <= since_cursor:
                continue
            if event.entity_id not in entities:
                continue

            payload = event.payload
            if slim:
                payload = self._slim_event_payload(payload)
            matched.append(payload)
            last_sent_cursor = event.cursor
            if len(matched) >= limit:
                break

        if matched:
            return matched, last_sent_cursor
        return [], since_cursor

    def _is_stale_cursor(self, since_cursor: int) -> bool:
        """Return True if requested cursor is out of range.

        Covers two cases:
        - Cursor is older than the oldest retained event (buffer overflow).
        - Cursor is ahead of the current server cursor (HA restarted and
          the coordinator's cursor reset to 0 while the watch kept its old
          cursor from a previous instance).
        """
        if since_cursor > self._cursor:
            return True
        if not self._events:
            return False
        oldest_cursor = self._events[0].cursor
        return since_cursor < (oldest_cursor - 1)

    @staticmethod
    def _parse_since(since: str | None, default_cursor: int) -> tuple[int, bool]:
        """Parse the client cursor."""
        if since is None or since == "":
            return default_cursor, False
        try:
            cursor = int(since)
        except ValueError:
            return 0, True
        return max(cursor, 0), False

    @property
    def real_sessions(self) -> dict[str, "WatchSession"]:
        """Return sessions excluding diagnostic probes."""
        return {
            wid: s
            for wid, s in self._sessions.items()
            if not wid.startswith("__") or not wid.endswith("__")
        }

    def _prune_sessions(self) -> None:
        """Drop idle watch sessions."""
        cutoff = dt_util.utcnow() - SESSION_TTL
        expired = [
            watch_id
            for watch_id, session in self._sessions.items()
            if session.last_seen < cutoff
        ]
        for watch_id in expired:
            self._sessions.pop(watch_id, None)
        if expired:
            self._fire_session_callbacks()

    def _response_payload(
        self,
        events: list[dict[str, Any]],
        next_cursor: str,
        need_entities: bool,
        resync_required: bool,
        include_details: bool = False,
        include_summary: bool = False,
        battery_threshold: int = 20,
        summary_entities: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "events": events,
            "next_cursor": next_cursor,
            "need_entities": need_entities,
            "resync_required": resync_required,
            "capabilities": sorted(self._capabilities),
        }
        if include_summary or include_details:
            payload["info_summary"] = self._compute_info_summary(
                include_details=include_details,
                battery_threshold=battery_threshold,
                summary_entities=summary_entities,
            )
        return payload

    def _compute_info_summary(self, *, include_details: bool = False, battery_threshold: int = 20, summary_entities: dict[str, list[str]] | None = None) -> dict[str, Any]:
        """Compute domain summaries from HA state machine (in-memory, instant).

        When summary_entities is provided, filter each domain to only the requested
        entity IDs and recompute counts from the filtered set. Entity details are
        always included for filtered domains (the caller asked for specific entities).
        """
        summary: dict[str, Any] = {}
        light_filter = (summary_entities or {}).get("light")
        person_filter = (summary_entities or {}).get("person")
        sensor_filter = (summary_entities or {}).get("sensor")
        binary_filter = (summary_entities or {}).get("binary_sensor")

        # Lights
        light_states = [
            s for s in self.hass.states.async_all("light")
            if s.entity_id.startswith("light.")
        ]
        if light_filter:
            light_filter_set = set(light_filter)
            light_states = [s for s in light_states if s.entity_id in light_filter_set]
        light_on = sum(1 for s in light_states if s.state == "on")
        light_data: dict[str, Any] = {"on": light_on, "total": len(light_states)}
        if include_details or light_filter:
            light_data["entities"] = [
                {
                    "entity_id": s.entity_id,
                    "state": s.state,
                    "name": s.attributes.get("friendly_name", s.entity_id),
                    "brightness": s.attributes.get("brightness"),
                }
                for s in light_states
            ]
        summary["light"] = light_data

        # Persons
        person_states = [
            s for s in self.hass.states.async_all("person")
            if s.entity_id.startswith("person.")
        ]
        if person_filter:
            person_filter_set = set(person_filter)
            person_states = [s for s in person_states if s.entity_id in person_filter_set]
        person_home = sum(1 for s in person_states if s.state == "home")
        person_data: dict[str, Any] = {"home": person_home, "total": len(person_states)}
        if include_details or person_filter:
            person_data["entities"] = [
                {
                    "entity_id": s.entity_id,
                    "state": s.state,
                    "name": s.attributes.get("friendly_name", s.entity_id),
                }
                for s in person_states
            ]
        summary["person"] = person_data

        # Sensors (temperature/humidity)
        sensor_states = [
            s for s in self.hass.states.async_all("sensor")
            if s.entity_id.startswith("sensor.")
            and s.attributes.get("device_class") in ("temperature", "humidity")
        ]
        if sensor_filter:
            sensor_filter_set = set(sensor_filter)
            sensor_states = [s for s in sensor_states if s.entity_id in sensor_filter_set]
        sensor_data: dict[str, Any] = {"total": len(sensor_states)}
        if include_details or sensor_filter:
            sensor_data["entities"] = [
                {
                    "entity_id": s.entity_id,
                    "state": s.state,
                    "name": s.attributes.get("friendly_name", s.entity_id),
                    "unit": s.attributes.get("unit_of_measurement"),
                }
                for s in sensor_states
            ]
        summary["sensor"] = sensor_data

        # Binary sensors (door/window/opening)
        binary_states = [
            s for s in self.hass.states.async_all("binary_sensor")
            if s.entity_id.startswith("binary_sensor.")
            and s.attributes.get("device_class") in ("door", "window", "opening", "garage_door")
        ]
        if binary_filter:
            binary_filter_set = set(binary_filter)
            binary_states = [s for s in binary_states if s.entity_id in binary_filter_set]
        binary_open = sum(1 for s in binary_states if s.state == "on")
        binary_data: dict[str, Any] = {"open": binary_open, "total": len(binary_states)}
        if include_details or binary_filter:
            binary_data["entities"] = [
                {
                    "entity_id": s.entity_id,
                    "state": s.state,
                    "name": s.attributes.get("friendly_name", s.entity_id),
                    "device_class": s.attributes.get("device_class"),
                }
                for s in binary_states
            ]
        summary["binary_sensor"] = binary_data

        # Battery sensors (device_class=battery, state is numeric percentage)
        LOW_BATTERY_THRESHOLD = battery_threshold
        battery_states = [
            s for s in self.hass.states.async_all("sensor")
            if s.entity_id.startswith("sensor.")
            and s.attributes.get("device_class") == "battery"
        ]
        # Parse numeric state values, skip unavailable/unknown
        battery_levels: list[tuple[Any, float]] = []
        for s in battery_states:
            try:
                level = float(s.state)
                battery_levels.append((s, level))
            except (ValueError, TypeError):
                continue
        low_count = sum(1 for _, lvl in battery_levels if lvl < LOW_BATTERY_THRESHOLD)
        battery_data: dict[str, Any] = {"low": low_count, "total": len(battery_levels)}
        if include_details:
            # Send all battery entities (watch filters by user-selected entity IDs)
            # Sort by level ascending (most critical first)
            battery_levels.sort(key=lambda x: x[1])
            battery_data["entities"] = [
                {
                    "entity_id": s.entity_id,
                    "name": s.attributes.get("friendly_name", s.entity_id),
                    "level": int(lvl),
                }
                for s, lvl in battery_levels
            ]
        summary["battery"] = battery_data

        return summary

    def _state_to_payload(self, state: State) -> dict[str, Any]:
        """Return HA state payload shape expected by the watch client."""
        return {
            "entity_id": state.entity_id,
            "state": state.state,
            "attributes": self._json_safe(state.attributes),
            "last_updated": state.last_updated.isoformat(),
        }

    def _slim_state_to_payload(self, state: State) -> dict[str, Any]:
        """Return a state payload with attributes filtered to domain whitelist."""
        domain = state.entity_id.split(".", 1)[0] if "." in state.entity_id else ""
        allowed = _SLIM_ATTRIBUTES.get(domain)
        if allowed is not None:
            attrs = {k: v for k, v in state.attributes.items() if k in allowed}
        else:
            attrs = dict(state.attributes)
        return {
            "entity_id": state.entity_id,
            "state": state.state,
            "attributes": self._json_safe(attrs),
            "last_updated": state.last_updated.isoformat(),
        }

    def _slim_event_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Post-filter an event payload's new_state attributes for slim mode."""
        new_state = payload.get("new_state")
        if not isinstance(new_state, dict):
            return payload
        attrs = new_state.get("attributes")
        if not isinstance(attrs, dict):
            return payload
        entity_id = new_state.get("entity_id", payload.get("entity_id", ""))
        domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
        allowed = _SLIM_ATTRIBUTES.get(domain)
        if allowed is None:
            return payload
        trimmed = {k: v for k, v in attrs.items() if k in allowed}
        return {
            **payload,
            "new_state": {**new_state, "attributes": trimmed},
        }

    def _json_safe(self, value: Any) -> Any:
        """Best-effort conversion for attribute values into JSON-safe types."""
        if value is None or isinstance(value, (bool, int, float, str)):
            return value

        if isinstance(value, dict):
            return {str(key): self._json_safe(item) for key, item in value.items()}

        if isinstance(value, (list, tuple, set)):
            return [self._json_safe(item) for item in value]

        if isinstance(value, datetime):
            return value.isoformat()

        if isinstance(value, timedelta):
            return value.total_seconds()

        enum_value = getattr(value, "value", None)
        if enum_value is not None and not callable(enum_value):
            return self._json_safe(enum_value)

        return str(value)


class PairingCoordinator:
    """Issues and redeems short-lived one-time pairing codes."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._sessions: dict[str, PairingSession] = {}
        self._active_code: str | None = None
        self._active_payload: dict[str, Any] | None = None
        self._active_callbacks: list[callback] = []

    @callback
    def async_add_active_listener(self, cb: callback) -> callback:
        """Register callback for active pairing updates."""
        self._active_callbacks.append(cb)

        @callback
        def _unsub() -> None:
            self._active_callbacks.remove(cb)

        return _unsub

    @property
    def active_payload(self) -> dict[str, Any] | None:
        """Return currently active pairing payload."""
        if self._active_code is None:
            return None
        if self._active_code not in self._sessions:
            return None
        return self._active_payload

    async def async_create_pairing_code(
        self,
        user: auth_models.User,
        *,
        home_assistant_url: str,
        local_url: str,
        remote_url: str,
        lifespan_days: int = PAIRING_DEFAULT_LIFESPAN_DAYS,
    ) -> dict[str, Any]:
        """Create a one-time code and return a pairing payload."""
        self._prune_expired()

        code = secrets.token_urlsafe(24)
        client_name = f"{PAIRING_CLIENT_NAME} {code[:8]}"
        refresh_token = await self.hass.auth.async_create_refresh_token(
            user=user,
            client_id=PAIRING_CLIENT_ID,
            client_name=client_name,
            token_type=auth_models.TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
            access_token_expiration=timedelta(days=lifespan_days),
        )
        expires_at = dt_util.utcnow() + PAIRING_CODE_TTL

        self._sessions[code] = PairingSession(
            code=code,
            refresh_token_id=refresh_token.id,
            home_assistant_url=home_assistant_url,
            local_url=local_url,
            remote_url=remote_url,
            expires_at=expires_at,
            lifespan_days=lifespan_days,
        )

        params: dict[str, str] = {
            "code": code,
            "base_url": home_assistant_url,
        }
        if local_url:
            params["local_url"] = local_url
        if remote_url:
            params["remote_url"] = remote_url
        uri_query = urlencode(params)
        return {
            "pairing_code": code,
            "pairing_uri": f"wristassistant://pair?{uri_query}",
            "expires_at": expires_at.isoformat(),
            "lifespan_days": lifespan_days,
            "home_assistant_url": home_assistant_url,
            "local_url": local_url,
            "remote_url": remote_url,
        }

    async def async_refresh_active_pairing(
        self,
        user: auth_models.User,
        *,
        home_assistant_url: str,
        local_url: str,
        remote_url: str,
        lifespan_days: int = PAIRING_DEFAULT_LIFESPAN_DAYS,
    ) -> dict[str, Any]:
        """Replace active pairing code with a new one."""
        previous_active_code = self._active_code

        payload = await self.async_create_pairing_code(
            user,
            home_assistant_url=home_assistant_url,
            local_url=local_url,
            remote_url=remote_url,
            lifespan_days=lifespan_days,
        )
        self._active_code = payload["pairing_code"]
        self._active_payload = payload

        if previous_active_code and previous_active_code != self._active_code:
            self._revoke_code(previous_active_code)

        self._fire_active_callbacks()
        return payload

    def async_redeem_pairing_code(
        self, code: str, remote_ip: str | None, device_name: str | None = None
    ) -> dict[str, Any] | None:
        """Redeem a one-time pairing code and return OAuth credentials."""
        self._prune_expired()

        session = self._sessions.get(code)
        if session is None:
            return None

        refresh_token = self.hass.auth.async_get_refresh_token(session.refresh_token_id)
        if refresh_token is None:
            self._sessions.pop(code, None)
            return None

        if device_name:
            refresh_token.client_name = f"Wrist Assistant ({device_name})"

        # Avoid proxy/X-Forwarded-For parsing issues; Home Assistant core's own
        # long-lived token command also omits remote_ip here.
        access_token = self.hass.auth.async_create_access_token(refresh_token)

        expires_in: int | None = None
        expiration = refresh_token.access_token_expiration
        if isinstance(expiration, timedelta):
            expires_in = int(expiration.total_seconds())
        elif isinstance(expiration, (int, float)):
            expires_in = int(expiration)
        if not expires_in or expires_in <= 0:
            expires_in = int(max(1, session.lifespan_days) * 86400)

        token_payload = {
            "access_token": access_token,
            "token_type": "Bearer",
            "auth_mode": "manual_token",
            "expires_in": expires_in,
            "home_assistant_url": session.home_assistant_url,
            "local_url": session.local_url,
            "remote_url": session.remote_url,
        }
        self._sessions.pop(code, None)
        return token_payload

    @property
    def tracked_refresh_token_ids(self) -> set[str]:
        """Return refresh token IDs currently tracked by active sessions."""
        return {s.refresh_token_id for s in self._sessions.values()}

    @callback
    def async_shutdown(self) -> None:
        """Revoke all unused pending pairing tokens."""
        for code in list(self._sessions):
            self._revoke_code(code)
        self._sessions.clear()
        self._active_code = None
        self._active_payload = None
        self._fire_active_callbacks()

    @callback
    def _prune_expired(self) -> None:
        """Revoke and remove expired unredeemed pairing sessions."""
        now = dt_util.utcnow()
        expired_codes = [
            code for code, session in self._sessions.items() if session.expires_at <= now
        ]
        for code in expired_codes:
            self._revoke_code(code)

        if self._active_code and self._active_code not in self._sessions:
            self._active_code = None
            self._active_payload = None
            self._fire_active_callbacks()

    @callback
    def _revoke_code(self, code: str) -> None:
        """Revoke and remove a pairing session by code."""
        session = self._sessions.pop(code, None)
        if session is None:
            return
        refresh_token = self.hass.auth.async_get_refresh_token(session.refresh_token_id)
        if refresh_token is not None:
            self.hass.auth.async_remove_refresh_token(refresh_token)
        if self._active_code == code:
            self._active_code = None
            self._active_payload = None

    @callback
    def _fire_active_callbacks(self) -> None:
        """Notify listeners about active pairing changes."""
        for cb in self._active_callbacks:
            cb()

class WatchUpdatesView(HomeAssistantView):
    """Authenticated long-poll endpoint for watch delta updates."""

    url = "/api/watch/updates"
    name = "api:wrist_assistant_updates"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: Request) -> Response:
        """Handle delta update poll request."""
        from .const import DATA_COORDINATOR, DATA_NOTIFICATION_TOKEN_STORE, DOMAIN

        domain_data = self._hass.data.get(DOMAIN, {})
        coordinator = domain_data.get(DATA_COORDINATOR)
        if coordinator is None:
            return self.json_message("Integration not loaded", status_code=503)
        notification_store = domain_data.get(DATA_NOTIFICATION_TOKEN_STORE)

        try:
            payload = await request.json()
        except (ValueError, UnicodeDecodeError):
            return self.json_message("Invalid JSON body", status_code=400)

        if not isinstance(payload, dict):
            return self.json_message("Expected JSON object body", status_code=400)

        watch_id = payload.get("watch_id")
        config_hash = payload.get("config_hash")
        since = payload.get("since")
        entities = payload.get("entities")
        timeout = payload.get("timeout", DEFAULT_TIMEOUT_SECONDS)

        if not isinstance(watch_id, str) or not watch_id:
            return self.json_message("watch_id is required", status_code=400)
        if not isinstance(config_hash, str) or not config_hash:
            return self.json_message("config_hash is required", status_code=400)
        if since is not None and not isinstance(since, str):
            return self.json_message("since must be a string cursor", status_code=400)
        if entities is not None and not isinstance(entities, list):
            return self.json_message("entities must be an array of entity IDs", status_code=400)

        normalized_entities: list[str] | None = None
        if entities is not None:
            normalized_entities = []
            for entity_id in entities:
                if isinstance(entity_id, str) and entity_id:
                    normalized_entities.append(entity_id)

        if not isinstance(timeout, int):
            return self.json_message("timeout must be an integer", status_code=400)
        timeout = max(MIN_TIMEOUT_SECONDS, min(timeout, MAX_TIMEOUT_SECONDS))

        force_delta = payload.get("force_delta", False) is True
        slim = payload.get("slim", False) is True
        include_summary = payload.get("include_summary", False) is True
        raw_threshold = payload.get("battery_threshold", 20)
        battery_threshold = max(5, min(95, int(raw_threshold) if isinstance(raw_threshold, (int, float)) else 20))

        # Optional: per-domain entity filters for info_summary
        # e.g. {"light": ["light.kitchen"], "person": ["person.jesse"], "sensor": ["sensor.temp"]}
        raw_summary_entities = payload.get("summary_entities")
        summary_entities: dict[str, list[str]] | None = None
        if isinstance(raw_summary_entities, dict):
            summary_entities = {}
            for domain, ids in raw_summary_entities.items():
                if isinstance(domain, str) and isinstance(ids, list):
                    summary_entities[domain] = [eid for eid in ids if isinstance(eid, str) and eid]

        # Piggyback device token registration on authenticated poll
        device_token = payload.get("device_token")
        if (
            notification_store is not None
            and isinstance(device_token, str)
            and device_token
        ):
            apns_env = payload.get("apns_environment", "production")
            if apns_env not in ("development", "production"):
                apns_env = "production"
            notification_store.register(
                watch_id, device_token, platform="watchos", environment=apns_env
            )

        status, body = await coordinator.handle_poll(
            watch_id=watch_id,
            since=since,
            config_hash=config_hash,
            entities=normalized_entities,
            timeout=timeout,
            force_delta=force_delta,
            battery_threshold=battery_threshold,
            summary_entities=summary_entities,
            slim=slim,
            include_summary=include_summary or force_delta or summary_entities is not None,
        )

        if status == 204:
            return Response(status=204)
        if body is None:
            return Response(status=status)

        json_bytes = _json.dumps(body, separators=(",", ":")).encode("utf-8")

        # Gzip compress if the client supports it
        accept_encoding = request.headers.get("Accept-Encoding", "")
        if "gzip" in accept_encoding:
            compressed = gzip.compress(json_bytes, compresslevel=6)
            return Response(
                body=compressed,
                status=status,
                content_type="application/json",
                headers={"Content-Encoding": "gzip"},
            )

        return Response(
            body=json_bytes,
            status=status,
            content_type="application/json",
        )


class WatchSummaryView(HomeAssistantView):
    """Authenticated endpoint for on-demand info summary snapshots."""

    url = "/api/wrist_assistant/summary"
    name = "api:wrist_assistant_summary"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: Request) -> Response:
        """Return an info summary without touching delta sessions."""
        from .const import DATA_COORDINATOR, DOMAIN

        coordinator = self._hass.data.get(DOMAIN, {}).get(DATA_COORDINATOR)
        if coordinator is None:
            return self.json_message("Integration not loaded", status_code=503)

        try:
            payload = await request.json()
        except (ValueError, UnicodeDecodeError):
            payload = {}

        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            return self.json_message("Expected JSON object body", status_code=400)

        include_details = payload.get("include_details", True) is True
        raw_threshold = payload.get("battery_threshold", 20)
        battery_threshold = max(
            5,
            min(95, int(raw_threshold) if isinstance(raw_threshold, (int, float)) else 20),
        )

        raw_summary_entities = payload.get("summary_entities")
        summary_entities: dict[str, list[str]] | None = None
        if isinstance(raw_summary_entities, dict):
            summary_entities = {}
            for domain, ids in raw_summary_entities.items():
                if isinstance(domain, str) and isinstance(ids, list):
                    summary_entities[domain] = [
                        eid for eid in ids if isinstance(eid, str) and eid
                    ]

        body = {
            "info_summary": coordinator._compute_info_summary(
                include_details=include_details,
                battery_threshold=battery_threshold,
                summary_entities=summary_entities,
            ),
            "capabilities": sorted(coordinator._capabilities),
        }

        json_bytes = _json.dumps(body, separators=(",", ":")).encode("utf-8")
        accept_encoding = request.headers.get("Accept-Encoding", "")
        if "gzip" in accept_encoding:
            compressed = gzip.compress(json_bytes, compresslevel=6)
            return Response(
                body=compressed,
                status=200,
                content_type="application/json",
                headers={"Content-Encoding": "gzip"},
            )
        return Response(body=json_bytes, status=200, content_type="application/json")


class PairingRedeemView(HomeAssistantView):
    """Unauthenticated endpoint that exchanges pairing code for OAuth credentials."""

    url = "/api/wrist_assistant/pairing/redeem"
    name = "api:wrist_assistant_pairing_redeem"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: Request) -> Response:
        """Redeem one-time pairing code."""
        from .const import DATA_PAIRING_COORDINATOR, DOMAIN

        pairing = self._hass.data.get(DOMAIN, {}).get(DATA_PAIRING_COORDINATOR)
        if pairing is None:
            return self.json_message("Integration not loaded", status_code=503)

        try:
            payload = await request.json()
        except (ValueError, UnicodeDecodeError):
            return self.json_message("Invalid JSON body", status_code=400)

        if not isinstance(payload, dict):
            return self.json_message("Expected JSON object body", status_code=400)

        pairing_code = payload.get("pairing_code")
        if not isinstance(pairing_code, str) or not pairing_code:
            return self.json_message("pairing_code is required", status_code=400)
        device_name = payload.get("device_name")
        if device_name is not None and not isinstance(device_name, str):
            device_name = None
        code_hint = pairing_code[:8]
        _LOGGER.info(
            "Pairing redeem request code=%s device=%s remote=%s",
            code_hint,
            device_name or "(not provided)",
            request.remote,
        )

        try:
            token_payload = pairing.async_redeem_pairing_code(
                pairing_code,
                remote_ip=request.remote,
                device_name=device_name,
            )
        except HomeAssistantError as err:
            _LOGGER.warning("Pairing code redemption rejected code=%s: %s", code_hint, err)
            return self.json_message(str(err), status_code=400)
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "Unexpected error redeeming Wrist Assistant pairing code code=%s",
                code_hint,
            )
            return self.json_message("Internal pairing redemption error", status_code=500)
        if token_payload is None:
            _LOGGER.warning("Pairing code invalid/expired code=%s", code_hint)
            return self.json_message("Invalid or expired pairing code", status_code=400)
        _LOGGER.info("Pairing redeem success code=%s", code_hint)
        return self.json(token_payload, status_code=200)
