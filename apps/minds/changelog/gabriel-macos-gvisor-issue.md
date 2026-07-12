- The Docker compute provider now picks its container runtime per platform
  instead of always using gVisor (`runsc`). The create form gained an advanced
  "Container runtime" setting (runc vs runsc) that defaults to runc on macOS
  (where gVisor is unavailable) and runsc on Linux, and can be overridden per
  workspace. macOS users no longer need the
  `MNGR__PROVIDERS__DOCKER__DOCKER_RUNTIME=runc` environment-variable workaround
  to create a local Docker workspace.

- Under the hood, selecting runsc stacks a new `docker_runsc` create-template
  overlay (in default-workspace-template) on top of the shared `docker` template,
  so the gVisor choice is the only difference between the two runtimes and the
  runc path -- the default -- is now what runs on macOS.

- The create-form/API runtime default now honors a `MINDS_DOCKER_RUNTIME_DEFAULT`
  environment override. It is unset in real deployments (so Linux still defaults
  to runsc) and is set to `RUNC` by the e2e/snapshot test paths, whose Docker
  daemon has no gVisor -- this is the layer that decides whether the create
  stacks `docker_runsc` at all, which the mngr provider-config env var cannot
  undo once the template is explicitly stacked.
