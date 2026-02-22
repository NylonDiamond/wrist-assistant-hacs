# Release Notes Template (HACS + GitHub)

Use this for each GitHub release. HACS users read this before updating.

## Before writing

1. Find previous tag: `git tag --sort=-creatordate | head -n 2`
2. Gather commits: `git log --oneline <previous_tag>..HEAD`
3. Keep only user-visible changes; skip noise/temporary commits.

## Title

`Wrist Assistant vX.Y.Z`

## Quick summary (one line)

- 

## Highlights

- 

## Added

- New capabilities users can see or use (for example: new entities, services, pairing UX improvements).

## Changed

- Behavior updates that are not fixes (for example: setup flow adjustments and quality-of-life improvements).

## Fixed

- Bug fixes users can notice (for example: reconnect issues, setup friction, reliability problems).

## Setup and onboarding

- Pairing experience updates, setup clarity, and first-run improvements.

## Watch experience and reliability

- Responsiveness, consistency, reconnect behavior, and overall watch experience quality.

## Diagnostics and observability

- Sensor/entity diagnostics, per-watch visibility, troubleshooting helpers.

## Breaking changes

- None.

## Upgrade steps

1. Update in HACS.
2. Restart Home Assistant.
3. Open Wrist Assistant on the watch and let it reconnect.
4. If sync appears stale, run `wrist_assistant.force_resync`.

## Known issues (optional)

- 

## Maintainer checklist

- Mention if Home Assistant minimum version changed.
- Mention if users need to re-pair.
- Mention if any user-facing names changed.
- Keep final notes concise (usually 5-12 bullets total).
