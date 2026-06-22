---
name: release-minds
argument-hint: <version>
description: Cut a new production release of the minds app (version bump, FALLBACK_BRANCH, vendor/mngr sync, and minds-v<version> tags on both mngr and forever-claude-template, proven green on CI). The full procedure lives in apps/minds/docs/release.md in the mngr checkout; this skill defers to it. Use when the user asks to "release a new version of minds", "cut a minds release", "bump the minds version", "update the vendored mngr in forever-claude-template", or anything of that shape.
---

# Release a new version of the minds app

The canonical, maintained runbook is **`apps/minds/docs/release.md`** in the mngr checkout. It is the single source of truth for the release process — read it in full and follow it. This skill is a thin pointer so the process is documented in exactly one place and cannot drift between a skill copy and the doc.

## What to do

1. Resolve the target version from the `args` passed to this skill (e.g. `0.3.2`). It maps to the `minds-v<version>` tag the runbook applies to both `mngr` and `forever-claude-template`. If no version was supplied, ask the user before doing anything.
2. Read `apps/minds/docs/release.md` from the root of the mngr checkout you are working in. Follow it as written — do not work from memory or from any older description of the release flow.
3. Follow that runbook's "Session setup" and "Procedure" sections exactly. It covers `$GH_TOKEN` (per the repo's GitHub-account rules), the `$MNGR` / `$FCT` checkout paths and the gitignored `apps/minds/.env` `FCT_DIR`, the version + `FALLBACK_BRANCH` bump, the `just sync-vendor-mngr` recipe, the two PRs that both target `main`, and tagging the verified mngr SHA as `minds-v<version>` on both repos.

## Notes

- There is no `~/project/minds_prod` step, and no long-lived `minds_v<version>` release branch. Releases are cut from your normal mngr checkout's `main` via two PRs; the runbook derives every path from the checkout and `apps/minds/.env` (or asks you). Do not introduce hardcoded clone paths.
- If `apps/minds/docs/release.md` is missing from the checkout, stop and tell the user — do not improvise a release.
