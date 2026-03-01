"""Camera device grouping — one entry per physical camera with stream role classification."""

from __future__ import annotations

import logging
import re
from typing import Any

from aiohttp.web import Request, Response

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)

# Platform-specific suffix → role mappings (checked in order, first match wins)
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

# Trailing _N pattern for multi-lens cameras (e.g. _fluent_2, _clear_2)
_LENS_SUFFIX_RE = re.compile(r"^(.+)_(\d+)$")


def _classify_entity_role(
    entity_id: str, platform: str | None
) -> tuple[str | None, int]:
    """Classify a camera entity's role within its device group.

    Returns (role, lens_index) where:
    - role: 'sd_stream', 'hd_stream', 'sd_snapshot', 'hd_snapshot', or None
    - lens_index: 0 for primary lens (no suffix), 2+ for additional lenses (_2, _3, etc.)
    """
    obj_id = entity_id.removeprefix("camera.")

    # Try platform-specific rules first
    if platform and platform in _PLATFORM_RULES:
        for suffix, role in _PLATFORM_RULES[platform]:
            if obj_id.endswith(suffix):
                return role, 0

    # Generic fallback
    for suffix, role in _GENERIC_RULES:
        if obj_id.endswith(suffix):
            return role, 0

    # Multi-lens detection: strip trailing _N (N >= 2) and re-classify.
    # Handles Reolink Duo/TrackMix where second lens uses e.g. _fluent_2, _clear_2.
    m = _LENS_SUFFIX_RE.match(obj_id)
    if m:
        lens_idx = int(m.group(2))
        if lens_idx >= 2:
            base_obj = m.group(1)
            if platform and platform in _PLATFORM_RULES:
                for suffix, role in _PLATFORM_RULES[platform]:
                    if base_obj.endswith(suffix):
                        return role, lens_idx
            for suffix, role in _GENERIC_RULES:
                if base_obj.endswith(suffix):
                    return role, lens_idx

    return None, 0


def build_camera_device_groups(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Build grouped camera device list from entity and device registries.

    Multi-lens cameras (e.g. Reolink Duo) are split into separate entries per lens.

    Returns a list of device dicts, each with:
    - device_id: str or None
    - name: str (device name or entity friendly name)
    - manufacturer: str or None
    - model: str or None
    - entities: dict mapping role → entity_id (e.g. {"sd_stream": "camera.x", "hd_stream": "camera.y"})
    - all_entity_ids: list of all camera entity IDs for this device/lens
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

        # Fallback name from first entity's friendly name
        if not device_name:
            first = entries[0]
            device_name = (
                first.name
                or first.original_name
                or first.entity_id.removeprefix("camera.").replace("_", " ").title()
            )

        # Classify each entity's role and lens index
        lens_roles: dict[int, dict[str, str]] = {}   # lens → {role → entity_id}
        lens_entities: dict[int, list[str]] = {}      # lens → [entity_ids]

        for entry in entries:
            role, lens_idx = _classify_entity_role(entry.entity_id, entry.platform)
            lens_roles.setdefault(lens_idx, {})
            lens_entities.setdefault(lens_idx, [])
            lens_entities[lens_idx].append(entry.entity_id)
            if role and role not in lens_roles[lens_idx]:
                lens_roles[lens_idx][role] = entry.entity_id

        is_multi_lens = any(idx > 0 for idx in lens_roles)

        # Create one device entry per lens
        for lens_idx in sorted(lens_roles):
            entity_roles = lens_roles[lens_idx]
            l_entity_ids = lens_entities.get(lens_idx, [])

            # Default: if no roles matched, use first entity as sd_stream
            if not entity_roles and l_entity_ids:
                entity_roles["sd_stream"] = l_entity_ids[0]

            # Append lens label for multi-lens cameras
            lens_name = device_name or ""
            if is_multi_lens and lens_idx > 0:
                lens_name = f"{lens_name} ({lens_idx})"

            devices.append({
                "device_id": device_id,
                "name": lens_name,
                "manufacturer": manufacturer,
                "model": model,
                "entities": entity_roles,
                "all_entity_ids": sorted(l_entity_ids),
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
