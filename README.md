# Wrist Assistant for Home Assistant

Official Home Assistant integration for the [Wrist Assistant](https://github.com/NylonDiamond/ha-watch) Apple Watch app.

Wrist Assistant adds fast delta-sync updates and a built-in QR pairing flow, so watch users can connect quickly and receive near real-time state changes without repeatedly downloading full entity state.

## Why install this

- Faster watch updates with lower bandwidth usage (delta sync + long-poll)
- Built-in QR pairing in Home Assistant (no manual token copy/paste)
- Support for multiple watches with independent subscriptions
- Recovery helpers for stale cursors and forced resync
- Bounded memory event buffer for stable runtime behavior

## Install

### HACS default store (once listed)

1. Open HACS -> Integrations.
2. Search for `Wrist Assistant`.
3. Install and restart Home Assistant.
4. Go to Settings -> Devices & Services -> Add Integration -> `Wrist Assistant`.

### HACS custom repository (while waiting for default listing)

1. Open HACS -> Integrations.
2. Open the 3-dot menu -> Custom repositories.
3. Add `https://github.com/NylonDiamond/homeassistant-wrist-assistant`.
4. Category: `Integration`.
5. Install `Wrist Assistant` and restart Home Assistant.
6. Go to Settings -> Devices & Services -> Add Integration -> `Wrist Assistant`.

### Manual

1. Copy `custom_components/wrist_assistant` into `<config>/custom_components/`.
2. Restart Home Assistant.
3. Go to Settings -> Devices & Services -> Add Integration -> `Wrist Assistant`.

## First-time setup (about 1 minute)

1. Install and add the integration.
2. Open the persistent notification: `Wrist Assistant pairing ready`.
3. In the watch app, open Sign in -> Scan QR.
4. Scan the QR shown in Home Assistant.
5. In the watch app settings, choose update mode `Auto` or `Delta`.

## What you get in Home Assistant

- `camera` entity: Pairing QR
- `button` entity: Refresh pairing QR
- `sensor` entity: Pairing expires at
- Per-watch diagnostic entities (activity, subscriptions, poll interval, sync status, and naming)
- Services: `wrist_assistant.create_pairing_code`, `wrist_assistant.force_resync`

## Screenshots and GIFs

Visual setup guide coming soon (integration card, pairing QR flow, and watch sync in action).

## Troubleshooting

- No pairing QR visible: Open the Wrist Assistant device page and press `Refresh pairing QR`. If still missing, restart Home Assistant and reopen the device page.
- Watch reports out-of-sync data: Run `wrist_assistant.force_resync` and let the watch reconnect.
- Pairing code expired: Press `Refresh pairing QR` or call `wrist_assistant.create_pairing_code` to generate a fresh code.

## Security notes

- Pairing codes are one-time and short-lived.
- Redeeming a pairing code creates a long-lived access token.
- Token lifespan is configurable with `lifespan_days` in `wrist_assistant.create_pairing_code`.
- Delta sync endpoint requires authentication with a Home Assistant token.

## Advanced API reference

### `POST /api/watch/updates`

Authenticated long-poll endpoint for watch delta updates.

Example request body:

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
| `config_hash` | string | yes | Hash of current watch configuration; changed hash triggers re-subscribe |
| `since` | string | no | Cursor from previous response; omit on first request |
| `entities` | string[] | no | Entity IDs to subscribe to; send when config changes |
| `timeout` | integer | no | Long-poll timeout in seconds (default 45, clamped to 5-55) |

Response behavior:

- `200`: Delta events returned with `next_cursor`
- `204`: No changes within timeout
- `410`: Cursor stale/invalid; client should do full refresh and restart cursor
- `200` with `need_entities: true`: resend request with `entities`

### `POST /api/wrist_assistant/pairing/redeem`

Unauthenticated one-time code redemption endpoint used by QR pairing.

Example request body:

```json
{
  "pairing_code": "kV2..."
}
```

Example response body:

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

Generates a short-lived one-time pairing code and returns a payload with `pairing_uri`.

Key response fields:

- `pairing_code`: one-time code
- `pairing_uri`: payload to encode as QR (`wristassistant://pair?...`)
- `expires_at`: UTC expiration timestamp
- `lifespan_days`: token lifespan (default 3650)
- `home_assistant_url`, `local_url`, `remote_url`: URLs included in pairing payload

## Release process

For each GitHub release, use `/RELEASE_NOTES_TEMPLATE.md` to write user-facing notes for HACS.

## License

MIT
