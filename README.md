# Wrist Assistant for Home Assistant

**Coming soon:** The Wrist Assistant app is not released yet.

Official Home Assistant integration for the [Wrist Assistant](https://apps.apple.com/us/search?term=Wrist%20Assistant) Apple Watch app (coming soon).

The iOS app is currently going through Apple App Store review and release processes.

Wrist Assistant gives you automatic, real-time two-way sync between Apple Watch and Home Assistant, with a setup experience that is fast, reliable, and hands-off.

## Why install this

- Fast watch updates for a snappy feel
- Automatic watch pairing
- Real-time two-way sync between your watch and Home Assistant
- Multi-watch support for shared homes
- Reliable background sync with minimal effort

## Install

### iOS app onboarding (recommended, coming soon)

1. Install the Wrist Assistant iOS app.
2. Go through the onboarding steps.
3. The app installs and sets up the Home Assistant integration automatically for you.

### HACS (available now)

1. Open HACS -> Integrations.
2. Search for `Wrist Assistant`.
3. Install and restart Home Assistant.
4. Go to Settings -> Devices & Services -> Add Integration -> `Wrist Assistant`.

### Manual

1. Copy `custom_components/wrist_assistant` into `<config>/custom_components/`.
2. Restart Home Assistant.
3. Go to Settings -> Devices & Services -> Add Integration -> `Wrist Assistant`.

## What you get in Home Assistant

- Automatic watch pairing during onboarding
- Real-time two-way sync for fast state and control updates
- Smooth multi-watch support for shared homes

## Services

### `wrist_assistant.send_notification`

Send push notifications directly to paired Apple Watches via APNs. Watches register their push tokens automatically during pairing — no extra setup needed.

**Basic notification:**

```yaml
service: wrist_assistant.send_notification
data:
  title: "Door Alert"
  message: "Front door was opened"
  sound: "default"
```

**Target a specific watch:**

```yaml
service: wrist_assistant.send_notification
data:
  message: "Garage door left open"
  target: "my-watch-id"
```

**Actionable notification (entity toggle):**

```yaml
service: wrist_assistant.send_notification
data:
  title: "Living Room"
  message: "Lights are still on"
  category: "ENTITY_TOGGLE"
  data:
    entity_id: "light.living_room"
```

**Silent background update:**

```yaml
service: wrist_assistant.send_notification
data:
  message: "sync"
  push_type: "background"
```

| Field | Required | Description |
|-------|----------|-------------|
| `message` | Yes | Notification body text |
| `title` | No | Notification title |
| `target` | No | Watch ID — omit to send to all watches |
| `category` | No | `ENTITY_TOGGLE`, `LOCK_CONTROL`, `ALARM_CONTROL`, `CONFIRM_ACTION`, `SCENE_ACTIVATE`, or `HA_CUSTOM` |
| `data` | No | Extra payload (e.g. `entity_id`, `domain`, `service`, `actions`) |
| `sound` | No | `"default"` for system sound, omit for silent |
| `push_type` | No | `"alert"` (default) or `"background"` for silent updates |

### `wrist_assistant.create_pairing_code`

Generate a one-time pairing code for the Wrist Assistant app. Returns a `pairing_uri` and `pairing_code`.

```yaml
service: wrist_assistant.create_pairing_code
data:
  local_url: "http://homeassistant.local:8123"
  remote_url: "https://ha.example.com"
```

### `wrist_assistant.force_resync`

Force all connected watches to perform a full state refresh on their next poll.

```yaml
service: wrist_assistant.force_resync
```

## Screenshots and GIFs

Visual setup guide coming soon (integration card, pairing flow, and watch sync in action).

## License

MIT
