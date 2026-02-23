"""Smart camera streaming with server-side crop, resize, and quality control."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from io import BytesIO
import logging
from aiohttp.web import Request, Response, StreamResponse
from PIL import Image

from homeassistant.components.camera import Image as CameraImage, async_get_image
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(__name__)

# Limits
MIN_WIDTH = 50
MAX_WIDTH = 2000
MIN_QUALITY = 10
MAX_QUALITY = 95
MIN_FPS = 0.5
MAX_FPS = 10.0
DEFAULT_WIDTH = 400
DEFAULT_QUALITY = 75
DEFAULT_FPS = 2.0


@dataclass(slots=True)
class ViewportState:
    """Normalized crop region (0.0-1.0)."""

    x: float = 0.0
    y: float = 0.0
    w: float = 1.0
    h: float = 1.0


@dataclass(slots=True)
class StreamSession:
    """Active stream session keyed by (watch_id, entity_id)."""

    viewport: ViewportState = field(default_factory=ViewportState)
    width: int = DEFAULT_WIDTH
    quality: int = DEFAULT_QUALITY
    fps: float = DEFAULT_FPS


class CameraStreamCoordinator:
    """Manages active smart camera stream sessions."""

    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str], StreamSession] = {}

    def get_or_create_session(
        self,
        watch_id: str,
        entity_id: str,
        width: int = DEFAULT_WIDTH,
        quality: int = DEFAULT_QUALITY,
        fps: float = DEFAULT_FPS,
        viewport: ViewportState | None = None,
    ) -> StreamSession:
        """Get existing session or create a new one."""
        key = (watch_id, entity_id)
        session = self._sessions.get(key)
        if session is None:
            session = StreamSession(
                viewport=viewport or ViewportState(),
                width=width,
                quality=quality,
                fps=fps,
            )
            self._sessions[key] = session
        else:
            session.width = width
            session.quality = quality
            session.fps = fps
        return session

    def update_session(
        self,
        watch_id: str,
        entity_id: str,
        viewport: ViewportState | None = None,
        width: int | None = None,
    ) -> bool:
        """Update params for an active session. Returns True if session exists."""
        key = (watch_id, entity_id)
        session = self._sessions.get(key)
        if session is None:
            return False
        if viewport is not None:
            session.viewport = viewport
        if width is not None:
            session.width = int(_clamp(width, MIN_WIDTH, MAX_WIDTH))
        return True

    def remove_session(self, watch_id: str, entity_id: str) -> None:
        """Remove a session on disconnect."""
        self._sessions.pop((watch_id, entity_id), None)

    def shutdown(self) -> None:
        """Clear all sessions."""
        self._sessions.clear()


def _process_frame(
    frame_bytes: bytes,
    viewport: ViewportState,
    width: int,
    quality: int,
) -> bytes:
    """Crop, resize, and recompress a camera frame (runs in executor)."""
    img = Image.open(BytesIO(frame_bytes))

    # Crop if viewport is not full-frame
    if not (viewport.x <= 0.001 and viewport.y <= 0.001 and viewport.w >= 0.999 and viewport.h >= 0.999):
        img_w, img_h = img.size
        left = int(viewport.x * img_w)
        top = int(viewport.y * img_h)
        right = int((viewport.x + viewport.w) * img_w)
        bottom = int((viewport.y + viewport.h) * img_h)
        # Clamp to image bounds
        left = max(0, min(left, img_w - 1))
        top = max(0, min(top, img_h - 1))
        right = max(left + 1, min(right, img_w))
        bottom = max(top + 1, min(bottom, img_h))
        img = img.crop((left, top, right, bottom))

    # Resize to target width maintaining aspect ratio
    cur_w, cur_h = img.size
    if cur_w > width:
        ratio = width / cur_w
        new_h = max(1, int(cur_h * ratio))
        img = img.resize((width, new_h), Image.LANCZOS)

    # Recompress as JPEG
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class CameraStreamView(HomeAssistantView):
    """GET endpoint that serves an MJPEG stream with server-side processing."""

    url = "/api/wrist_assistant/camera/stream/{entity_id}"
    name = "api:wrist_assistant_camera_stream"
    requires_auth = True

    def __init__(self, hass: HomeAssistant, coordinator: CameraStreamCoordinator) -> None:
        self._hass = hass
        self._coordinator = coordinator

    async def get(self, request: Request, entity_id: str) -> StreamResponse:
        """Handle MJPEG stream request."""
        # Validate entity
        state = self._hass.states.get(entity_id)
        if state is None or not entity_id.startswith("camera."):
            return Response(text="Invalid camera entity", status=404)

        # Parse query params
        query = request.query
        width = int(_clamp(float(query.get("width", DEFAULT_WIDTH)), MIN_WIDTH, MAX_WIDTH))
        quality = int(_clamp(float(query.get("quality", DEFAULT_QUALITY)), MIN_QUALITY, MAX_QUALITY))
        fps = _clamp(float(query.get("fps", DEFAULT_FPS)), MIN_FPS, MAX_FPS)
        watch_id = query.get("watch_id", "unknown")

        # Parse optional initial viewport
        viewport = ViewportState()
        if "x" in query:
            viewport.x = _clamp(float(query.get("x", 0)), 0, 1)
            viewport.y = _clamp(float(query.get("y", 0)), 0, 1)
            viewport.w = _clamp(float(query.get("w", 1)), 0.01, 1)
            viewport.h = _clamp(float(query.get("h", 1)), 0.01, 1)

        session = self._coordinator.get_or_create_session(
            watch_id, entity_id, width, quality, fps, viewport
        )

        # Set up MJPEG response
        response = StreamResponse(
            status=200,
            headers={
                "Content-Type": "multipart/x-mixed-replace; boundary=frame",
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
            },
        )
        await response.prepare(request)

        frame_interval = 1.0 / fps

        try:
            while True:
                # Read current viewport from session (may be updated by POST endpoint)
                current_viewport = session.viewport
                current_width = session.width
                current_quality = session.quality

                try:
                    # Get frame from HA camera platform
                    image: CameraImage = await async_get_image(
                        self._hass, entity_id, timeout=5
                    )
                    if image is None or image.content is None:
                        await asyncio.sleep(frame_interval)
                        continue

                    # Process frame in executor (PIL is sync/CPU-bound)
                    processed = await self._hass.async_add_executor_job(
                        _process_frame,
                        image.content,
                        current_viewport,
                        current_width,
                        current_quality,
                    )

                    # Write MJPEG frame
                    await response.write(
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(processed)).encode() + b"\r\n"
                        b"\r\n" + processed + b"\r\n"
                    )

                except (ConnectionResetError, ConnectionAbortedError):
                    break
                except HomeAssistantError:
                    _LOGGER.debug("Camera unavailable for %s, retrying", entity_id)
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("Frame error for %s, continuing", entity_id)

                await asyncio.sleep(frame_interval)
        except asyncio.CancelledError:
            pass
        finally:
            self._coordinator.remove_session(watch_id, entity_id)
            _LOGGER.debug("Smart stream ended for %s (watch: %s)", entity_id, watch_id)

        return response


class CameraViewportView(HomeAssistantView):
    """POST endpoint to update the crop viewport for an active stream."""

    url = "/api/wrist_assistant/camera/viewport"
    name = "api:wrist_assistant_camera_viewport"
    requires_auth = True

    def __init__(self, coordinator: CameraStreamCoordinator) -> None:
        self._coordinator = coordinator

    async def post(self, request: Request) -> Response:
        """Update stream params (viewport and/or width) for an active session."""
        try:
            payload = await request.json()
        except (ValueError, UnicodeDecodeError):
            return self.json_message("Invalid JSON body", status_code=400)

        if not isinstance(payload, dict):
            return self.json_message("Expected JSON object", status_code=400)

        entity_id = payload.get("entity_id")
        watch_id = payload.get("watch_id")
        if not isinstance(entity_id, str) or not isinstance(watch_id, str):
            return self.json_message("entity_id and watch_id required", status_code=400)

        # Optional viewport
        viewport = None
        if any(k in payload for k in ("x", "y", "w", "h")):
            viewport = ViewportState(
                x=_clamp(float(payload.get("x", 0)), 0, 1),
                y=_clamp(float(payload.get("y", 0)), 0, 1),
                w=_clamp(float(payload.get("w", 1)), 0.01, 1),
                h=_clamp(float(payload.get("h", 1)), 0.01, 1),
            )

        # Optional width
        width = None
        if "width" in payload:
            width = int(float(payload["width"]))

        if self._coordinator.update_session(watch_id, entity_id, viewport=viewport, width=width):
            return self.json({"status": "ok"})
        return self.json_message("No active stream for this session", status_code=404)
