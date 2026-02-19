"""HTTP API for Wrist Assistant long-poll delta updates."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import io
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
PAIRING_CLIENT_NAME = "Wrist Assistant QR Pairing"
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
    """Single-use QR pairing payload."""

    code: str
    refresh_token_id: str
    home_assistant_url: str
    local_url: str
    remote_url: str
    expires_at: datetime
    lifespan_days: int


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

        # When since is nil, the client is requesting a full state snapshot.
        # Fetch current state directly from HA's state machine (in-memory, instant).
        if since is None or since == "":
            snapshot_events = self._snapshot_current_state(session.entities)
            return 200, self._response_payload(
                events=snapshot_events,
                next_cursor=str(self._cursor),
                need_entities=False,
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

    def _snapshot_current_state(
        self, entities: set[str]
    ) -> list[dict[str, Any]]:
        """Build a full state snapshot from HA's state machine for the given entities."""
        snapshot: list[dict[str, Any]] = []
        for entity_id in entities:
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            snapshot.append(
                {
                    "entity_id": state.entity_id,
                    "state": state.state,
                    "new_state": self._state_to_payload(state),
                    "context_id": (
                        state.context.id if state.context is not None else None
                    ),
                    "last_updated": state.last_updated.isoformat(),
                }
            )
        return snapshot

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


