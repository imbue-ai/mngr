- minds and the packaged desktop app now depend on the new `imbue-mngr-docker` plugin, which is where mngr's docker provider (the `LaunchMode.DOCKER` compute provider) lives after being split out of the core `imbue-mngr` package. No behavior change.

- Comment-only: the workspace-recovery docstring's pointer to mngr's docker usage page now names `mngr help docker_usage`, since that page moved into the plugin.

- Regenerated the packaged desktop app's `apps/minds/electron/pyproject/uv.lock` so it records the new `imbue-mngr-docker` dependency.
