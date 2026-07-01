- The Docker compute provider now picks its container runtime per platform
  instead of always using gVisor (`runsc`). The create form gained an advanced
  "Container runtime" setting (runc vs runsc) that defaults to runc on macOS
  (where gVisor is unavailable) and runsc on Linux, and can be overridden per
  workspace. macOS users no longer need the
  `MNGR__PROVIDERS__DOCKER__DOCKER_RUNTIME=runc` environment-variable workaround
  to create a local Docker workspace.

- Under the hood, selecting runsc stacks a new `docker_runsc` create-template
  overlay (in forever-claude-template) on top of the shared `docker` template,
  so the gVisor choice is the only difference between the two runtimes and the
  runc path -- the default -- is now what runs on macOS.
