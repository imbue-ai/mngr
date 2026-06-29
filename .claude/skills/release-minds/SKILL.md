---
name: release-minds
argument-hint: <version>
description: Cut a new production release of the minds app (version bump, FALLBACK_BRANCH, vendor/mngr sync, and minds-v<version> tags on both mngr and forever-claude-template, proven green on CI). The full procedure lives in apps/minds/docs/release.md in the mngr checkout; this skill defers to it. Use when the user asks to "release a new version of minds", "cut a minds release", "bump the minds version", "update the vendored mngr in forever-claude-template", or anything of that shape.
---

# Release a new version of the minds app

The full procedure lives in **`apps/minds/docs/release.md`** in the mngr checkout — the single source of truth. This skill only routes you there.

1. Resolve the target version from the `args` passed to this skill (e.g. `0.3.2`); it becomes the `minds-v<version>` tag the runbook applies to both repos. If no version was given, ask the user first.
2. Read `apps/minds/docs/release.md` and follow its "Session setup" and "Procedure" sections as written.
