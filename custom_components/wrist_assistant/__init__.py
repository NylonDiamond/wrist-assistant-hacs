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
    PairingQRCodeView,
    PairingRedeemView,
    WatchUpdatesView,
)
from .const import (
    DATA_COORDINATOR,
    DATA_PAIRING_COORDINATOR,
    DOMAIN,
    PLATFORMS,
    SERVICE_CREATE_PAIRING_CODE,
    SERVICE_FORCE_RESYNC,
)

_LOGGER = logging.getLogger(__name__)

# Unique ID suffixes from removed entity classes (cleanup on upgrade)
_ORPHANED_SUFFIXES = ("_entity_list",)
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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Wrist Assistant from a config entry."""
    _cleanup_orphaned_entities(hass, entry)

    coordinator = DeltaCoordinator(hass)
    pairing_coordinator = PairingCoordinator(hass)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][DATA_COORDINATOR] = coordinator
    hass.data[DOMAIN][DATA_PAIRING_COORDINATOR] = pairing_coordinator
    hass.http.register_view(WatchUpdatesView(coordinator))
    hass.http.register_view(PairingRedeemView(pairing_coordinator))
    hass.http.register_view(PairingQRCodeView(pairing_coordinator))

    # Revoke orphaned pairing refresh tokens from previous runs that were
    # never redeemed (e.g., HA crashed or was killed before shutdown cleanup).
    await _cleanup_orphaned_pairing_tokens(hass, pairing_coordinator)

    @callback
    def _handle_stop(_event) -> None:
        coordinator.async_shutdown()
        pairing_coordinator.async_shutdown()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _handle_stop)
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    default_user = await _resolve_pairing_user(hass, None)
    local_url = _sanitize_base_url(hass.config.internal_url) or _discover_base_url(
        hass, prefer_external=False
    )
    remote_url = _sanitize_base_url(hass.config.external_url) or _discover_base_url(
        hass, prefer_external=True
    )
    home_assistant_url = remote_url or local_url
    if not home_assistant_url:
        home_assistant_url = _discover_base_url(hass, prefer_external=True)
    pairing_coordinator.async_configure_defaults(
        user_id=default_user.id if default_user else None,
        home_assistant_url=home_assistant_url,
        local_url=local_url,
        remote_url=remote_url,
        lifespan_days=3650,
    )

    @callback
    def _on_active_pairing_changed() -> None:
        active = pairing_coordinator.active_payload
        if active is not None:
            _show_pairing_notification(hass, entry, active)

    unsub_pairing_listener = pairing_coordinator.async_add_active_listener(
        _on_active_pairing_changed
    )
    entry.async_on_unload(unsub_pairing_listener)

    if default_user and home_assistant_url:
        payload = await pairing_coordinator.async_refresh_active_pairing(
            default_user,
            home_assistant_url=home_assistant_url,
            local_url=local_url,
            remote_url=remote_url,
            lifespan_days=3650,
        )
        _show_pairing_notification(hass, entry, payload)

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
        _show_pairing_notification(hass, entry, payload)
        return payload

    hass.services.async_register(
        DOMAIN,
        SERVICE_CREATE_PAIRING_CODE,
        _handle_create_pairing_code,
        schema=_CREATE_PAIRING_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
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
        persistent_notification.async_dismiss(
            hass, _PAIRING_NOTIFICATION_ID_TEMPLATE % entry.entry_id
        )
        hass.services.async_remove(DOMAIN, SERVICE_CREATE_PAIRING_CODE)
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
        "QR pairing will not be available until an owner user exists"
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
    """
    active_token_ids = pairing.tracked_refresh_token_ids
    revoked = 0
    for user in await hass.auth.async_get_users():
        for token in list(user.refresh_tokens.values()):
            if (
                token.client_id == PAIRING_CLIENT_ID
                and token.client_name
                and token.client_name.startswith("Wrist Assistant QR Pairing")
                and token.id not in active_token_ids
            ):
                hass.auth.async_remove_refresh_token(token)
                revoked += 1
    if revoked:
        _LOGGER.info(
            "Revoked %d orphaned Wrist Assistant pairing token(s) from previous runs",
            revoked,
        )


def _show_pairing_notification(
    hass: HomeAssistant, entry: ConfigEntry, payload: dict[str, object]
) -> None:
    """Show immediate post-setup pairing notification with QR."""
    expires_at = payload.get("expires_at", "unknown")
    pairing_code = payload.get("pairing_code", "")
    qr_path = "/api/wrist_assistant/pairing/qr.svg"
    if isinstance(pairing_code, str) and pairing_code:
        qr_path = f"{qr_path}?code={pairing_code}"
    message = (
        "Scan this QR in Wrist Assistant app:\n\n"
        f"![Wrist Assistant Pairing QR]({qr_path})\n\n"
        "App path: **Connect -> Sign in -> Scan QR**\n\n"
        f"Pairing code expires: `{expires_at}`"
    )
    persistent_notification.async_create(
        hass,
        message=message,
        title="Wrist Assistant pairing ready",
        notification_id=_PAIRING_NOTIFICATION_ID_TEMPLATE % entry.entry_id,
    )
