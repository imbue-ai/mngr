- e2e tutorial tests: the shared `e2e` fixture now configures a default agent
  type (`claude`) under `[commands.create]`. The source-coded default was
  removed (the installer writes it to user config), so tutorial commands that
  omit `--type` (e.g. `mngr create my-task --provider modal`) previously failed
  with "No agent type provided" in the isolated test environment.
- `test_create_modal_dockerfile_and_context` now writes a Dockerfile that
  `COPY`s a marker file out of the build context, so a successful `mngr create`
  genuinely exercises both `--file` and `--context-dir` (the prior version used
  a bare `FROM` with an empty context, leaving `--context-dir` untested).
