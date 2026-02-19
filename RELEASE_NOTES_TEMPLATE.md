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

- Behavior updates that are not fixes (for example: URL discovery logic, setup flow adjustments).

## Fixed

- Bug fixes (for example: stale cursor handling, QR refresh behavior, async/runtime issues).

## Pairing and onboarding

- QR pairing flow updates, token handling, URL fields, notification behavior.

## Watch sync and reliability

- Delta sync stability, 204/410 handling, session lifecycle, reconnect behavior.

## Diagnostics and observability

- Sensor/entity diagnostics, per-watch visibility, troubleshooting helpers.

## Internal and maintenance (optional)

- Validation/workflow/refactor work that may matter to advanced users.

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
- Mention if any service/entity names changed.
- Keep final notes concise (usually 5-12 bullets total).
