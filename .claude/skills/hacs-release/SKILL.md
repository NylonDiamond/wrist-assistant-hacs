---
name: hacs-release
description: Bump version, create PR, merge, and publish a GitHub release so HACS can pick up the update.
argument-hint: "[version e.g. 0.7.0]"
disable-model-invocation: true
---

Create a full HACS release. The version argument is $ARGUMENTS (if not provided, auto-increment the patch version from manifest.json).

## Steps

1. **Determine version**: Use the provided version, or read `custom_components/wrist_assistant/manifest.json` and increment the patch number.
2. **Check git status**: Ensure there are changes to release (staged, unstaged, or already committed on the branch ahead of main). If the working tree is clean and on main with nothing ahead, abort — there's nothing to release.
3. **Bump version** in `custom_components/wrist_assistant/manifest.json`.
4. **Create a branch** named after the release (e.g., `release/v0.7.0`).
5. **Stage and commit** all changes (including the version bump) with a descriptive commit message. Include `Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>`.
6. **Push** the branch with `-u`.
7. **Create a PR** with a summary of changes and a test plan.
8. **Merge the PR** with `gh pr merge --merge --admin`.
9. **Pull main** so local is up to date.
10. **Create a GitHub release** with `gh release create vX.Y.Z` including release notes summarizing the changes.
11. **Report** the release URL to the user.
