Workspaces can now be renamed. A new `POST /api/v1/workspaces/<agent_id>/rename` endpoint updates the workspace's normalized host name (slug) and its human-readable display name together, so the two never drift. When the new name normalizes to the same slug as the current host name, only the display name changes (no host rename -- works on every provider, online or offline). Collisions with another active workspace on the same provider are rejected (409).

Workspace names are now decoupled into two concepts with a single canonical home each:

- The human-readable display name is arbitrary (any characters, mixed case). It lives in a `workspace_display_name` label on the workspace's `system-services` agent, and is what the UI shows.

- The host name is a normalized lowercase slug derived from the display name (non-alphanumeric runs become dashes, truncated). A name that normalizes to nothing (e.g. all punctuation/emoji) is rejected.

The `workspace` label has been removed entirely: workspace discovery now keys off the `is_primary` label, per-provider name collisions are checked against the actual host name, and the display name comes from the new label. Legacy workspaces (created before the display label existed) fall back to showing their host name; this fallback is temporary and can be removed around September 2026. (The companion forever-claude-template change that drops the in-container `mngr list` `has(labels.workspace)` filter must be released before this ships, so the primary agent is not hidden from that listing.)

The imbue_cloud LiteLLM key is no longer minted with `host_name` metadata.
