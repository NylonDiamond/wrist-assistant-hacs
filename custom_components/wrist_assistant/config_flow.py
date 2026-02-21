"""Config flow for Wrist Assistant."""

from __future__ import annotations

import logging
import secrets

from aiohttp.web import Request, Response

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigFlow

from .api import PairingCoordinator
from .const import DOMAIN, SETUP_QR_DATA_KEY

_LOGGER = logging.getLogger(__name__)

_SETUP_QR_VIEW_KEY = "setup_qr_view_registered"


class WristAssistantConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Wrist Assistant."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            return await self.async_step_pairing()

        return self.async_show_form(step_id="user")

    async def async_step_pairing(self, user_input=None):
        """Show pairing QR and create config entry on submit."""
        if user_input is not None:
            return self.async_create_entry(
                title="Wrist Assistant",
                data={"initial_setup_done": True},
            )

        qr_url = await self._async_generate_setup_qr()
        if qr_url:
            return self.async_show_form(
                step_id="pairing",
                description_placeholders={"qr_url": qr_url},
            )

        # QR generation failed — create entry without pairing step
        return self.async_create_entry(title="Wrist Assistant", data={})

    async def _async_generate_setup_qr(self) -> str | None:
        """Generate a one-time pairing QR and return the SVG URL."""
        try:
            from . import (  # noqa: PLC0415
                _discover_base_url,
                _resolve_pairing_user,
                _sanitize_base_url,
            )

            hass = self.hass

            user = await _resolve_pairing_user(
                hass, self.context.get("user_id")
            )
            if user is None:
                return None

            local_url = _sanitize_base_url(
                hass.config.internal_url
            ) or _discover_base_url(hass, prefer_external=False)
            remote_url = _sanitize_base_url(
                hass.config.external_url
            ) or _discover_base_url(hass, prefer_external=True)
            home_assistant_url = remote_url or local_url
            if not home_assistant_url:
                home_assistant_url = _discover_base_url(
                    hass, prefer_external=True
                )
            if not home_assistant_url:
                return None

            coordinator = PairingCoordinator(hass)
            payload = await coordinator.async_create_pairing_code(
                user,
                home_assistant_url=home_assistant_url,
                local_url=local_url,
                remote_url=remote_url,
            )

            secret = secrets.token_urlsafe(32)
            hass.data.setdefault(DOMAIN, {})[SETUP_QR_DATA_KEY] = {
                "coordinator": coordinator,
                "secret": secret,
                "payload": payload,
            }

            # Register the view once (idempotent)
            domain_data = hass.data[DOMAIN]
            if not domain_data.get(_SETUP_QR_VIEW_KEY):
                hass.http.register_view(SetupQRCodeView())
                domain_data[_SETUP_QR_VIEW_KEY] = True

            return f"/api/wrist_assistant/setup_qr.svg?secret={secret}"
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to generate setup QR code")
            return None


class SetupQRCodeView(HomeAssistantView):
    """Unauthenticated endpoint serving the setup QR SVG."""

    url = "/api/wrist_assistant/setup_qr.svg"
    name = "api:wrist_assistant_setup_qr"
    requires_auth = False

    async def get(self, request: Request) -> Response:
        """Return setup QR SVG image."""
        hass = request.app["hass"]
        setup_data = hass.data.get(DOMAIN, {}).get(SETUP_QR_DATA_KEY)
        if not setup_data:
            return Response(status=404)

        request_secret = request.query.get("secret", "")
        if not request_secret or not secrets.compare_digest(
            request_secret, setup_data["secret"]
        ):
            return Response(status=404)

        coordinator = setup_data["coordinator"]
        svg = coordinator.svg_qr_bytes(setup_data["payload"])
        return Response(
            body=svg,
            content_type="image/svg+xml",
            headers={"Cache-Control": "no-store"},
        )
