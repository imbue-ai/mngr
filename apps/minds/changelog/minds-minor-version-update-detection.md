- Imbue Studio now prompts a workspace to update only when a newer minor release of the workspace template is available. With the app on 0.8.4, workspaces on 0.7.x and older show "Update available" and are covered by "Update all" / "Schedule all", while workspaces on 0.8.0-0.8.3 are not badged, bannered, or bulk-updated. A prerelease counts as the minor release it precedes.

- A workspace behind only by a patch release can still be updated or scheduled by hand from its Settings > Updates, and a schedule the user armed for it runs in the update window like any other.

- The release runbook (`docs/deploy/ops/app-release.md`) now explains that choosing a patch or a minor bump decides whether existing workspaces are prompted to update.
