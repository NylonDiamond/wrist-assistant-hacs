# Wrist Assistant - Home Assistant Integration

Custom integration for [Home Assistant](https://www.home-assistant.io/) that adds a high-performance delta-sync API endpoint used by the [Wrist Assistant](https://github.com/NylonDiamond/ha-watch) Apple Watch app.

## What it does

This integration registers a single authenticated HTTP endpoint (`POST /api/watch/updates`) that provides near-real-time entity state updates to the watch app via long-polling. Instead of fetching all entity states on every poll, the watch sends a cursor and receives only the changes since that cursor — dramatically reducing bandwidth and latency.

### Key features

- **Long-poll delta sync** — the watch holds a connection open for up to 55 seconds, receiving updates the instant they happen
- **Per-watch session tracking** — multiple watches can connect simultaneously, each with their own entity subscriptions
- **Automatic cursor management** — stale cursors trigger a resync signal so the watch can recover gracefully
- **Bounded memory** — uses a fixed-size ring buffer (5,000 events) so memory usage stays constant

## Installation

### HACS (recommended)

1. Open HACS in your Home Assistant UI
2. Go to **Integrations**
3. Click the three dots menu (top right) and select **Custom repositories**
4. Add this repository URL: `https://github.com/NylonDiamond/wrist-assistant-hacs`
5. Select category: **Integration**
6. Click **Add**, then find "Wrist Assistant" in the integration list and install it
7. Restart Home Assistant
8. Add `halights_watch:` to your `configuration.yaml` (see below)

### Manual

1. Copy the `custom_components/halights_watch` directory to your Home Assistant `<config>/custom_components/` directory
2. Restart Home Assistant
3. Add `halights_watch:` to your `configuration.yaml` (see below)

## Configuration

Add this to your `configuration.yaml`:

```yaml
halights_watch:
```

No additional options are required. Restart Home Assistant after adding.

## API Reference

### `POST /api/watch/updates`

Authenticated long-poll endpoint. Requires a valid Home Assistant long-lived access token in the `Authorization` header.

#### Request body

```json
{
  "watch_id": "UUID",
  "since": "123",
  "config_hash": "f88e947d...",
  "entities": ["light.kitchen", "switch.fan"],
  "timeout": 45
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `watch_id` | string | yes | Unique identifier for this watch |
| `config_hash` | string | yes | Hash of the current watch configuration — triggers entity re-sync when changed |
| `since` | string | no | Cursor from previous response. Omit on first request |
| `entities` | string[] | no | List of entity IDs to subscribe to. Send when config changes; omit on subsequent polls |
| `timeout` | integer | no | Long-poll timeout in seconds (default 45, clamped to 5–55) |

#### Responses

**`200`** — Delta events available:

```json
{
  "events": [
    {
      "entity_id": "light.kitchen",
      "state": "on",
      "new_state": {
        "entity_id": "light.kitchen",
        "state": "on",
        "attributes": { "brightness": 128 },
        "last_updated": "2026-02-09T22:11:01.123456+00:00"
      },
      "context_id": "01J....",
      "last_updated": "2026-02-09T22:11:01.123456+00:00"
    }
  ],
  "next_cursor": "124",
  "need_entities": false,
  "resync_required": false
}
```

**`204`** — No changes within the timeout period.

**`410`** — Cursor is stale or invalid. Client should perform a full state refresh and start with a fresh cursor.

**`200` with `need_entities: true`** — Server needs the entity list. Client should resend the request with the `entities` array populated.

## Wrist Assistant App

This integration is the server-side component for [Wrist Assistant](https://github.com/NylonDiamond/ha-watch), an Apple Watch app for controlling Home Assistant entities. In the watch app settings, set the update mode to **Auto** or **Delta** to use this endpoint.

## License

MIT
