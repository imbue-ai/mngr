The docker provider moved out of the core package into the `imbue-mngr-docker` plugin (`libs/mngr_docker/`).

- A plain `imbue-mngr` install no longer registers a `docker` backend or depends on the `docker` and `requests` packages. Install `imbue-mngr-docker` to get it back; the install script offers it when a Docker daemon answers, the plugin catalog lists it, and a `[providers.docker]` block found without the plugin now names the package to install.

- `mngr help docker_usage` and the `mngr docker` command group come from the plugin. The docker-specific error classes moved to `imbue.mngr_docker.errors` under the same names.

- Generic provider messages no longer assume docker is built in: the unknown-provider hint points at `mngr plugin list`, and the default provider-unavailable help no longer says "start Docker".

- Test support: `load_local_backend_only` skips the docker backend like the other external backends (there is no `include_docker` flag any more); the leaked docker container sweep and the release-sandbox dockerd startup fixture moved to the plugin; `MockProviderInstance` can now back a readable offline host with a local volume and persist agent data in memory, which the tests that previously borrowed the docker provider for that now use.
