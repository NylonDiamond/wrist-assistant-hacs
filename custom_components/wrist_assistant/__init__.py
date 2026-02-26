"""Wrist Assistant delta API integration."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import network

from .api import (
    PAIRING_CLIENT_ID,
    DeltaCoordinator,
    PairingCoordinator,
    PairingRedeemView,
    WatchUpdatesView,
)
from .apns_client import APNsClient
from .camera_stream import (
    CameraBatchView,
    CameraStreamCoordinator,
    CameraStreamView,
    CameraViewportView,
)
from .const import (
    DATA_APNS_CLIENT,
    DATA_CAMERA_STREAM_COORDINATOR,
    DATA_COORDINATOR,
    DATA_NOTIFICATION_TOKEN_STORE,
    DATA_PAIRING_COORDINATOR,
    DOMAIN,
    PLATFORMS,
    SERVICE_CREATE_PAIRING_CODE,
    SERVICE_FORCE_RESYNC,
    SERVICE_SEND_NOTIFICATION,
)
from .notifications import (
    NotificationRegisterView,
    NotificationTokenStore,
)

_LOGGER = logging.getLogger(__name__)

# Unique ID suffixes from removed entity classes (cleanup on upgrade)
_ORPHANED_SUFFIXES = ("_entity_list", "_refresh_pairing_qr", "_pairing_qr", "_connection_qr")
# Entities to auto-disable on upgrade (disabled-by-default only affects new installs)
_DISABLE_ON_UPGRADE_SUFFIXES = (
    "_events_processed",
    "_buffer_usage",
    "_events_per_minute",
    "_pairing_expires_at",
    "_poll_interval",
    "_connected_since",
)
_PAIRING_NOTIFICATION_ID_TEMPLATE = "wrist_assistant_pairing_%s"
_CREATE_PAIRING_SCHEMA = vol.Schema(
    {
        vol.Optional("local_url"): cv.string,
        vol.Optional("remote_url"): cv.string,
        vol.Optional("lifespan_days", default=3650): vol.All(
            vol.Coerce(int),
            vol.Range(min=1, max=36500),
        ),
    }
)
_SEND_NOTIFICATION_SCHEMA = vol.Schema(
    {
        vol.Required("message"): cv.string,
        vol.Optional("title"): cv.string,
        vol.Optional("target"): cv.string,
        vol.Optional("category"): cv.string,
        vol.Optional("data"): dict,
        vol.Optional("sound"): cv.string,
        vol.Optional("push_type", default="alert"): vol.In(["alert", "background"]),
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Wrist Assistant from a config entry."""
    _cleanup_orphaned_entities(hass, entry)
    _disable_noisy_entities(hass, entry)

    coordinator = DeltaCoordinator(hass)
    pairing_coordinator = PairingCoordinator(hass)
    camera_stream_coordinator = CameraStreamCoordinator()
    notification_store = NotificationTokenStore(hass)
    await notification_store.async_load()

    # Register server capabilities
    coordinator.register_capability("gzip")
    coordinator.register_capability("slim_payloads")
    coordinator.register_capability("camera_batch")
    coordinator.register_capability("push_notifications")

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][DATA_COORDINATOR] = coordinator
    hass.data[DOMAIN][DATA_PAIRING_COORDINATOR] = pairing_coordinator
    hass.data[DOMAIN][DATA_CAMERA_STREAM_COORDINATOR] = camera_stream_coordinator
    hass.data[DOMAIN][DATA_NOTIFICATION_TOKEN_STORE] = notification_store
    hass.http.register_view(WatchUpdatesView(hass))
    hass.http.register_view(PairingRedeemView(hass))
    hass.http.register_view(CameraStreamView(hass))
    hass.http.register_view(CameraViewportView(hass))
    hass.http.register_view(CameraBatchView(hass))
    hass.http.register_view(NotificationRegisterView(hass))

    # APNs client – read key in executor to avoid blocking the event loop.
    apns_client = await _create_apns_client(hass)
    if apns_client:
        hass.data[DOMAIN][DATA_APNS_CLIENT] = apns_client
        _LOGGER.info("APNs client ready")

    # Revoke orphaned pairing refresh tokens from previous runs that were
    # never redeemed (e.g., HA crashed or was killed before shutdown cleanup).
    await _cleanup_orphaned_pairing_tokens(hass, pairing_coordinator)

    @callback
    def _handle_stop(_event) -> None:
        coordinator.async_shutdown()
        pairing_coordinator.async_shutdown()
        camera_stream_coordinator.shutdown()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _handle_stop)
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if not entry.data.get("initial_setup_done"):
        _show_pairing_notification(hass, entry, pairing_coordinator)
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "initial_setup_done": True}
        )

    async def _handle_force_resync(call: ServiceCall) -> None:
        coordinator.async_force_resync()

    hass.services.async_register(DOMAIN, SERVICE_FORCE_RESYNC, _handle_force_resync)

    async def _handle_create_pairing_code(call: ServiceCall) -> ServiceResponse:
        user = await _resolve_pairing_user(hass, call.context.user_id)
        if user is None:
            raise HomeAssistantError("Unable to resolve an active Home Assistant user for pairing.")

        requested_local_url = _sanitize_base_url(call.data.get("local_url"))
        local_url = requested_local_url or _sanitize_base_url(
            hass.config.internal_url
        ) or _discover_base_url(hass, prefer_external=False)
        requested_remote_url = _sanitize_base_url(call.data.get("remote_url"))
        remote_url = requested_remote_url or _sanitize_base_url(
            hass.config.external_url
        ) or _discover_base_url(hass, prefer_external=True)
        lifespan_days = int(call.data.get("lifespan_days", 3650))
        home_assistant_url = remote_url or local_url
        if not home_assistant_url:
            home_assistant_url = _discover_base_url(hass, prefer_external=True)
        if not home_assistant_url:
            raise HomeAssistantError(
                "Set local_url/remote_url in the service call or configure internal/external URL in Home Assistant."
            )

        payload = await pairing_coordinator.async_refresh_active_pairing(
            user,
            home_assistant_url=home_assistant_url,
            local_url=local_url,
            remote_url=remote_url,
            lifespan_days=lifespan_days,
        )
        return payload

    hass.services.async_register(
        DOMAIN,
        SERVICE_CREATE_PAIRING_CODE,
        _handle_create_pairing_code,
        schema=_CREATE_PAIRING_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def _handle_send_notification(call: ServiceCall) -> None:
        client = hass.data[DOMAIN].get(DATA_APNS_CLIENT)
        if client is None:
            raise HomeAssistantError(
                "APNs client failed to initialize. Check Home Assistant logs for details."
            )

        store = hass.data[DOMAIN][DATA_NOTIFICATION_TOKEN_STORE]
        target = call.data.get("target")
        message = call.data["message"]
        title = call.data.get("title")
        category = call.data.get("category")
        extra_data = call.data.get("data")
        sound = call.data.get("sound")
        push_type = call.data.get("push_type", "alert")

        # Resolve targets (need full entries for environment)
        if target:
            entry = store.get_entry(target)
            if entry is None:
                raise HomeAssistantError(f"No registered push token for watch '{target}'")
            targets = {target: entry}
        else:
            all_tokens = store.all_tokens
            if not all_tokens:
                raise HomeAssistantError("No watches have registered for push notifications")
            targets = all_tokens

        # Send to each target
        failures = []
        for watch_id, token_entry in targets.items():
            success, reason, used_env = await client.send_push(
                device_token=token_entry.device_token,
                title=title,
                body=message,
                category=category,
                data=extra_data,
                sound=sound,
                push_type=push_type,
                environment=token_entry.environment,
            )
            if success:
                if used_env != token_entry.environment:
                    store.register(
                        watch_id,
                        token_entry.device_token,
                        platform=token_entry.platform,
                        environment=used_env,
                    )
            else:
                if APNsClient.is_dead_token(reason):
                    _LOGGER.warning(
                        "Removing dead token for watch_id=%s (reason=%s)",
                        watch_id,
                        reason,
                    )
                    store.remove(watch_id)
                failures.append((watch_id, reason))

        if failures and len(failures) == len(targets):
            reasons = ", ".join(f"{wid}: {r}" for wid, r in failures)
            raise HomeAssistantError(f"All push notifications failed: {reasons}")

        if failures:
            reasons = ", ".join(f"{wid}: {r}" for wid, r in failures)
            _LOGGER.warning("Some push notifications failed: %s", reasons)

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_NOTIFICATION,
        _handle_send_notification,
        schema=_SEND_NOTIFICATION_SCHEMA,
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data = hass.data.get(DOMAIN)
        if data and DATA_COORDINATOR in data:
            data[DATA_COORDINATOR].async_shutdown()
            data.pop(DATA_COORDINATOR, None)
        if data and DATA_PAIRING_COORDINATOR in data:
            data[DATA_PAIRING_COORDINATOR].async_shutdown()
            data.pop(DATA_PAIRING_COORDINATOR, None)
        if data and DATA_CAMERA_STREAM_COORDINATOR in data:
            data[DATA_CAMERA_STREAM_COORDINATOR].shutdown()
            data.pop(DATA_CAMERA_STREAM_COORDINATOR, None)
        data.pop(DATA_NOTIFICATION_TOKEN_STORE, None)
        data.pop(DATA_APNS_CLIENT, None)
        persistent_notification.async_dismiss(
            hass, _PAIRING_NOTIFICATION_ID_TEMPLATE % entry.entry_id
        )
        hass.services.async_remove(DOMAIN, SERVICE_CREATE_PAIRING_CODE)
        hass.services.async_remove(DOMAIN, SERVICE_FORCE_RESYNC)
        hass.services.async_remove(DOMAIN, SERVICE_SEND_NOTIFICATION)
    return unload_ok


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Allow removal of a device from the UI."""
    return True


async def _create_apns_client(hass: HomeAssistant) -> APNsClient | None:
    """Create APNs client, reading the bundled key off the event loop."""
    import ssl

    from .apns_client import _BUNDLED_KEY_PATH  # noqa: WPS433

    def _blocking_init() -> tuple[str, ssl.SSLContext]:
        key_content = _BUNDLED_KEY_PATH.read_text()
        ctx = ssl.create_default_context()
        return key_content, ctx

    try:
        key_content, ssl_context = await hass.async_add_executor_job(_blocking_init)
        return APNsClient(key_content, ssl_context=ssl_context)
    except Exception:
        _LOGGER.exception("Failed to create APNs client")
        return None


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


def _disable_noisy_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """One-time disable of noisy diagnostic entities on upgrade."""
    ent_reg = er.async_get(hass)
    disabled = []
    for entity_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if entity_entry.disabled:
            continue
        if any(entity_entry.unique_id.endswith(s) for s in _DISABLE_ON_UPGRADE_SUFFIXES):
            ent_reg.async_update_entity(
                entity_entry.entity_id, disabled_by=er.RegistryEntryDisabler.INTEGRATION
            )
            disabled.append(entity_entry.entity_id)
    if disabled:
        _LOGGER.info("Disabled %d noisy entities on upgrade: %s", len(disabled), disabled)


def _sanitize_base_url(value: str | None) -> str:
    """Normalize Home Assistant base URLs."""
    if value is None:
        return ""
    trimmed = value.strip()
    if not trimmed:
        return ""

    if "://" not in trimmed:
        # Default to http:// for local-looking hostnames to avoid silently
        # upgrading plain-HTTP HA instances to unreachable HTTPS URLs.
        trimmed = f"http://{trimmed}"

    try:
        parsed = cv.url(trimmed)
    except vol.Invalid:
        return ""
    if not parsed.startswith(("http://", "https://")):
        return ""

    return parsed.rstrip("/")


def _discover_base_url(hass: HomeAssistant, *, prefer_external: bool) -> str:
    """Best-effort discover a reachable Home Assistant base URL."""
    try:
        discovered = network.get_url(
            hass,
            prefer_external=prefer_external,
            allow_ip=True,
            require_ssl=False,
        )
    except HomeAssistantError:
        return ""
    return _sanitize_base_url(discovered)


async def _resolve_pairing_user(hass: HomeAssistant, user_id: str | None):
    """Resolve user for pairing token creation."""
    if user_id:
        user = await hass.auth.async_get_user(user_id)
        if user is not None and user.is_active:
            return user

    for user in await hass.auth.async_get_users():
        if user.is_owner and user.is_active:
            return user
    _LOGGER.warning(
        "No active owner user found for Wrist Assistant pairing; "
        "pairing will not be available until an owner user exists"
    )
    return None


async def _cleanup_orphaned_pairing_tokens(
    hass: HomeAssistant, pairing: PairingCoordinator
) -> None:
    """Revoke leftover pairing refresh tokens from previous runs.

    When HA crashes or is killed, shutdown cleanup never runs, leaving
    orphaned long-lived tokens in the auth system. Identify them by
    client_id and client_name prefix, then revoke any that are not
    tracked by the current PairingCoordinator.

    Only revoke tokens that were never redeemed (last_used_at is None).
    Redeemed tokens have been issued to a watch app and must be kept.
    """
    active_token_ids = pairing.tracked_refresh_token_ids
    revoked = 0
    for user in await hass.auth.async_get_users():
        for token in list(user.refresh_tokens.values()):
            if (
                token.client_id == PAIRING_CLIENT_ID
                and token.client_name
                and (
                    token.client_name.startswith("Wrist Assistant QR Pairing")
                    or token.client_name.startswith("Wrist Assistant Pairing")
                )
                and token.id not in active_token_ids
                and token.last_used_at is None
            ):
                hass.auth.async_remove_refresh_token(token)
                revoked += 1
    if revoked:
        _LOGGER.info(
            "Revoked %d orphaned Wrist Assistant pairing token(s) from previous runs",
            revoked,
        )


def _show_pairing_notification(
    hass: HomeAssistant, entry: ConfigEntry, pairing: PairingCoordinator
) -> None:
    """Show persistent pairing notification."""
    message = (
        "### Long-Lived Access Token (recommended)\n\n"
        "Call the `wrist_assistant.create_pairing_code` service to generate "
        "a pairing code, then enter the values in the Wrist Assistant app.\n\n"
        "### OAuth\n\n"
        "Choose **OAuth** in the app — no code needed. "
        "The app will open your Home Assistant login page directly."
    )
    persistent_notification.async_create(
        hass,
        message=message,
        title="Wrist Assistant pairing ready",
        notification_id=_PAIRING_NOTIFICATION_ID_TEMPLATE % entry.entry_id,
    )