class PairingCoordinator:
    """Issues and redeems short-lived one-time pairing codes."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._sessions: dict[str, PairingSession] = {}
        self._active_code: str | None = None
        self._active_payload: dict[str, Any] | None = None
        self._active_callbacks: list[callback] = []
        self._default_user_id: str | None = None
        self._default_local_url = ""
        self._default_remote_url = ""
        self._default_home_assistant_url = ""
        self._default_lifespan_days = PAIRING_DEFAULT_LIFESPAN_DAYS

    @callback
    def async_add_active_listener(self, cb: callback) -> callback:
        """Register callback for active pairing updates."""
        self._active_callbacks.append(cb)

        @callback
        def _unsub() -> None:
            self._active_callbacks.remove(cb)

        return _unsub

    @callback
    def async_configure_defaults(
        self,
        *,
        user_id: str | None,
        home_assistant_url: str,
        local_url: str,
        remote_url: str,
        lifespan_days: int,
    ) -> None:
        """Set default pairing config used by refresh operations."""
        self._default_user_id = user_id
        self._default_home_assistant_url = home_assistant_url
        self._default_local_url = local_url
        self._default_remote_url = remote_url
        self._default_lifespan_days = lifespan_days

    @property
    def active_payload(self) -> dict[str, Any] | None:
        """Return currently active pairing payload."""
        if self._active_code is None:
            return None
        if self._active_code not in self._sessions:
            return None
        return self._active_payload

    @callback
    def async_is_active_code(self, code: str | None) -> bool:
        """Return whether the provided code is the current active pairing code."""
        if not code:
            return False
        self._prune_expired()
        return code == self._active_code and code in self._sessions

    async def async_create_pairing_code(
        self,
        user: auth_models.User,
        *,
        home_assistant_url: str,
        local_url: str,
        remote_url: str,
        lifespan_days: int = PAIRING_DEFAULT_LIFESPAN_DAYS,
    ) -> dict[str, Any]:
        """Create a one-time code and return a QR payload URI."""
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

        qr_params: dict[str, str] = {
            "code": code,
            "base_url": home_assistant_url,
        }
        if local_url:
            qr_params["local_url"] = local_url
        if remote_url:
            qr_params["remote_url"] = remote_url
        uri_query = urlencode(qr_params)
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

    async def async_refresh_active_pairing_default(self) -> dict[str, Any] | None:
        """Refresh active pairing using configured defaults."""
        user_id = self._default_user_id
        if user_id is None:
            return None
        user = await self.hass.auth.async_get_user(user_id)
        if user is None or not user.is_active:
            return None
        if not self._default_home_assistant_url:
            return None
        return await self.async_refresh_active_pairing(
            user,
            home_assistant_url=self._default_home_assistant_url,
            local_url=self._default_local_url,
            remote_url=self._default_remote_url,
            lifespan_days=self._default_lifespan_days,
        )

    def async_redeem_pairing_code(
        self, code: str, remote_ip: str | None
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
    def async_code_was_active(self, code: str) -> bool:
        """Return whether redeemed code was the active QR code."""
        return code == self._active_code

    @callback
    def async_clear_active_pairing(self) -> None:
        """Clear active pairing state and notify listeners."""
        self._active_code = None
        self._active_payload = None
        self._fire_active_callbacks()
        
    def svg_qr_bytes(self, payload: dict[str, Any] | None = None) -> bytes:
        """Render the active pairing URI as an SVG QR image."""
        active = payload or self.active_payload
        if active is None:
            return self._empty_qr_svg("No active pairing code")
        pairing_uri = active.get("pairing_uri", "")
        if not pairing_uri:
            return self._empty_qr_svg("Missing pairing URI")

        import segno  # noqa: PLC0415

        qr = segno.make(pairing_uri, micro=False, error="M")
        output = io.BytesIO()
        qr.save(output, kind="svg", scale=8, border=2)
        return output.getvalue()

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

    @staticmethod
    def _empty_qr_svg(message: str) -> bytes:
        return (
            "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 256 256'>"
            "<rect width='256' height='256' fill='#ffffff'/>"
            "<text x='128' y='128' text-anchor='middle' dominant-baseline='middle' "
            "font-family='sans-serif' font-size='14' fill='#222222'>"
            f"{message}"
            "</text></svg>"
        ).encode("utf-8")


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


class PairingRedeemView(HomeAssistantView):
    """Unauthenticated endpoint that exchanges pairing code for OAuth credentials."""

    url = "/api/wrist_assistant/pairing/redeem"
    name = "api:wrist_assistant_pairing_redeem"
    requires_auth = False

    def __init__(self, pairing: PairingCoordinator) -> None:
        self._pairing = pairing

    async def post(self, request: Request) -> Response:
        """Redeem one-time pairing code."""
        try:
            payload = await request.json()
        except (ValueError, UnicodeDecodeError):
            return self.json_message("Invalid JSON body", status_code=400)

        if not isinstance(payload, dict):
            return self.json_message("Expected JSON object body", status_code=400)

        pairing_code = payload.get("pairing_code")
        if not isinstance(pairing_code, str) or not pairing_code:
            return self.json_message("pairing_code is required", status_code=400)
        code_hint = pairing_code[:8]
        _LOGGER.info(
            "Pairing redeem request code=%s remote=%s",
            code_hint,
            request.remote,
        )

        try:
            token_payload = self._pairing.async_redeem_pairing_code(
                pairing_code,
                remote_ip=request.remote,
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

        if self._pairing.async_code_was_active(pairing_code):
            async def _refresh_active() -> None:
                try:
                    await self._pairing.async_refresh_active_pairing_default()
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Failed to refresh active pairing after redeem code=%s", code_hint)

            self._pairing.hass.async_create_task(_refresh_active())

        return self.json(token_payload, status_code=200)


class PairingQRCodeView(HomeAssistantView):
    """Endpoint returning current pairing QR SVG for a valid active code."""

    url = "/api/wrist_assistant/pairing/qr.svg"
    name = "api:wrist_assistant_pairing_qr"
    requires_auth = False

    def __init__(self, pairing: PairingCoordinator) -> None:
        self._pairing = pairing

    async def get(self, request: Request) -> Response:
        """Return current pairing QR image."""
        # Persistent notifications render markdown images via plain <img> fetches,
        # which do not include Home Assistant auth headers. Accept only a valid
        # active one-time pairing code so this endpoint remains scoped and short-lived.
        if not self._pairing.async_is_active_code(request.query.get("code")):
            return Response(status=404)
        svg = self._pairing.svg_qr_bytes()
        return Response(
            body=svg,
            content_type="image/svg+xml",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )
