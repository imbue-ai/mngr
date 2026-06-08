Fixed the `test_create_docker_custom_dockerfile` e2e release test, which had been
failing since the `mngr create` agent-type default was moved into user config
(an explicit `--type` is now required). The test now passes `--type command` and
builds the custom image from `debian:bookworm-slim` (which provides `apt-get` for
the required host packages) instead of `alpine`, and verifies the custom Dockerfile
was actually used by reading back a marker file baked into the image.
