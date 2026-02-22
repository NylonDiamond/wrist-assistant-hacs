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

## Screenshots and GIFs

Visual setup guide coming soon (integration card, pairing flow, and watch sync in action).

## License

MIT
