The docker provider is now its own plugin, `imbue-mngr-docker`, split out of the core `imbue-mngr` package.

- `mngr create @.docker`, `mngr docker resize`, `mngr help docker_usage`, and the `[providers.docker]` settings are unchanged, but they need the plugin installed: `uv tool install imbue-mngr --with imbue-mngr-docker`, or `mngr plugin add imbue-mngr-docker` on an existing install. The install script offers the plugin when a Docker daemon answers.

- The backend and the `mngr docker` command group register lazily, so `mngr --help`, `mngr config`, and `mngr list` no longer import the docker SDK.

- The docker-specific error classes (`DockerBuildTimeoutError`, `DockerRuntimeNotRegisteredError`, `DockerGvisorEphemeralRootfsError`, `DockerConfigValidationError`, `InvalidContainerSizeError`) moved to `imbue.mngr_docker.errors`; their names, and so the `error_class` in jsonl output, are unchanged.

- Test support: the docker CLI and SDK resource guards, and the session-end sweep of leaked test containers, are declared through the plugin's own `resource_guards` entry point, so every pytest session in which the plugin is installed enforces the `docker` / `docker_sdk` marks and sweeps leaked containers.
