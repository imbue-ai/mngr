Removed a duplicated definition of the plugin catalog's gate classes (`SignalGate`, `RequiredPackagesGate`, `Gate`), which were declared twice back to back.

The release Docker-in-Docker image (`Dockerfile.release.extras`) now installs the buildx CLI plugin beside the static Docker binaries, so `docker build` inside it uses BuildKit; the legacy builder rejects the `--secret` flag and `RUN --mount` the workspace template now uses.
