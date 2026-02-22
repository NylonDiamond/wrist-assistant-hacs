# HA API Alternatives Research

> Can the HACS integration be replaced by using Home Assistant's built-in APIs directly?
>
> **TL;DR: No.** The integration uniquely combines HTTP long-poll + server-side entity filtering + delta-only payloads + WatchOS compatibility. No combination of built-in HA APIs replicates all four on WatchOS.

## 1. HA Built-in API Capabilities

### WebSocket `subscribe_entities`
- Server-side entity filtering via `entity_ids` parameter + compressed deltas (~80% bandwidth reduction).
- Available since HA 2022.4.
- The gold standard for real-time state updates — but requires WebSocket.

### SSE `GET /api/stream`
- Long-lived HTTP streaming (Server-Sent Events).
- **No entity filtering** — only event type filtering via `?restrict=` query parameter.
- Requires admin-level authentication.
- Semi-deprecated: not documented in the official HA REST API docs.

### `POST /api/template`
- Single HTTP request, returns rendered Jinja2 template.
- Can embed entity list + timestamps for delta detection client-side.
- Zero HA config needed — works out of the box.
- Polling only.

### History API
- `GET /api/history/period/<timestamp>?filter_entity_id=x,y&minimal_response`
- True server-side time + entity filtering.
- Queries the recorder database directly.
- Polling only.

### REST `GET /api/states/<entity_id>`
- Per-entity polling. N requests for N entities.
- Simplest approach but worst scaling.

## 2. WatchOS Platform Restrictions

**WebSocket is completely blocked on WatchOS 9+ (September 2022).**

This is not a reliability issue — it is a hard platform restriction. Both `URLSessionWebSocketTask` and `NWConnection` WebSocket produce `Error: Operation not supported by device`. The only exception is active audio streaming sessions.

Key facts:
- Works in Simulator (uses macOS networking stack) but **fails 100% on real hardware**.
- Every major WebSocket library (Pusher, Starscream, Socket.IO, Firebase) dropped WatchOS support.
- WatchOS 10 and 11 did **not** lift this restriction.
- Standard HTTP (`URLSessionDataTask`) works reliably on WatchOS.

## 3. Comparison Table

| Approach | Server push? | Entity filtering? | Delta? | WatchOS? |
|---|---|---|---|---|
| **Integration (current)** | Yes (long-poll) | Yes (server) | Yes | **Yes** |
| WS `subscribe_entities` | Yes | Yes | Yes (compressed) | **NO — blocked** |
| SSE `/api/stream` | Yes | **NO** | No | Uncertain (untested) |
| `POST /api/template` | No (poll) | Yes (template) | Yes (timestamps) | Yes |
| History API | No (poll) | Yes (server) | Yes (time-based) | Yes |

## 4. Potential HA Core PR

### Option A: Small PR — Add entity filtering to `/api/stream`
- Add `entity_id` query parameter to the existing SSE endpoint (~10 lines of code).
- **Acceptance chance: ~20%** — the endpoint is semi-deprecated and not officially documented.

### Option B: Better PR — New SSE endpoint
- New `GET /api/states/stream` SSE endpoint mirroring `subscribe_entities` behavior with `entity_ids` + compressed deltas over HTTP.
- **Acceptance chance: ~30-40%** — solves a real gap in the API surface.

### Key argument for either PR
> "Apple Watch and embedded devices cannot use WebSocket due to platform restrictions. There is no HTTP-based alternative for real-time entity state streaming."

### Precedent
The `entity_ids` parameter was added to the WebSocket `subscribe_entities` command specifically for the Android Companion app's performance needs. A similar motivation exists here for HTTP-based clients.

## 5. Conclusion

The integration provides unique value that cannot be fully replicated with built-in HA APIs:

1. **HTTP long-poll** — works on WatchOS (unlike WebSocket)
2. **Server-side entity filtering** — reduces bandwidth (unlike SSE `/api/stream`)
3. **Delta-only payloads** — minimizes transfer size
4. **WatchOS compatibility** — the combination of the above three

The closest alternative would be polling `POST /api/template` with timestamp-based delta detection, but this trades real-time push for polling latency and puts template logic burden on the client.

A successful HA Core PR adding an HTTP-based streaming endpoint with entity filtering would be the only path to eliminating the need for this integration.
