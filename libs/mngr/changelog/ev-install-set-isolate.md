`mngr config wizard` now covers two more common user-scope settings.

- Docker host-volume isolation: the wizard asks whether each docker host should see only its own `host_dir` sub-folder (the recommended, forthcoming default; requires Docker Engine >= 25.0) or keep the legacy shared state volume, writing `providers.docker.isolate_host_volumes`. Opting in silences the one-shot deprecation warning that a fresh install otherwise hits the first time it uses docker.

- Default agent type: the "pick a default agent type for `mngr create`" step moved here from `mngr extras`. Because the wizard runs as its own step in the installer (after plugins are installed), it now sees freshly-installed plugin agent types instead of only those registered at startup.

Each step short-circuits when its setting is already configured, so re-running the wizard only fills gaps. `mngr extras` no longer has a `config` subcommand or a default-agent-type step; user-scope config setup lives entirely in `mngr config wizard`.
