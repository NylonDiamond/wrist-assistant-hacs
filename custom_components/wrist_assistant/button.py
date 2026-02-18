"""Button entities for Wrist Assistant pairing."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.core import HomeAssistant

from .api import PairingCoordinator
from .const import DATA_PAIRING_COORDINATOR, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Wrist Assistant button entities."""
    pairing: PairingCoordinator = hass.data[DOMAIN][DATA_PAIRING_COORDINATOR]
    async_add_entities([RefreshPairingQRButton(pairing, entry)])


class RefreshPairingQRButton(ButtonEntity):
    """Regenerate one-time pairing QR code."""

    _attr_has_entity_name = True
    _attr_name = "Refresh pairing QR"
    _attr_icon = "mdi:qrcode-scan"

    def __init__(self, pairing: PairingCoordinator, entry: ConfigEntry) -> None:
        self._pairing = pairing
        self._entry = entry
        self._attr_unique_id = f"wrist_assistant_{entry.entry_id}_refresh_pairing_qr"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Wrist Assistant",
            manufacturer="Wrist Assistant",
            model="Delta Coordinator",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_press(self) -> None:
        """Refresh the currently active pairing code."""
        await self._pairing.async_refresh_active_pairing_default()
