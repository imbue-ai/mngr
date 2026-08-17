Cut the minds-v0.3.17 release: bump the app version to 0.3.17 and pin `FALLBACK_BRANCH` to the `minds-v0.3.17` default-workspace-template tag.

Bundle the `imbue-mngr-codex` and `imbue-mngr-pi-coding` plugins into the desktop app (build.js, env-setup.js, electron pyproject, and the drift-guard list). The default workspace template's `.mngr/settings.toml` now declares `[agent_types.codex]` and `[agent_types.pi-coding]`, and without these plugins the app's bundled mngr rejected those sections as unknown fields, failing every workspace create ("Unknown fields in agent_types.codex").

Regenerate the dev-time `electron/pyproject/uv.lock`, which had also drifted (it was missing mngr's new `python-frontmatter` dependency).
