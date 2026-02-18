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
4. Add this repository URL: `https://github.com/NylonDiamond/homeassistant-wrist-assistant`
5. Select category: **Integration**
6. Click **Add**, then find "Wrist Assistant" in the integration list and install it
7. Restart Home Assistant
8. Go to **Settings** → **Devices & Services** → **Add Integration** → search for **Wrist Assistant**

### Manual

1. Copy the `custom_components/wrist_assistant` directory to your Home Assistant `<config>/custom_components/` directory
2. Restart Home Assistant
3. Go to **Settings** → **Devices & Services** → **Add Integration** → search for **Wrist Assistant**

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

### `POST /api/wrist_assistant/pairing/redeem`

Unauthenticated one-time code redemption endpoint used by QR setup flow.

#### Request body

```json
{
  "pairing_code": "kV2..."
}
```

#### Response body

```json
{
  "access_token": "eyJ...",
  "token_type": "Bearer",
  "auth_mode": "manual_token",
  "expires_in": 315360000,
  "home_assistant_url": "https://ha.example.com",
  "local_url": "http://homeassistant.local:8123",
  "remote_url": "https://ha.example.com"
}
```

### `wrist_assistant.create_pairing_code` service

Creates a short-lived one-time pairing code and returns a payload suitable for QR generation.

#### Service response fields

- `pairing_code` — one-time code (expires in 10 minutes)
- `pairing_uri` — payload to encode as QR, e.g. `wristassistant://pair?...`
- `expires_at` — UTC expiration timestamp
- `lifespan_days` — long-lived token lifespan (default 3650 days)
- `home_assistant_url` / `local_url` / `remote_url` — URLs included in pairing payload

Use this from **Developer Tools -> Actions**, then encode `pairing_uri` as a QR code and scan it from Wrist Assistant's **Sign in -> Scan QR** path.

## Wrist Assistant App

This integration is the server-side component for [Wrist Assistant](https://github.com/NylonDiamond/ha-watch), an Apple Watch app for controlling Home Assistant entities. In the watch app settings, set the update mode to **Auto** or **Delta** to use this endpoint.

## License

MIT
