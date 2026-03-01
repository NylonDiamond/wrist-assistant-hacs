"""Camera device grouping — one entry per physical camera with stream role classification."""

from __future__ import annotations

import logging
from typing import Any

from aiohttp.web import Request, Response

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)

# Platform-specific suffix → role mappings (checked longest-first via sorted key length)
_PLATFORM_RULES: dict[str, list[tuple[str, str]]] = {
    "reolink": [
        # Dual-lens telephoto snapshots
        ("_autotrack_snapshots_sub_lens_0", "sd_snapshot"),
        ("_autotrack_snapshots_sub_lens_1", "sd_snapshot"),
        ("_autotrack_snapshots_main_lens_0", "hd_snapshot"),
        ("_autotrack_snapshots_main_lens_1", "hd_snapshot"),
        # Dual-lens snapshots
        ("_snapshots_sub_lens_0", "sd_snapshot"),
        ("_snapshots_sub_lens_1", "sd_snapshot"),
        ("_snapshots_main_lens_0", "hd_snapshot"),
        ("_snapshots_main_lens_1", "hd_snapshot"),
        # Telephoto snapshots & streams
        ("_autotrack_snapshots_sub", "sd_snapshot"),
        ("_autotrack_snapshots_main", "hd_snapshot"),
        ("_autotrack_snapshots_fluent", "sd_snapshot"),
        ("_autotrack_snapshots_clear", "hd_snapshot"),
        ("_autotrack_sub", "sd_stream"),
        ("_autotrack_main", "hd_stream"),
        ("_autotrack_fluent", "sd_stream"),
        ("_autotrack_clear", "hd_stream"),
        # Dual-lens streams
        ("_sub_lens_0", "sd_stream"),
        ("_sub_lens_1", "sd_stream"),
        ("_main_lens_0", "hd_stream"),
        ("_main_lens_1", "hd_stream"),
        ("_ext_lens_0", "sd_stream"),
        ("_ext_lens_1", "sd_stream"),
        # Snapshots (current + legacy)
        ("_snapshots_sub", "sd_snapshot"),
        ("_snapshots_main", "hd_snapshot"),
        ("_snapshots_fluent", "sd_snapshot"),
        ("_snapshots_clear", "hd_snapshot"),
        # Balanced → sd
        ("_ext", "sd_stream"),
        # Legacy streams
        ("_fluent", "sd_stream"),
        ("_clear", "hd_stream"),
        # Current naming
        ("_sub", "sd_stream"),
        ("_main", "hd_stream"),
    ],
    "unifiprotect": [
        ("_low_resolution_channel", "sd_stream"),
        ("_medium_resolution_channel", "sd_stream"),
        ("_high_resolution_channel", "hd_stream"),
    ],
    "tapo": [
        ("_sd", "sd_stream"),
        ("_hd", "hd_stream"),
    ],
}

# Generic fallback suffixes (platform-agnostic)
_GENERIC_RULES: list[tuple[str, str]] = [
    ("_sub", "sd_stream"),
    ("_main", "hd_stream"),
    ("_fluent", "sd_stream"),
    ("_clear", "hd_stream"),
    ("_low", "sd_stream"),
    ("_high", "hd_stream"),
    ("_sd", "sd_stream"),
    ("_hd", "hd_stream"),
    ("_ext", "sd_stream"),
    ("_low_resolution_channel", "sd_stream"),
    ("_medium_resolution_channel", "sd_stream"),
    ("_high_resolution_channel", "hd_stream"),
]


def _classify_entity_role(entity_id: str, platform: str | None) -> str | None:
    """Classify a camera entity's role within its device group.

    Returns a role string like 'sd_stream', 'hd_stream', 'sd_snapshot', 'hd_snapshot',
    or None if no suffix matches (single-entity device or unrecognized).
    """
    obj_id = entity_id.removeprefix("camera.")

    # Try platform-specific rules first
    if platform and platform in _PLATFORM_RULES:
        for suffix, role in _PLATFORM_RULES[platform]:
            if obj_id.endswith(suffix):
                return role

    # Generic fallback
    for suffix, role in _GENERIC_RULES:
        if obj_id.endswith(suffix):
            return role

    return None


def build_camera_device_groups(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Build grouped camera device list from entity and device registries.

    Returns a list of device dicts, each with:
    - device_id: str or None
    - name: str (device name or entity friendly name)
    - manufacturer: str or None
    - model: str or None
    - entities: dict mapping role → entity_id (e.g. {"sd_stream": "camera.x", "hd_stream": "camera.y"})
    - all_entity_ids: list of all camera entity IDs for this device
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    # Collect all non-disabled camera entities
    camera_entries: list[er.RegistryEntry] = []
    for entry in ent_reg.entities.values():
        if (
            entry.domain == "camera"
            and not entry.disabled_by
        ):
            camera_entries.append(entry)

    # Group by device_id
    device_groups: dict[str | None, list[er.RegistryEntry]] = {}
    for entry in camera_entries:
        device_groups.setdefault(entry.device_id, []).append(entry)

    devices: list[dict[str, Any]] = []

    for device_id, entries in device_groups.items():
        # Get device info
        device_name = None
        manufacturer = None
        model = None
        if device_id:
            device = dev_reg.async_get(device_id)
            if device:
                device_name = device.name_by_user or device.name
                manufacturer = device.manufacturer
                model = device.model

        # Classify each entity's role
        entity_roles: dict[str, str] = {}  # role → entity_id
        all_entity_ids: list[str] = []

        for entry in entries:
            all_entity_ids.append(entry.entity_id)
            role = _classify_entity_role(entry.entity_id, entry.platform)
            if role:
                # First match wins for each role (avoid duplicates)
                if role not in entity_roles:
                    entity_roles[role] = entry.entity_id

        # Single-entity devices: entity gets sd_stream role
        if len(entries) == 1 and not entity_roles:
            entity_roles["sd_stream"] = entries[0].entity_id

        # Multi-entity with no matches: use first as sd_stream
        if not entity_roles and entries:
            entity_roles["sd_stream"] = entries[0].entity_id

        # Fallback name from first entity's friendly name
        if not device_name:
            first = entries[0]
            device_name = (
                first.name
                or first.original_name
                or first.entity_id.removeprefix("camera.").replace("_", " ").title()
            )

        devices.append({
            "device_id": device_id,
            "name": device_name,
            "manufacturer": manufacturer,
            "model": model,
            "entities": entity_roles,
            "all_entity_ids": sorted(all_entity_ids),
        })

    # Sort by name
    devices.sort(key=lambda d: (d["name"] or "").lower())
    return devices


class CameraDevicesView(HomeAssistantView):
    """GET endpoint returning camera devices grouped by physical device."""

    url = "/api/wrist_assistant/camera/devices"
    name = "api:wrist_assistant_camera_devices"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: Request) -> Response:
        devices = build_camera_device_groups(self._hass)
        return self.json({"devices": devices})
