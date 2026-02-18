"""Camera entity exposing current Wrist Assistant pairing QR."""

from __future__ import annotations

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import PairingCoordinator
from .const import DATA_PAIRING_COORDINATOR, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Wrist Assistant camera entities."""
    pairing: PairingCoordinator = hass.data[DOMAIN][DATA_PAIRING_COORDINATOR]
    async_add_entities([PairingQRCamera(pairing, entry)])


class PairingQRCamera(Camera):
    """Camera that renders current active pairing code as QR."""

    _attr_has_entity_name = True
    _attr_name = "Pairing QR"
    _attr_should_poll = False

    def __init__(self, pairing: PairingCoordinator, entry: ConfigEntry) -> None:
        super().__init__()
        self._pairing = pairing
        self._entry = entry
        self._attr_unique_id = f"wrist_assistant_{entry.entry_id}_pairing_qr"
        self.content_type = "image/svg+xml"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Wrist Assistant",
            manufacturer="Wrist Assistant",
            model="Delta Coordinator",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self._pairing.async_add_active_listener(self._handle_update)
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes:
        """Return QR image as SVG."""
        return self._pairing.svg_qr_bytes()

    @property
    def extra_state_attributes(self) -> dict:
        payload = self._pairing.active_payload
        if payload is None:
            return {"active_pairing": False}
        return {
            "active_pairing": True,
            "expires_at": payload.get("expires_at"),
        }
