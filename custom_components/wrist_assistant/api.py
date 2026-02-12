"""HTTP API for Wrist Assistant long-poll delta updates."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from aiohttp.web import Request, Response

from homeassistant.components.http import HomeAssistantView
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.util import dt as dt_util

DEFAULT_TIMEOUT_SECONDS = 45
MIN_TIMEOUT_SECONDS = 5
MAX_TIMEOUT_SECONDS = 55
MAX_EVENTS_BUFFER = 5000
MAX_EVENTS_PER_RESPONSE = 250
SESSION_TTL = timedelta(minutes=5)


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
        self._unsub_state_changed = hass.bus.async_listen(
            EVENT_STATE_CHANGED, self._handle_state_changed
        )

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
            )

        if self._is_stale_cursor(since_cursor):
            return 410, self._response_payload(
                events=[],
                next_cursor=str(self._cursor),
                need_entities=False,
                resync_required=True,
            )

        events, next_cursor = self._collect_events(
            since_cursor=since_cursor,
            entities=session.entities,
            limit=MAX_EVENTS_PER_RESPONSE,
        )
        if events:
            return 200, self._response_payload(
                events=events,
                next_cursor=str(next_cursor),
                need_entities=False,
                resync_required=False,
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
                    )
                    if events:
                        return 200, self._response_payload(
                            events=events,
                            next_cursor=str(next_cursor),
                            need_entities=False,
                            resync_required=False,
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
                )
                if events:
                    return 200, self._response_payload(
                        events=events,
                        next_cursor=str(next_cursor),
                        need_entities=False,
                        resync_required=False,
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

    def _collect_events(
        self, since_cursor: int, entities: set[str], limit: int
    ) -> tuple[list[dict[str, Any]], int]:
        """Collect filtered events after the provided cursor."""
        matched: list[dict[str, Any]] = []
        last_sent_cursor = since_cursor
        for event in self._events:
            if event.cursor <= since_cursor:
                continue
            if event.entity_id not in entities:
                continue

            matched.append(event.payload)
            last_sent_cursor = event.cursor
            if len(matched) >= limit:
                break

        if matched:
            return matched, last_sent_cursor
        return [], since_cursor

    def _is_stale_cursor(self, since_cursor: int) -> bool:
        """Return True if requested cursor is older than retained history."""
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

    @staticmethod
    def _response_payload(
        events: list[dict[str, Any]],
        next_cursor: str,
        need_entities: bool,
        resync_required: bool,
    ) -> dict[str, Any]:
        return {
            "events": events,
            "next_cursor": next_cursor,
            "need_entities": need_entities,
            "resync_required": resync_required,
        }

    def _state_to_payload(self, state: State) -> dict[str, Any]:
        """Return HA state payload shape expected by the watch client."""
        return {
            "entity_id": state.entity_id,
            "state": state.state,
            "attributes": self._json_safe(state.attributes),
            "last_updated": state.last_updated.isoformat(),
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


class WatchUpdatesView(HomeAssistantView):
    """Authenticated long-poll endpoint for watch delta updates."""

    url = "/api/watch/updates"
    name = "api:wrist_assistant_updates"
    requires_auth = True

    def __init__(self, coordinator: DeltaCoordinator) -> None:
        self._coordinator = coordinator

    async def post(self, request: Request) -> Response:
        """Handle delta update poll request."""
        try:
            payload = await request.json()
        except ValueError:
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

        status, body = await self._coordinator.handle_poll(
            watch_id=watch_id,
            since=since,
            config_hash=config_hash,
            entities=normalized_entities,
            timeout=timeout,
        )

        if status == 204:
            return Response(status=204)
        if body is None:
            return Response(status=status)
        return self.json(body, status_code=status)
