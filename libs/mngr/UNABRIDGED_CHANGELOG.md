# Unabridged Changelog - mngr

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-08

# Install the agent-coordination Claude Code skills

`mngr extras claude-plugin` now installs more than just the code review
plugin. It offers two Claude Code plugins and lets you install either or
both:

- `imbue-code-guardian` -- automated code review enforcement (unchanged).
- `imbue-mngr-skills` -- the `message-agent`, `wait-for-agent`, `find-agent`,
  and `mngr-help` skills for working with mngr, published from the dedicated
  `imbue-ai/mngr-claude-skills` repo.

With an interactive terminal the step shows a checkbox picker of the
not-yet-installed plugins (all preselected; Space toggles, Enter confirms),
matching the multi-select UI of the `mngr extras plugins` wizard;
`mngr extras claude-plugin -y` auto-installs every plugin that is not already
present. `mngr extras` status output reports each plugin's
installed/not-installed state individually.

Regenerated the `mngr imbue_cloud admin pool create` CLI reference docs to
include the new `--no-recycle` flag (forces a fresh OVH VPS order instead of
reclaiming a cancelled one). Docs-only change to `libs/mngr/docs/`.

Test-only flake mitigations (no production code change):

- Made the Docker `test_pull_image_not_found_raises` integration test resilient to a Docker Hub registry-connectivity flake: when the registry is unreachable (the pull times out before returning its 404), the test now skips instead of failing, while still asserting the clean "image not found" path when the registry is reachable. Also marked it `@pytest.mark.flaky` so offload retries it.
- Marked the tmux integration test `test_start_restart_stopped_agent` `@pytest.mark.flaky` (it occasionally exceeds the 10s pytest-timeout by a few hundred ms under CI load), matching its already-flaky siblings (`test_list`, `test_create`, `test_connect`, `test_destroy`) so offload retries it rather than hard-failing.

- Marked two real-agent integration tests (`test_stop_agent_kills_multi_pane_processes`,
  `test_cleanup_destroy_json_output_with_real_agent`) as `@pytest.mark.flaky` so
  offload retries them. They intermittently exceed the 10s `pytest-timeout` under
  offload load while waiting on a spawned agent process; this matches the
  already-flaky sibling `test_start_restart_stopped_agent`. No production change.

Fixed a batch of breakage in the test suite introduced by a bulk merge:

- Added missing imports and a missing fixture parameter in the e2e tutorial tests (`json`, `re`, `Path`, `Any`, `sys`, and the `temp_git_repo` fixture) that caused F821 errors.
- Fixed a stale `_create_my_task` call signature, removed an unused `json` import, deduplicated stray imports, and deleted a dead duplicate transcript-staging helper in `test_transcript.py`.
- Applied ruff import-sorting (`destroy_test.py`, `start_test.py`) and formatting (14 e2e test files) that the merge left unformatted.
- Regenerated the CLI markdown docs to reflect new options the merge added (`mngr connect --connect-command`, `mngr destroy --dry-run`, and updates to `start`/`stop`/`message`/`snapshot`).
- Rephrased a comment in `test_templates.py` that coincidentally tripped the `exec()` ratchet (the prose read "exec (which ...").
- Replaced `@pytest.mark.flaky` with `@pytest.mark.timeout(30)` on `test_list_command_with_sort_by_name`, the one sibling in the `test_list_command_with_*` family missed by the earlier timeout-flake audit (its teardown latency trips the 10s default under CI load; reruns do not help latency).

No user-facing behavior change.

Added a `docker_runtime` option to the docker provider config. When set (e.g. `docker_runtime = "runsc"`), mngr passes `--runtime=<value>` to `docker run`, letting hosts run agent containers under an alternative runtime such as gVisor. Defaults to unset (no `--runtime` flag, i.e. Docker's default runtime). The named runtime must be registered with the Docker daemon, otherwise container creation fails with Docker's native "unknown runtime" error. Override per-environment with `MNGR__PROVIDERS__<NAME>__DOCKER_RUNTIME` (e.g. `=runc` where gVisor is unavailable, such as CI).

Fixed a shell-quoting bug in agent launch: extra `agent_args` (the arguments passed after `--` on `mngr create`) are now shell-quoted before being spliced into the launch command.

Previously, `BaseAgent.assemble_command` joined `agent_args` into the command string with plain spaces. An argument whose value contained spaces or shell metacharacters -- for example `--model "Gemini 3.5 Flash (Medium)"` -- was emitted raw (`--model Gemini 3.5 Flash (Medium)`), so the shell that evaluates the launch command word-split the value and parsed `(Medium)` as a subshell, failing with `syntax error near unexpected token '('`.

Each `agent_args` element is now passed through `shlex.quote` (via a shared `quote_agent_args` helper, which the `mngr_claude` plugin's own `assemble_command` override also uses so the two cannot drift). `cli_args` are left unquoted, unchanged: string-form `cli_args` configs are split with a quote-preserving (non-POSIX) shlex and so already arrive shell-safe. This fixes every agent type that inherits the base method, including `antigravity`/`agy`.

Added shell tab-completion for the positional arguments of `mngr plugin add` and `mngr plugin remove`.

- `mngr plugin add <TAB>` suggests installable plugin package names (e.g. `imbue-mngr-claude`, `imbue-mngr-modal`) drawn from the plugin catalog -- the same set the `mngr extras` install wizard offers.
- `mngr plugin remove <TAB>` suggests the plugin packages currently installed (from the uv-tool receipt), filtered to packages that actually register `mngr` entry points -- so non-plugin dependencies installed alongside plugins (e.g. workspace libraries) are not offered.

Both support prefix filtering and repeat the completion for each package when operating on several at once.

Test-infrastructure cleanup: the shared mngr plugin test fixtures (HOME
isolation via the autouse `setup_test_mngr_env`, temp host/profile/config dirs,
git-repo helpers, and the shell-stub fixtures `stub_mngr_log_sh` /
`mngr_transcript_lib_sh`) are now single-sourced in
`imbue.mngr.utils.plugin_testing` and exposed through
`register_plugin_test_fixtures`. mngr's own `conftest.py` now registers that
shared set rather than redefining ~20 duplicate fixtures, keeping only two that
still differ for mngr-core: the deliberately-blocking `plugin_manager`, and
`mngr_test_id` (which differs only incidentally -- its `worker_test_ids`
bookkeeping is read solely by mngr-core's `session_cleanup` leak scan, which is
not shared with plugins). The shared `temp_mngr_ctx` now resets the
provider-instance cache on teardown for plugins too, so that behavior no longer
diverges. No user-facing behavior change.

- `plugin_catalog.py`: `UNPUBLISHED_PACKAGES` is now the single source of truth for "deliberately not published to PyPI", consulted by both the install wizard (which never offers an unpublished package) and the release tooling (which auto-discovers every `libs/*` package as a publish candidate and subtracts this set). Added `imbue-mngr-mapreduce` (internal map-reduce framework library, no CLI), `imbue-mngr-claude-subagent-proxy` (experimental, coupled to Claude Code internals), and `skitwright` (e2e-test-only helper) alongside the existing `imbue-mngr-tmr`. No runtime behavior change.

Fixed `ProviderInstanceConfig.merge_with` so a higher-precedence config layer
only overrides the provider fields it actually set. It previously used an
"override wins unless its value is None" rule, which meant any field whose
default is a non-None value (a `bool` defaulting to `False`, an empty tuple,
etc.) was silently reset to that default whenever a higher layer touched the
provider block at all -- even via a single-key override like a create
template's `setting__extend = ["providers.<name>.is_enabled=true"]`.

Concretely, applying `providers.lima.is_enabled=true` (as the minds
forever-claude-template's lima create template does) reset `is_host_in_docker`,
`install_gvisor_runtime`, and `default_container_run_args` back to their
defaults, so the Lima provider silently ran in direct-in-VM mode instead of
docker-in-VM mode. The merge now uses `model_fields_set` (matching
`AgentTypeConfig` / `PluginConfig`), so untouched fields keep their base value.

`mngr create --new-host` now tears down a freshly-created host on *any* failure
up to and including the initial-message send, so a failed create never leaks a
host (or, for non-idle-shutdown providers, its lease). The whole create flow --
host env-var write, on_host_created hooks, post-host-create commands, locking,
provisioning, agent start, and the initial-message delivery -- is now wrapped in
a single continuous teardown guard, closing a gap where failures between the
former two separate guard blocks (and the host env-var write) could leak the
host. The `--edit-message` send, which the CLI performs after the API create
returns, is now likewise covered. The existing
`MNGR_DEBUG_RETAIN_LOCK_FOR_FAILED_HOSTS_DURING_CREATE=1` escape hatch still
retains a failed host for debugging.

- Fix the "collect results from all agents" example in the tutorial. It used `mngr exec "$agent" -- git log --oneline -3`, but `mngr exec` takes the command as a single positional argument (not a kubectl-style `--`-separated argv), so the `--oneline`/`-3` tokens were mis-parsed as agent names and the command always failed. The example now passes the command as a single quoted string: `mngr exec "$agent" "git log --oneline -3"`.

- Fix the `test_advanced_fan_out_create` e2e tutorial test so it runs. The
  fan-out loop creates four local command agents, which invokes `rsync` and
  `tmux` and takes longer than the default per-test timeout, so the test now
  carries `@pytest.mark.rsync`, `@pytest.mark.tmux`, and an extended
  `@pytest.mark.timeout(180)` (plus a larger per-command timeout for the shared
  loop). The superfluous `@pytest.mark.modal` mark was removed because the test
  substitutes local agents and never exercises Modal.
- Strengthen the same test's assertions: instead of only checking the fan-out
  loop exits 0, it now verifies that one agent was created per task (via
  `mngr list --provider local --addrs`) and that the task commands are actually
  running (via `mngr exec ... pgrep`).

Removed the superfluous `@pytest.mark.modal` from the `mngr observe --discovery-only` e2e tutorial test (it never shells out to the `modal` CLI, so the mark was a resource-guard `NEVER_INVOKED` violation) and strengthened it to create a local agent, capture the JSONL discovery stream, and assert the stream is well-formed JSON that reports the agent.

Fixed the `test_advanced_watch_dashboard_running` e2e tutorial test: removed the incorrect `@pytest.mark.modal` mark (the `watch -n 5 mngr list --running` dashboard command only enumerates the local provider when no remote hosts are registered, so it never contacts Modal) and added a verification that the underlying `mngr list --running` query succeeds and returns a well-formed, empty dashboard.

Fixed the `test_advanced_watch_list_live_dashboard` e2e tutorial test. It
carried a spurious `@pytest.mark.modal` mark, but the `watch -n 5 mngr list`
dashboard command it exercises only performs Modal host discovery via the
in-subprocess gRPC SDK, which the resource guard cannot observe (the guard only
tracks the `modal` CLI binary or in-process gRPC). The mark therefore tripped
the guard's "marked modal but never invoked modal" check. Removed the mark and
strengthened the test to create a local agent and assert it appears in the live
dashboard output, rather than only checking that the `watch` wrapper exits.

Strengthened the `mngr archive` e2e tutorial test (`test_archive_command`) to verify
the agent is actually archived: it now stops the agent first (mirroring the tutorial
narrative), runs `mngr archive my-task`, and asserts the `archived_at` label is set and
the agent appears under `mngr list --archived`. Added an unhappy-path test
(`test_archive_running_agent_is_skipped`) verifying that archiving a running agent
without `--force` is a no-op that warns and leaves the agent un-archived.

Fixed the `test_archive_stopped_via_stdin` e2e tutorial test: removed the
incorrect `@pytest.mark.modal` marker (the test only exercises local lifecycle
commands and never invokes the Modal CLI, so the resource guard failed the
otherwise-passing test) and strengthened it to verify the archive's actual
effect (the `archived_at` label is applied, the agent appears under
`mngr list --archived`, and is filtered out of `mngr list --active`).

Fixed the `test_command_agent_dev_server_extra_windows` e2e release test, which
was marked `@pytest.mark.modal` but never ran any command that contacts Modal, so
the resource guard failed it. It now runs `mngr list` (which exercises the Modal
discovery path, matching the sibling create tests) to verify the command agent was
created, and asserts that the extra `logs` tmux window requested via `-w` actually
exists.

Fixed and strengthened the `test_command_agent_python_http` e2e tutorial test
for the "RUNNING NON-AGENT PROCESSES" section. The test created a local
`--type command` agent but was incorrectly marked `@pytest.mark.modal`, so the
resource guard failed it for never invoking Modal; the mark was removed. The
test now also verifies actual behavior: it checks that the managed process is
running inside the agent (`mngr exec ... ps`) and that the agent is listed as a
local command agent with the expected command (`mngr list --provider local
--format json`). No production code changes.

- Strengthen the `mngr config edit --scope project` e2e tutorial test (`test_config_edit_scope`) to verify the actual behavior: it now resolves the project-scoped config path via `mngr config path --scope project`, confirms the edit command announces and targets that exact file, and checks that the file is created from the template on disk. Added a companion unhappy-path test (`test_config_edit_scope_missing_editor`) verifying the command fails cleanly with an "Editor not found" message when `$EDITOR`/`$VISUAL` point at a nonexistent program.

Strengthened the `mngr config edit` e2e tutorial test: it now drives the command
with a fake editor and verifies the editor is invoked on the freshly-created
config file (rather than only checking the exit code). Added a companion test
covering the unhappy path where the editor exits non-zero and the command
propagates the failure.

- Strengthen the `mngr config get` tutorial release test: it now establishes a value with `mngr config set` and reads it back, asserting the returned value is exactly what was set (instead of only checking the exit code). Added a companion test covering the unhappy path where `mngr config get` is asked for a key that has not been set, verifying it exits non-zero with a clear "Key not found" message.

- Fix the `test_config_list_scope` release e2e test, which ran three sequential `mngr config list --scope ...` subprocesses and exceeded the default 10s per-test pytest-timeout (each `mngr` cold-start costs several seconds). Added a `@pytest.mark.timeout(60)` marker, matching how other multi-command e2e tutorial tests handle cumulative subprocess startup time.
- Strengthen the same test to assert each invocation reports the scope it actually read from (`Config from user/project/local`), so a pass confirms `--scope` selects the right config file rather than silently falling back to the merged view.

- Strengthen the `mngr config list` e2e tutorial test: it now persists a known top-level value (`headless`) and asserts that the human-readable output carries both the "Merged configuration (all scopes):" banner and the persisted `headless = true` line, rather than only checking the exit code. Added a companion `test_config_list_json` covering the same tutorial block via `--format json`, asserting the parsed document carries the value under the top-level `config` object.

Strengthened the `mngr config path --scope user` e2e tutorial test to verify the
reported path is actually the user-scope config file (by writing a value at user
scope and confirming it lands in that exact file), and added an unhappy-path test
that asserts an unsupported `--scope` value is rejected.

Strengthened the `mngr config path` e2e tutorial test (`test_config_path`) to verify actual behavior: it now asserts that all three scopes (user, project, local) are reported, that the user/local scopes resolve to `settings.toml` / `settings.local.toml`, and that the `(exists)`/`(not found)` annotation for each scope matches the real on-disk state.

Fixed the e2e tutorial test harness so that `mngr config set` against the
project scope works under pytest. The shared e2e fixture now seeds the
project `settings.toml` with the `is_allowed_in_pytest` opt-in (previously
only `settings.local.toml` opted in, so a project-scope `config set` created
a file that the pytest config guard rejected on the next load), and opts the
harness into the assign-by-default merge semantics
(`allow_settings_key_assignment_narrowing = true`) so a project-scope
`commands.create.*` setting no longer collides with the local-scope
`connect_command` in the command's shared `defaults` map. This unblocks the
`test_config_set_default_provider` and `test_config_set_headless` tutorial
tests.

Strengthened the `mngr config set headless true` tutorial e2e test
(`test_config_set_headless_globally`) to verify the value actually persists to
the project-scope config file (read back directly as a boolean), rather than
only checking the command exits 0. Added a companion unhappy-path test
(`test_config_set_rejects_unknown_key`) that confirms `mngr config set` rejects
an unknown key with a non-zero exit and does not create the config file. No
production behavior change.

Fixed the `test_config_set_headless` e2e release test so it reliably passes: added a `@pytest.mark.timeout(60)` marker (two sequential `mngr` invocations exceeded the default 10s per-test timeout) and seeded the project `settings.toml` with `is_allowed_in_pytest = true` before `mngr config set headless true` writes to it, so the follow-up `mngr config get headless` no longer trips the pytest config opt-in guard.

Strengthened the `mngr config set --scope` e2e tutorial test (`test_config_set_scope`) to verify the actual effect of the command: it now reads the value back from the user scope and confirms the value did not leak into the project scope. Added a companion unhappy-path test (`test_config_set_invalid_scope`) that confirms an unrecognized `--scope` value is rejected and writes nothing.

- Harden the `mngr config set` tutorial e2e test: give it an explicit 60s timeout so a cold-start `mngr` subprocess does not blow the default 10s pytest budget, assert that the value actually lands in the project `settings.toml` (rather than only checking the exit code), and add an unhappy-path test verifying that setting an unknown configuration key is rejected and not persisted.

Removed a superfluous `@pytest.mark.modal` mark from the
`test_connect_by_agent_id_fictional` e2e test. The test connects to a
non-existent agent id and only verifies that mngr parses the id-as-target
syntax and reports a clean "not found" error; it never shells out to the
`modal` CLI (the only Modal chokepoint tracked inside the mngr subprocess),
so the resource guard correctly flagged the mark as never invoked. The test
still runs in the release suite (it is `@pytest.mark.release`), which executes
on Modal infrastructure regardless of the mark. Also strengthened the test's
assertion to confirm the command exits non-zero and the error explicitly names
the missing agent id. No production behavior change.

Fixed the `mngr connect` e2e tutorial tests (`test_connect.py`). The happy-path
tests asserted `mngr connect my-task` succeeds, but the standalone `connect`
command execs `tmux attach` (the `connect_command` override only applies to
`create`/`start`), which aborts with "open terminal failed: not a terminal"
under the plain pipe-based test runner. Added an `E2eSession.run_connect_interactively`
helper that runs the command under a PTY, waits for the tmux client to attach,
and detaches it externally so the command exits cleanly, and switched the
happy-path tests to it (now also asserting the command attaches to the named
agent's session). Removed the superfluous `@pytest.mark.modal` from the
local-only connect tests, which never query modal (the resource guard enforces
this); the id/host error-path tests keep it.

Fixed the `test_connect_explicit_host` e2e tutorial test, which was failing
because it carried `@pytest.mark.tmux`, `@pytest.mark.rsync`, and
`@pytest.mark.modal` resource-guard marks that the command never exercises.
`mngr conn my-task@my-host` fails during host resolution (the host does not
exist) and exits before attaching a tmux session, running rsync, or making any
real Modal call, so the resource guard rejected the superfluous marks. The test
now carries only `@pytest.mark.release`, matching the other unhappy-path connect
test. Also strengthened the assertion to verify the error clearly reports the
missing host rather than only checking the exit code.

Fixed the `test_connect_no_start` e2e tutorial test. It now allows enough time for
the create+connect flow (adds `@pytest.mark.timeout(180)`), drops the superfluous
`@pytest.mark.modal` mark (the local create/connect path never invokes Modal), and
asserts the real behavior of `mngr connect my-task --no-start`: a running agent is
accepted (no auto-start-disabled error) and connect proceeds to a real `tmux attach`,
which fails cleanly with "open terminal failed" in the headless harness. Also
corrected the module docstring, which incorrectly claimed the e2e fixture rewrites
the standalone `connect` command to a no-op recorder.

Fixed the `test_connect_short_form` e2e tutorial test for `mngr conn`.

`mngr connect`/`conn` replaces itself with `tmux attach`, which blocks until the
user detaches and requires a real terminal, so the test could never succeed when
run headlessly (it either hung on a tty or failed with "open terminal failed:
not a terminal"). The e2e session now exposes `run_connecting_command`, which
launches the connect command under a pseudo-terminal in a background subprocess
and verifies that a client actually attaches to the agent's tmux session (the
observable effect of a successful connect). The `@pytest.mark.modal` mark was
also removed because connecting to a local agent never invokes Modal.

`mngr connect` now honors a custom `connect_command` (via the new
`--connect-command` flag or the `connect_command` config), running it instead
of the builtin tmux attach -- matching how `mngr create` and `mngr start`
already behave. A re-entrancy guard (the `MNGR_CONNECT_COMMAND_ACTIVE`
environment variable) prevents infinite recursion when a custom connect command
itself invokes `mngr connect`.

Fixed and expanded the e2e release test for controlling mngr via `MNGR__*`
environment variables (`test_control_mngr_via_env`). The test now opts into
assign-by-default behavior so it works around the e2e fixture's
`[commands.create]` local setting, asserts the agent actually lands on the
`local` provider chosen via the env var, and adds an unhappy-path test verifying
that an invalid provider value supplied via the env var is rejected. No
user-facing behavior change.

- Fix the `test_create_address_syntax_existing_host` tutorial e2e test (`mngr create my-task@my-dev-box`). It now passes an explicit `--type` so the command reaches host resolution (the agent-type default is not configured in the isolated test environment, so without it the command failed earlier on "No agent type provided"), and it verifies the expected "Could not find host" error. Removed the inaccurate `@pytest.mark.modal` mark because resolving a non-existent named host short-circuits before any Modal API call. Also strengthened the test to assert the parsed host name (`my-dev-box`) is echoed in the error.

- Remove the superfluous `@pytest.mark.modal` from the e2e tutorial test `test_create_and_destroy_agent`. The test creates a `--type command` (local) agent and its body never invokes the Modal CLI, so the resource guard correctly flagged the mark as never-invoked (the only Modal touch is the fixture's teardown environment cleanup, which runs after the call-phase guard check). The test still carries the `rsync` and `tmux` marks it genuinely exercises.

- Test-only change (no user-visible behavior change): removed a stale `@pytest.mark.modal` from the `mngr rename` e2e release test, which created only a local command agent and never actually invoked Modal (the mark began failing once the resource-guard's must-use enforcement was re-enabled). Also added an e2e release test covering `mngr rename --dry-run`, asserting it previews the rename without mutating the agent's name.

Updated the "clone" creation tutorial block to use the current `mngr create --transfer=git-mirror` flag instead of the removed `--clone` flag, which created a full git clone (an independent copy of the repo with its own working directory and history) rather than a worktree. The corresponding e2e release test (`test_create_clone`) now runs the updated command and verifies the agent runs in a separate working directory that is a real git clone (its own `.git` directory rather than a worktree's `.git` file).

- Fix the `test_create_codex_agent` e2e tutorial test so its scaffolding `mngr config set agent_types.codex.command 'sleep 99999'` writes to the `local` config scope (`settings.local.toml`) instead of the default `project` scope. The e2e fixture only opts `settings.local.toml` into the pytest run (`is_allowed_in_pytest = true`); writing a fresh `project` `settings.toml` produced a config file without that flag, so the subsequent `mngr create` refused to load it under the pytest config guard. Also strengthen the test to assert the created codex agent's resolved command matches the configured `sleep 99999`.

Fixed the `test_create_codex_explicit_type` release e2e test for the codex agent-type tutorial block: the test now configures the codex command in the local config scope (whose `settings.local.toml` opts into the pytest config guard) instead of the project scope, and dropped the superfluous `@pytest.mark.modal` mark since a local `--no-connect` create never invokes Modal. The test also now verifies via `mngr list --format json` that the created agent has type `codex` and is running, rather than only checking the exit code.

Removed the inapplicable `@pytest.mark.modal` mark from the release e2e test
`test_create_command_agent_runs_post_dash_command_in_agent`. The test creates a
`command`-type agent on the default (local) provider and never invokes Modal in
any way the resource guard can observe, so the mark caused a spurious
NEVER_INVOKED guard failure. The test still exercises the documented `mngr create
--type command -- <command>` behavior and verifies the command actually runs in
the agent.

- Tighten the `test_create_command_custom_script` e2e tutorial test. Dropped the superfluous `@pytest.mark.modal` mark (the local command-agent it creates never exercises Modal, which the resource guard now flags as a `NEVER_INVOKED` violation), and strengthened it to verify that the custom command is actually forwarded to a running `command`-type agent (via `mngr list --format json`) rather than only asserting that `mngr create` exits 0.

Fixed the `test_create_command_python_http` e2e tutorial test: it was marked
`@pytest.mark.modal` but only creates a local `command`-type agent, so it never
invoked Modal and failed the resource guard. Removed the spurious mark and added
verification that the agent is actually created with the expected command and is
running inside the agent.

- Fixed the BASIC CREATION tutorial: the "create a copy instead of a worktree" example used the removed `--copy` flag. It now uses `mngr create my-task --transfer=git-mirror`, matching the current `--transfer` interface (a plain rsync copy is still the default for non-git projects).

- Add an e2e release test (`test_create_default_branch_distinct_per_agent`) covering the tutorial's claim that `mngr create` creates a *separate* default branch *per agent*: it creates two agents and verifies each lands on its own distinct `mngr/<name>` branch (worktree HEAD checked via `mngr exec`), with both branches starting from the same base commit.

Removed the superfluous `@pytest.mark.modal` from the `test_create_default` e2e
tutorial test (it creates a local-provider agent and never invokes Modal, so the
resource guard flagged the mark as never-invoked). Strengthened the test to
verify the agent is actually running inside its worktree by execing `pwd` in the
agent and comparing it against the reported `work_dir`.

Fixed the tutorial's Docker resource-limit example: `mngr create my-task
--provider docker -s cpus=2` used a bare `cpus=2` token, which `docker run`
would have interpreted as the image name rather than a CPU limit. The example
now uses the correct `--cpus=2` flag. Also strengthened the corresponding e2e
release test to specify an agent type and to verify the CPU limit is actually
applied inside the created container.

Fixed the `test_create_docker_custom_dockerfile` e2e release test, which had been
failing since the `mngr create` agent-type default was moved into user config
(an explicit `--type` is now required). The test now passes `--type command` and
builds the custom image from `debian:bookworm-slim` (which provides `apt-get` for
the required host packages) instead of `alpine`, and verifies the custom Dockerfile
was actually used by reading back a marker file baked into the image.

- Fixed the `test_create_docker_default_image` e2e tutorial test: the isolated test profile has no configured default agent type, so the bare `mngr create my-task --provider docker` it ran failed with "No agent type provided". The test now passes `--type command -- sleep <N>` explicitly (matching the basic-creation tutorial tests) and additionally verifies the container is running mngr's default image (`debian:bookworm-slim`) by reading `/etc/os-release` via `mngr exec`.

- Fix the `test_create_docker_start_args_overview` e2e release test so the `mngr create --provider docker` command supplies an agent type (`--type command`), which the isolated test environment does not configure as a default. The test now also reads the container hostname back to confirm the `-s` start arg is forwarded to `docker run`, rather than only checking the create command's exit code.

Fixed the `test_create_docker_start_args` e2e tutorial test: the `mngr create`
invocation now passes `--type command -- sleep ...` so that the isolated test
environment (which has no default agent type configured) can create the agent
and keep the container alive for the follow-up `mngr exec my-task hostname`
assertion that verifies the `-s "--hostname=..."` start arg was forwarded to
`docker run`.

Fixed the `test_create_docker_volume_start_arg` e2e tutorial test, which was
failing because `mngr create` requires an agent type and the test supplied
none. The test now uses the standard `--type command -- sleep <N>` stand-in
(matching the other tutorial create tests) and verifies that the `-v` start arg
actually bind-mounts the host directory into the container.

Strengthened the e2e test for duplicate agent names (`mngr create` with an
already-used name). It now asserts the failure is specifically the
duplicate-name rejection ("already exists") and verifies the rejected duplicate
leaves the original agent untouched -- exactly one agent of that name remains,
still running its original command rather than the duplicate's.

- Remove the superfluous `@pytest.mark.modal` from the `test_create_from_another_agent` e2e tutorial test. The test only creates local command agents and clones one from another via rsync; it never invokes Modal through the bare `modal` CLI (the only path the resource guard can observe -- exercised solely during Modal *host* creation). `mngr list` enumerates the Modal provider through the in-process-untrackable SDK, so the mark could never be satisfied and the resource guard failed the test with "marked with @pytest.mark.modal but never invoked modal". The mark has no effect on CI test selection (release offload jobs filter by `release`, not `modal`), so dropping it does not change where the test runs.
- Strengthen `test_create_from_another_agent` to verify the core behavior of `--from <agent>`: it now writes a marker file into the source agent's work directory before cloning and asserts the cloned agent received it, confirming the source agent's directory contents are actually copied (previously the test only checked that the two agents had distinct work dirs on the same host and that the clone got its own branch).

- Fix the release e2e test `test_create_git_mirror_with_existing_branch` so it passes again. The test only ever creates a local-provider agent (via `--transfer=git-mirror --branch <current>`), so it never invokes Modal, but it was marked `@pytest.mark.modal`. Since provider discovery now skips the Modal backend when no Modal environment exists (`ProviderEmptyError`), `mngr list` makes no Modal call and the resource guard failed the test with "marked with @pytest.mark.modal but never invoked modal". Removed the stale mark. Also strengthened the test to assert that omitting the `:NEW` part creates no new `mngr/*` branch in either the source repo or the agent's git mirror. No production behavior change.

- Remove the superfluous `@pytest.mark.modal` from the `test_create_headless` e2e release test. The test creates a local command agent and only runs `mngr list`, which never invokes the Modal CLI (the resource guard only tracks Modal CLI invocations in subprocesses, and `mngr list` discovery does not create a Modal environment), so the resource guard correctly flagged the mark as never-invoked.
- Strengthen `test_create_headless` to verify the headless agent is actually running by exec-ing into its host, instead of only checking that it appears in `mngr list` output.

Fixed the `test_create_headless` tutorial e2e test (`mngr create --headless`): added a per-test timeout matching the other agent-creating e2e tests so the real create plus `mngr list` no longer trips the global 10s default, and removed the superfluous `@pytest.mark.modal` mark since the test creates a local agent and never invokes Modal. Also strengthened the test to verify the headless agent is actually running and reachable via `mngr exec`.

Strengthened the `mngr create --help` e2e tests: the help command now also asserts that stderr is empty (no stray warnings or deprecation notices), and a new test verifies the abbreviated forms advertised in the help's own SYNOPSIS -- the `-h` short flag and the `c` alias -- both produce the create help output.

Added an unhappy-path e2e test (`test_create_rejects_unknown_option`) covering the same `mngr create` tutorial block: it verifies that an unknown option is rejected with exit code 2, an empty stdout, and a usage error on stderr.

Fixed the `test_create_in_place_alias_target` e2e release test so it passes: added an explicit `@pytest.mark.timeout(120)` (the test runs four sequential `mngr` invocations and was exceeding the global 10s `func_only` timeout), and removed the superfluous `@pytest.mark.modal` mark (the test is local-only and never invokes Modal, which the resource guard correctly flagged).

Fixed the `test_create_in_place` release e2e test: added an explicit
`@pytest.mark.timeout(120)` so the test's multiple serial `mngr` subprocess
invocations are not killed by the default 10s per-test timeout, and removed the
superfluous `@pytest.mark.modal` mark (the in-place `--transfer=none` flow runs
entirely on the local provider and never invokes Modal). Also strengthened the
test to confirm at runtime, via `mngr exec my-task pwd`, that the agent process
actually runs in the source directory rather than relying on `mngr list`
metadata alone.

Fixed a regression where the first `mngr create --provider modal` for a brand-new
Modal environment failed with "Provider 'modal' has no state yet" instead of
bootstrapping the environment. The create flow now resolves the new-host provider
with `is_for_host_creation=True`, allowing the per-user Modal environment to be
created on first use.

Strengthened the `test_create_modal_idle_mode_run` e2e test to verify the
concrete effect of the create (a command-type agent that runs the requested
command with run idle mode and a 60s timeout) instead of only checking the exit
code, and marked it flaky so offload retries transient remote Modal create
failures.

- Fix `mngr create --provider modal` (and any other backend with one-time per-user bootstrap) so the very first create in a fresh Modal environment no longer fails with `Provider 'modal' has no state yet`. The create path resolves the new-host provider for failure-teardown before provisioning; that lookup was constructed without `is_for_host_creation=True`, so on a not-yet-existing Modal environment it eagerly raised `ProviderEmptyError` instead of letting the create bootstrap the environment. It now passes `is_for_host_creation=True`, matching the subsequent host-creation lookup.

- Fixed the `test_create_modal_pass_host_env` e2e tutorial test so it actually runs: the isolated test profile configures no default agent type, so the bare `mngr create my-task --provider modal --pass-host-env MY_VAR` command failed with "No agent type provided". The test now passes `--type command -- sleep ...` (matching the convention used by the other create tests) and additionally verifies, via `mngr exec`, that the forwarded `MY_VAR` host env var actually reaches the host. No production behavior change.

- Fix the `test_create_named_agent` e2e release test so it passes: bump its per-test timeout to 120s (the cumulative `create` + `list` + `exec` work over the full provisioning path exceeds the default 10s func-only timeout, especially when the ttyd download is slow), and drop the spurious `@pytest.mark.modal` mark. The test creates a purely local agent (`mngr create my-task`) and never invokes Modal in a guard-tracked way during the call phase, so the mark tripped the resource-guard NEVER_INVOKED check once the body completed. This matches the 48 other local-only e2e tests that already omit the Modal mark.
- Strengthen `test_create_named_agent` to assert that `mngr exec my-task pwd` output is rooted in the agent's dedicated worktree (the unique `my-task-<hash>` directory reported by `mngr list`), rather than only checking that the command succeeded.

Fixed the `test_create_short_forms` e2e tutorial test (BASIC CREATION). It now
carries an explicit `@pytest.mark.timeout(120)` because it issues two `mngr
create` commands, whose combined function-body time exceeds the global 10s
pytest-timeout default. Removed its `@pytest.mark.modal` mark: the test only
creates local (`--type command`) agents and runs `mngr list`, which reaches
Modal exclusively via the in-process gRPC SDK inside the spawned `mngr`
subprocess -- a path the resource guard cannot track -- so the mark tripped the
guard's NEVER_INVOKED check. No user-facing behavior changes.

- Fix the `test_create_stack_templates` e2e tutorial test so it actually runs: add the missing `@pytest.mark.tmux` and a `@pytest.mark.timeout(120)` (a real local `mngr create` exceeds the 10s default), drop the inaccurate `@pytest.mark.modal` (the locally-substituted template never touches Modal), and correct the `with-tests` template stub (a stray trailing comma had been redirecting its body into a `settings.local.toml,` file, and `no_ensure_clean` is not a valid template field -- it now writes `ensure_clean = false`). Also add an unhappy-path test verifying that stacking an undefined `--template` name fails fast with a clear "not found" error.

Fixed the `test_create_template_short_form` e2e tutorial test: it now carries `@pytest.mark.tmux` (command-type agents start via tmux) and no longer carries the spurious `@pytest.mark.modal` (the locally-substituted template runs entirely on the local provider and never invokes modal). Also strengthened the test to verify the template actually took effect by confirming the agent runs in-place.

- Remove the superfluous `@pytest.mark.modal` mark from the `test_create_with_agent_args` e2e tutorial test. The test creates a purely local agent and runs `mngr list`, which only makes Modal gRPC calls when a Modal environment already exists; for a local-only test no Modal call is ever made, so the resource guard correctly flagged the mark as never-invoked.
- Add a complementary unhappy-path e2e test verifying that agent-targeted flags (e.g. `--model opus`) must be separated with `--`, and that omitting the separator makes `mngr create` reject the flag as an unknown option.

Strengthened the e2e tutorial test for `mngr create --branch BASE:NEW`: the happy-path test now also verifies (via `git worktree list`) that the agent's worktree is actually checked out on the new branch at the base commit, and a new unhappy-path test confirms that creating an agent with a nonexistent base branch fails cleanly without leaving a dangling agent branch.

Fixed the `test_create_with_connect_command` release e2e test (custom `--connect-command` on `mngr create`). Added an explicit `@pytest.mark.timeout(120)` (the default 10s per-test budget was exceeded by the create + list commands) and removed the stale `@pytest.mark.modal` mark: the test uses the default local provider and `mngr list` never shells out to the guarded `modal` CLI, so the mark tripped the resource guard's never-invoked check once the test ran to completion.

Strengthened the `test_create_with_custom_branch_pattern` e2e test to also verify that a `--branch ":feature/*"` pattern (with an omitted BASE) creates the new branch off the current branch, by asserting `feature/my-task` points at the same commit as `HEAD`.

Strengthened the e2e test `test_create_with_dirty_tree_fails` so it actually exercises the dirty-working-tree guard: it now supplies a concrete agent type (previously the create aborted on a missing default type, so the test passed for the wrong reason) and asserts that the failure message specifically mentions the uncommitted changes and the `--no-ensure-clean` escape hatch.

- Fix the `test_create_with_env_file` e2e tutorial test: a plain local command-agent create never exercises cross-provider discovery, so the `@pytest.mark.modal` mark was superfluous and the resource guard failed the otherwise-passing test. Dropped the mark (matching the equivalent local-only test) and strengthened the test to assert that the `--env-file` variable is actually loaded into the agent's on-disk environment, rather than only checking that the command exits cleanly.

Added a `@pytest.mark.timeout(120)` marker to the `test_create_with_env` e2e tutorial test so it no longer hits the global 10s pytest timeout when run directly (the heavier e2e tests already carry explicit timeout markers). Also strengthened the test to assert that `--env` persists the variable into the agent's on-disk env file, in addition to the existing tmux-pane check.

Strengthened the `test_create_with_explicit_branch_name` e2e tutorial test: it now asserts that an explicit `--branch ":feature/my-task"` name is taken literally (no default `mngr/my-task` branch is created) and that the new branch starts from the current branch's commit.

Fixed the `test_create_with_extra_tmux_windows` e2e release test: it now overrides
the default 10s function timeout (a real local create plus `mngr list` can exceed
it, especially when `ttyd` is being installed) and no longer carries a stale
`@pytest.mark.modal`. Since `mngr list` stopped auto-creating the per-user Modal
environment for read-only commands, this purely local-provider test never invokes
Modal, so the resource guard was correctly flagging the mark as never-invoked.
The test also now exercises two named extra tmux windows (`server` and `logs`),
matching the tutorial more faithfully.

- Fix the `test_create_with_json_output` e2e tutorial test (BASIC CREATION section): remove the incorrect `@pytest.mark.modal` marker (the test uses the local provider and never exercises the Modal CLI, so the resource guard failed it with a "never invoked modal" violation) and add `@pytest.mark.timeout(120)` (the create-plus-list body exceeds the global 10s default and was being killed mid-create).
- Add `test_create_with_quiet_output`, a sibling e2e test sharing the same output-format tutorial block, that verifies `mngr create --quiet` emits no stdout while still creating the agent (covering the previously-undocumented "--quiet suppresses all output" line of the block).

Fixed the `test_create_with_json_output` e2e release test: added the missing `@pytest.mark.timeout(120)` (matching the other modal-backed e2e tests) and removed the superfluous `@pytest.mark.modal` mark (the test creates a local command agent and only runs `mngr list`, neither of which invokes the guard-tracked `modal` CLI). Also strengthened its assertions to parse and cross-check the `--format json` output of both `mngr create` and `mngr list`.

Fixed the `test_create_with_label` e2e tutorial test: gave it a 120s timeout
(it creates a real command agent and then runs `mngr list`, which exceeds the
default 10s budget) and removed the incorrect `@pytest.mark.modal` mark (the
test only creates a local `--type command` agent and never exercises Modal).

Added `test_create_with_invalid_label_format`, an unhappy-path test sharing the
same tutorial block, which verifies that `mngr create --label <no-equals>` is
rejected with a `KEY=VALUE` error and creates no agent.

Fixed the `test_create_with_label` e2e release test. It now raises the pytest
timeout (the local `mngr create --type command` routinely exceeds the global
10s default) and drops the superfluous `@pytest.mark.modal` mark, since the
test performs a purely local create whose Modal discovery never reaches the
resource guard's subprocess-tracked `modal` CLI path.

Fixed the `test_create_with_message` e2e release test (tutorial `mngr create --message` block). It was hitting the default 10s function timeout during `mngr list` and then failing the resource guard's NEVER_INVOKED check for `@pytest.mark.modal`. Gave it a `@pytest.mark.timeout(120)` (matching the sibling idle-timeout test) and dropped the `@pytest.mark.modal` mark, since the test creates a local command agent and only touches Modal via `mngr list`'s in-subprocess SDK discovery, which the guard cannot observe.

Fixed the `test_create_with_multiple_labels` e2e tutorial test: raised its per-test timeout so the local `mngr create` has time to complete, dropped the superfluous `@pytest.mark.modal` mark (the local create never invokes Modal), and strengthened it to verify that both labels are actually applied to the created agent via `mngr list --format json`.

Fixed the `test_create_with_pass_env` e2e tutorial test, which was failing
because it carried a superfluous `@pytest.mark.modal` mark while only creating
a local command agent (the tutorial block uses the default local provider, so
the test never invoked Modal). Removed the unused mark, then strengthened the
test to verify that the forwarded `--pass-env` variable is actually present in
the created agent's environment via `mngr exec`.

Fixed the `test_create_with_pass_env` e2e tutorial test, which was incorrectly
marked `@pytest.mark.modal` despite never exercising the modal provider (it
creates a local `--type command` agent). The modal resource guard failed the
test during teardown; removing the mark fixes it.

Added a companion unhappy-path test, `test_create_with_pass_env_unset`, that
verifies `mngr create --pass-env` silently skips a variable that is not set in
the current shell: the agent is still created and the variable is absent from
its environment.

- Add a happy-path e2e tutorial test (`test_create_with_real_plugin_flags`) for the `mngr create --plugin/--disable-plugin` tutorial block. The existing `test_create_with_plugin_flags` only exercises the unhappy path (non-existent plugin names, where the strict `--disable-plugin` validation fails before `--plugin` is ever applied); the new test passes *real* registered plugin names so both flags take effect and verifies the agent is actually created and running.

Fixed the `test_create_with_project_label` e2e tutorial test, which was marked
`@pytest.mark.modal` but never invoked the modal binary (provider discovery uses
the modal Python SDK, not the guarded binary), causing the resource guard to fail
it. Removed the superfluous mark and added a companion `test_create_default_project_label`
that verifies the default `project` label is derived from the git repo folder name
when `--project` is omitted.

Fixed a regression where `mngr create --provider modal` (and any other backend
with one-time bootstrap resources) failed against a brand-new Modal environment
with `Provider 'modal' has no state yet: Modal environment ... does not exist
yet`. The create path eagerly loaded the provider for failure-teardown using a
read-only construction (`is_for_host_creation=False`), which refused to
bootstrap the environment before the actual host-creation step could create it.
It now loads with `is_for_host_creation=True`, matching the create intent (the
instance is cached, so no second provider is built).

Also tightened the `test_create_with_snapshot_fictional` release test: it now
runs the full `mngr create --provider modal --snapshot snap-123abc` flow with an
explicit agent type, verifies the bad snapshot id is actually handed to Modal
and rejected, and asserts the failure is a clean single-line error with no raw
Python traceback.

Fixed the `test_create_with_source_path_no_git` e2e tutorial test: removed the superfluous `@pytest.mark.modal` mark (the test only creates a local agent and never invokes Modal, which tripped the resource-guard "marked modal but never invoked" check) and added an explicit `@pytest.mark.timeout(120)` so the create/list/exec sequence is not killed by the default 10s per-test timeout.

Removed the inapplicable `@pytest.mark.modal` mark from the e2e tutorial test
`test_create_with_source_path`. The test creates a local agent via
`mngr create --from <path>`; its only modal contact is the incidental
discovery `mngr list` performs via the in-process modal SDK inside the `mngr`
subprocess, which the resource guard cannot observe. The mark therefore always
failed the guard's "marked modal but never invoked modal" check.

Added an explicit `@pytest.mark.timeout(120)` to the `test_create_with_template_modal_disabled` e2e release test so the `mngr create` subprocess it spawns is not killed by the tight global 10s pytest timeout on a cold start. This matches the convention already used by the other subprocess-spawning e2e tests (e.g. in `test_create_modal.py` and `test_create_commands.py`). No user-facing behavior change.

Fixed the `test_create_with_template` e2e release test. The test creates a
local in-place agent (via a `transfer = "none"` create template), so it does
not exercise the Modal provider: removed the superfluous `@pytest.mark.modal`
(which the resource guard correctly flagged as never invoked) and scoped its
verification `mngr list` to `--provider local` so it no longer fans out to the
slow, network-bound Modal provider. Also strengthened the test to confirm the
agent's actual runtime working directory via `mngr exec my-task pwd`, not just
the `mngr list` metadata. No production behavior change.

Removed the incorrect `@pytest.mark.modal` mark from the `test_create_with_transfer_none` e2e tutorial test. The test creates a purely local in-place agent (`--transfer=none`), so no Modal environment is ever created and `mngr list` correctly skips Modal discovery; the resource guard failed the test for declaring a Modal dependency it never exercises. Also strengthened the test to verify the agent's actual runtime working directory via `mngr exec`.

- Fix the `test_default_output_human_readable` e2e tutorial test: remove the superfluous `@pytest.mark.modal` mark (running `mngr ls` against an empty, isolated test environment makes no Modal API call, so the resource guard correctly rejected the mark) and strengthen its assertions to confirm the default `mngr ls` output is human-readable (`No agents found`) rather than the machine-readable `[]` that `--format json` would emit.

Fixed the tutorial's "destroy all docker agents" one-liner: it now pipes agent
ids into `mngr destroy -f -` (with the `-` stdin placeholder) instead of
`mngr destroy -f`. Without `-`, `mngr destroy` does not read from stdin and
fails with "Must specify at least one agent". This matches every other
filter-into-stdin example in the tutorial (e.g. `mngr list --ids | mngr stop -`).

Also hardened the corresponding release test to create a real Docker agent and
verify it is actually destroyed by the command, rather than only running the
command against an empty agent list.

Fixed the "destroy all Modal agents" tutorial example, which piped agent ids into `mngr destroy -f` without the `-` stdin placeholder. Like every other stdin-consuming command (`exec`, `stop`, `start`, `message`), `destroy` only reads piped input when given an explicit `-`, so the documented command failed with "Must specify at least one agent (use '-' to read from stdin)". The example (and its release test) now read `mngr list --include 'host.provider == "modal"' --ids | mngr destroy -f -`.

Strengthened the `test_destroy_all_modal_agents` release test to create a real Modal agent, confirm it is listed, run the destroy-all command, and verify the agent is gone -- so the test exercises Modal and validates the destroy behavior instead of running against an empty environment.

Added a `@pytest.mark.timeout(120)` marker to the `test_destroy_all_via_stdin`
e2e tutorial test. The test creates two command agents plus several list/destroy
commands, which exceeds the default 10s per-test timeout (it previously timed out
during the second `mngr create`). The new marker matches the convention used by
other multi-step destroy tests (e.g. `test_create_and_destroy_agent`, which uses
`@pytest.mark.timeout(60)` for a single create+destroy).

Fixed the `test_destroy_by_session_name` e2e test, which was failing because it
was marked `@pytest.mark.modal` even though the command under test
(`mngr destroy --session my-session-name`) fails fast on input validation and
never provisions a modal-backed agent. Removed the spurious mark and added a
happy-path test that creates a real agent and destroys it via its derived tmux
session name, verifying the agent is actually gone.

Corrected the "DESTROYING AGENTS" section of the mega tutorial: the
`mngr list --ids | mngr destroy - --dry-run` example referenced a `--dry-run`
flag that was removed from `mngr destroy` (and the other multi-target commands).
The tutorial now shows how to preview what would be destroyed by running
`mngr destroy my-task` without `--force` and answering "no" at the confirmation
prompt, which lists the targets without destroying anything.

Also fixed the corresponding e2e tutorial test (`test_destroy_dry_run`) so it
exercises this confirmation-preview behavior and gives the agent-creation step a
realistic timeout.

- Add a `--dry-run` flag to `mngr destroy`. It previews exactly which agents would be destroyed (honoring the same `--format`, JSON, and JSONL output options as a real destroy) and then exits without touching anything -- no agents, hosts, branches, or garbage collection. This matches the behavior already documented in the tutorial (e.g. `mngr list --ids | mngr destroy - --dry-run`) and mirrors the existing `--dry-run` on `mngr archive` and `mngr cleanup`.

Fixed the `test_destroy_multiple_at_once` e2e release test, which destroys
multiple agents in a single `mngr destroy agent-1 agent-2 agent-3 --force`
command. The test was hitting the global 10s pytest timeout (it creates three
agents and destroys them), so it now carries an explicit `@pytest.mark.timeout(120)`.
The misapplied `@pytest.mark.modal` mark was removed because the test exercises
only local command agents and never invokes Modal. Also strengthened the test
to verify all three agents exist before the destroy, that each is reported as
destroyed, and that none remain afterward. No mngr behavior change.

- Fix the `test_destroy_no_gc` e2e tutorial test, which failed the resource guard with "marked with @pytest.mark.modal but never invoked modal". By design `mngr destroy --no-gc` skips the post-destroy garbage-collection pass, which is the only step that reaches Modal, so the test cannot invoke Modal. Removed the incorrect `@pytest.mark.modal` mark and rewrote the test to actually exercise the flag: it now creates an agent, destroys it with `--no-gc --force`, and verifies the agent is gone and that the "Garbage collecting..." progress output is absent (proving gc was disabled), instead of merely checking that the bare command errors out.

- Fixed the `test_destroy_remove_branch` tutorial e2e test (`mngr destroy --force --remove-created-branch`). It now carries `@pytest.mark.timeout(60)` because destroying an agent and running the default garbage collection exceeds the 10s default pytest timeout, and the spurious `@pytest.mark.modal` mark was removed since the test creates a purely-local command agent and never invokes Modal (the resource guard flagged the unused mark). Also strengthened the test to verify the actual effect: the agent's `mngr/my-task` branch exists after create, the destroy output reports `Deleted branch: mngr/my-task`, and the branch is gone afterward.

- Fixed the `test_destroy_remove_created_branch_inline` e2e tutorial test (WORKING WITH GIT section): a create-then-destroy cycle needs more than the default 10s per-test timeout, so the test now carries `@pytest.mark.timeout(60)` to match the sibling create+destroy tests. Also strengthened the test to verify the actual effect of `mngr destroy --remove-created-branch`: it now confirms the agent's `mngr/my-task` branch exists beforehand, that destroy reports deleting it, and that the branch is genuinely gone from the repo afterward.

Fixed the `test_destroy_short_form` e2e release test, which was timing out under the global 10s default because creating and destroying a real agent takes longer. Added a `@pytest.mark.timeout(120)` override (matching the other agent-creating destroy tests) and strengthened the test to verify that `mngr rm` actually removes the agent, plus added coverage for the safe-by-default behavior where a non-forced `mngr rm` refuses to destroy a still-running agent.

Fixed the `test_destroy_specific` e2e tutorial test so the documented `mngr destroy my-task` command is actually exercised: the agent is now stopped before the bare (non-`--force`) destroy so it is genuinely destroyed instead of being skipped by the running-agent guard, an explicit per-test timeout was added (the destroy + gc exceeds the 10s default), and the test now verifies the agent is really gone via `mngr list`. Removed the superfluous `@pytest.mark.modal` mark: the test creates a localhost agent, which never invokes Modal (only remote-host provisioning does), so the mark could never be satisfied.

Fixed the `test_destroy_with_gc` e2e tutorial test: added a `@pytest.mark.timeout(120)` marker (the global 10s default was too short for the create + destroy + gc flow over the test harness) and removed a superfluous `@pytest.mark.modal` mark (the test exercises a local command agent, so gc never makes a Modal network call and the resource guard flagged the unused mark). Also strengthened the assertions to verify gc actually ran and the agent is gone.

- Remove the superfluous `@pytest.mark.modal` from the `test_env_var_mngr_headless` e2e tutorial test. The test only runs `mngr list` (with no agents) and `mngr config get headless`, which never invoke the Modal provider, so the resource guard failed the otherwise-passing test as a superfluous-mark violation.

Fixed the `mngr event` tutorial e2e tests (`test_event.py`) so they run
reliably. Each test now carries an explicit `@pytest.mark.timeout(120)` (they
previously inherited the 10s default, which is too short for a release e2e test
that creates an agent and reads its events) and passes generous subprocess
timeouts to the `mngr create`/`mngr event` calls. Removed the superfluous
`@pytest.mark.modal` mark: these tests use the default (local) provider, which
never invokes Modal, so the resource guard rejected the unused mark. The tests
keep the `tmux` and `rsync` marks, which the local agent creation genuinely
exercises. No production behavior change.

Also strengthened `test_event_default` to verify the tutorial's documented
contract that `mngr event` emits clean JSONL: every line on stdout must parse
as a JSON object (catching warnings or log lines leaking into the jq-able
stream), and any events present must carry the four guaranteed fields
(`event_id`, `timestamp`, `source`, `type`).

- Fix the `test_event_head` e2e tutorial test (`mngr event my-task --head 10`). It carried a superfluous `@pytest.mark.modal` mark even though creating a local command agent and reading its events never invokes modal, which the resource-guard check flagged as a violation. Removed the mark and added `@pytest.mark.timeout(120)` so the test does not trip the default 10s pytest-timeout while a local agent is created (rsync + tmux) and its events are read. Also strengthened the test to assert that `--head 10` returns at most 10 JSONL events, each carrying the four guaranteed fields (`event_id`, `timestamp`, `source`, `type`), and added an unhappy-path test (`test_event_head_conflicts_with_tail`) verifying that combining `--head` and `--tail` fails with "Cannot specify both --head and --tail".

Fixed the `test_event_tail` tutorial e2e test (`mngr event my-task --tail 20`).

- Added a `@pytest.mark.timeout(120)` override so the test is not killed by the
  default 10s per-test timeout while it creates a local command agent and reads
  its events.
- Removed the inapplicable `@pytest.mark.modal` mark: `mngr event <agent>`
  resolves the agent via the discovery event-stream optimization, which narrows
  discovery to the agent's (local) provider and never invokes Modal, so the
  resource guard flagged the mark as never exercised.
- Strengthened the assertions to verify the documented JSONL contract: every
  emitted line is a JSON object carrying the guaranteed `event_id`, `timestamp`,
  `source`, and `type` fields, and `--tail 20` yields at most 20 events.

- Fixed the `test_exec_all_git_status` tutorial e2e test (WORKING WITH GIT section, `mngr list --ids | mngr exec - "git status --short"`). It was inheriting the 10s default pytest timeout because `test_git.py` was the only e2e tutorial file missing per-test `@pytest.mark.timeout` markers, so it timed out before the piped command finished. Added `@pytest.mark.timeout(120)` and removed the superfluous `@pytest.mark.modal` mark: the test operates entirely on a local agent and never invokes the Modal CLI, which is the only Modal signal the resource guard can observe across the mngr subprocess boundary. Also strengthened the assertion to verify the fan-out actually reaches the agent (not merely that the command exits 0).

- Fix the `test_exec_as_other_user` e2e tutorial test (covering the `mngr exec <agent> "sudo -u other-user ..."` block) so it passes: add a `@pytest.mark.timeout(120)` override (the global 10s pytest timeout killed the test mid-`mngr exec`) and remove the incorrect `@pytest.mark.modal` mark (the test creates a local command agent and never invokes Modal, which tripped the resource-guard "marked modal but never invoked modal" check). Also strengthen the assertion to verify `mngr exec` streams back the executed command's real stdout (a numeric uid line) rather than only checking the exit code.

Removed the superfluous `@pytest.mark.modal` mark from the `test_exec_basic`
e2e tutorial test. The test creates a single local command agent and runs
`mngr exec` against it by name, a path that never invokes Modal, so the
resource guard failed the test for declaring a Modal dependency it did not
exercise. The `rsync` and `tmux` marks remain because local `mngr create`
genuinely invokes both.

Also strengthened the assertion to verify that `mngr exec` forwards the
command's stdout back from the agent's host (checking for the leading
"total" line of `ls -la`), rather than only checking the exit code.

Fixed the `test_exec_branch_show_current` e2e tutorial test (WORKING WITH GIT
section). It was hitting the default 10s pytest timeout during agent creation and
carried a superfluous `@pytest.mark.modal` mark even though it only exercises a
local command agent. Added `@pytest.mark.timeout(60)` and removed the modal mark,
and strengthened the assertion to verify the agent reports its own
`mngr/{agent_name}` branch.

- Fix the `test_exec_cwd` tutorial e2e test: it created a local `command` agent (which never touches Modal) yet carried `@pytest.mark.modal`, so the resource guard failed it with "marked with @pytest.mark.modal but never invoked modal", and it lacked a `@pytest.mark.timeout` override so it hit the 10s default timeout while creating the agent and running exec. Removed the superfluous `modal` mark and added `@pytest.mark.timeout(300)`.
- Strengthen the same test's assertions: verify `mngr exec --cwd /tmp "pwd"` prints exactly `/tmp` (line-anchored), and add a contrasting run without `--cwd` confirming the command defaults to the agent's work_dir rather than `/tmp`.

Fixed the `test_exec_force_commit` git tutorial e2e test: removed the superfluous `@pytest.mark.modal` mark (the test creates a local command agent and runs `mngr exec`, which never invokes Modal, so the resource guard failed it). Strengthened the test to verify the actual force-commit behavior by creating an uncommitted change in the agent, committing it via `mngr exec`, and asserting the commit message lands in the agent's git log and the change is no longer reported as uncommitted.

Fixed the `test_exec_git_log` tutorial e2e test: removed the superfluous
`@pytest.mark.modal` marker (the test exercises a local command agent and
never invokes Modal, which the resource guard flags), and strengthened its
assertions to verify the agent's `git log --oneline` output shows real commit
history rather than only checking the exit code.

Fixed the `test_exec_git_push_then_merge` e2e release test. It was marked
`@pytest.mark.modal` even though its body only creates a local command agent and
runs local git operations (`mngr exec`, `git fetch`, `git merge`), so it never
invokes Modal -- the resource guard failed the otherwise-passing test with "never
invoked modal". Removed the spurious mark. The test now also verifies the actual
behavior: that the agent-side `git push` fails because no `origin` remote is
configured (proving `mngr exec` forwards the command to the agent host and
surfaces its non-zero exit code) and that the local `git fetch && git merge` chain
succeeds as an up-to-date no-op.

Fixed the `test_exec_git_status_short` e2e tutorial test: removed the superfluous
`@pytest.mark.modal` marker (the test only creates a local command-type agent and
never invokes Modal, so the resource guard failed it), and strengthened it to
deterministically create an uncommitted file in the agent's workspace and assert
that `git status --short` reports it.

- Fix the `test_exec_no_start` e2e tutorial test (covering `mngr exec --no-start`): removed the spurious `@pytest.mark.modal` mark, since the test creates a local agent and the local-host exec never invokes Modal, which made the resource guard fail the test. Also strengthened the assertion to verify the command actually ran on the agent's host (the `cat /etc/os-release` output contains `NAME=`) rather than only checking the exit code.

Fixed the `mngr exec` "error handling on multiple agents" tutorial block, which
still referenced the removed `-a`/`--all` flag. It now demonstrates the supported
pattern for targeting all agents: `mngr list --ids | mngr exec - ...`. The
corresponding e2e release test was updated to match and to verify the command
actually runs on the agent's host.

Fixed the `test_exec_short_form` e2e tutorial test: removed the spurious
`@pytest.mark.modal` mark. The test creates a local `--type command` agent and
runs `mngr x my-task "git status"` on it, so it never provisions a Modal
environment and never invokes the `modal` CLI; the modal resource guard
therefore failed it with "marked with @pytest.mark.modal but never invoked
modal". Also strengthened the assertion to verify the short-form `mngr x`
actually ran git in the agent's work_dir (observing the `On branch` output)
rather than only checking the exit code.

- Fix the `test_exec_with_start` e2e tutorial test (`mngr exec my-task --start`). It was missing the `@pytest.mark.timeout` marker that every other e2e tutorial test carries, so under the default 10s pytest timeout it timed out before `mngr create` finished. It also carried a superfluous `@pytest.mark.modal` mark even though it only exercises a local-provider agent and never invokes Modal, which tripped the resource guard's "marked modal but never invoked modal" check. Added a 180s timeout marker and removed the Modal mark. Also strengthened the test to assert that `mngr exec` actually forwarded the command output (the contents of `/etc/os-release`) rather than only checking the exit code.

- Remove the incorrect `@pytest.mark.modal` from the `test_full_lifecycle` e2e release test. The test exercises a local-provider command agent (create, exec, stop, start, destroy) and never initializes the Modal provider, so it never invokes the Modal CLI. The mark caused the resource guard to fail the otherwise-passing test with "marked with @pytest.mark.modal but never invoked modal". The `tmux` and `rsync` marks are retained because the local command agent runs in tmux and transfers its worktree via rsync.
- Strengthen `test_full_lifecycle` to verify that `mngr start` actually relaunches the agent's own command after a stop: it now asserts the `sleep 100100` process is running again post-restart (via `mngr exec ... 'ps aux'`), rather than only checking that `exec` works.

- Fix the `test_gc_background_watch` e2e tutorial test (CLEANING UP RESOURCES section). It was marked `@pytest.mark.modal`, but `mngr gc` only reaches Modal via in-process gRPC SDK calls in the `mngr` subprocess -- which the resource guard cannot observe (only `modal environment create`/`deploy` shell out to the guarded `modal` CLI binary, and gc does neither). The mark could therefore never be satisfied, so the test always failed with a "never invoked modal" resource-guard violation. Removed the superfluous mark. Also strengthened the test to verify the `commands.destroy.gc` setting actually persists and that `mngr gc` (the command `watch` runs) produces garbage-collection results.

- Fix `mngr gc --provider <name>` so that an explicitly selected provider which reports it has no state yet (e.g. a Modal per-user environment that has not been created) is skipped rather than failing the whole command. This matches the existing behavior of `mngr list --provider modal` and the documented intent that read/cleanup paths can always safely skip an empty provider; previously `mngr gc --provider modal` exited with an error on a fresh setup.
- Fix `mngr create` on a fresh Modal account: new-host creation no longer fails with "Provider 'modal' has no state yet". The provider handle used for teardown-on-failure was being constructed read-only *before* the host was resolved, which raised before the create path could bootstrap the Modal per-user environment. It is now obtained after host resolution, reusing the creation-capable (cached) provider instance.

Fixed the `test_get_json_into_var` scripting e2e test. It now overrides the 10s
global pytest timeout (Modal provider discovery inside the `mngr list` subprocess
can exceed it) and no longer carries `@pytest.mark.modal`: `mngr list` discovers
Modal via the in-process Python SDK inside the subprocess, which the modal
resource guard (whose SDK monkeypatch lives only in the pytest process) cannot
observe, so the mark produced a spurious "never invoked modal" failure. The test
also strengthens its assertion to confirm the shell variable actually captured a
non-empty JSON document.

Fixed the `test_git_merge_agent_branch` e2e release test: removed the
superfluous `@pytest.mark.modal` marker (the test only creates a local command
agent and merges its branch, so it never invokes Modal and the resource guard
failed it). Also strengthened the test to merge real agent work and verify the
merged file appears in the caller's working tree.

Updated the `mngr --help` tutorial line to list current top-level commands (`git`, `clone`) instead of the removed standalone `push`/`pull` commands, which are now `mngr git push`/`mngr git pull`. Added an unhappy-path e2e test verifying that an unknown command fails with a helpful error pointing back to `--help`.

Strengthened the `mngr --help` e2e test to assert that the help output lists the
other commands the tutorial advertises (`destroy`, `message`, `connect`, `clone`
in addition to `create` and `list`), and added an unhappy-path test verifying
that an unknown command fails and points the user back to `mngr --help`.

Strengthened the `test_invalid_provider_fails` e2e test so it genuinely exercises the unknown-provider path. Previously the command `mngr create my-task --provider nonexistent ...` was rejected first for not specifying an agent type, so the test passed without ever validating the provider. It now passes `--type command` so the failure is attributable to the unknown provider backend, asserts the error message names the offending provider, and confirms via `mngr list` that the failed create left no agent behind. Test-only change; no user-facing behavior change.

Fixed the tutorial example that combines `mngr list --format json` with `jq`. The
filter now reads `.agents[] | select(.state == "RUNNING") | .name` to match the
actual JSON shape (`mngr list --format json` emits an object with an `agents`
array), instead of the previous `.[]` which errored with "Cannot index array with
string". Tightened the corresponding e2e test accordingly.

- Fix the "JSON and JSONL works with most commands" tutorial example so it actually runs. It previously used `mngr snapshot list --format json`, but `mngr snapshot list` requires an explicit agent or host target and exits with a usage error when given none, so the example never worked standalone. It now uses `mngr list --format json && mngr plugin list --format jsonl`, two commands that work without setup and still demonstrate that `--format json`/`--format jsonl` apply across different commands. The corresponding e2e test additionally parses the combined output to confirm the JSON half is a single object and the JSONL half is one parseable object per line.

Fixed the `test_list_active_filter` e2e tutorial test, which exercises `mngr list --active`. The test was marked `@pytest.mark.modal`, but in a fresh environment with no agents `mngr list` deliberately skips the (not-yet-created) Modal provider instead of invoking it, so the resource guard failed the test for declaring a Modal mark it never used. Removed the superfluous mark and strengthened the test to assert the command reports "No agents found".

Fixed the `test_list_archived_filter` e2e release test: dropped the superfluous `@pytest.mark.modal` mark (the read-only `mngr list --archived` smoke test never invokes the `modal` CLI in an empty environment, so the resource guard flagged the mark as never-invoked) and strengthened it to assert that the `--archived` filter returns an empty, well-formed listing in a fresh environment.

Fixed the `test_list_combine_exclude_filters` e2e tutorial test (LABELS AND FILTERING section). The test was marked `@pytest.mark.modal` but a read-only `mngr list` never invokes the Modal CLI binary (it only uses the Modal SDK, which the resource guard cannot observe from the `mngr` subprocess), so the resource guard failed the test with "marked with @pytest.mark.modal but never invoked modal". Removed the superfluous mark and strengthened the test to create labeled agents and assert that `--exclude` applies OR logic (an agent matching any exclusion filter is dropped).

- Test-only change: fixed and strengthened the `test_list_combine_include_filters` e2e tutorial test for the LABELS AND FILTERING section. Removed an incorrect `@pytest.mark.modal` (the test never invokes Modal, so the resource guard failed it) and rewrote it to create labeled agents, stop one, and assert that combining multiple `--include` CEL filters applies AND semantics (every returned agent must satisfy all clauses).

Fixed the `test_list_compound_cel` LABELS tutorial e2e test, which timed out under the default 10s pytest limit and carried an unsatisfiable `@pytest.mark.modal` mark (a plain `mngr list` never shells out to the `modal` CLI, so the guard reported it never invoked Modal). Added a `@pytest.mark.timeout(120)` override and dropped the Modal mark. The test now creates labelled local agents and asserts that the compound CEL expression `labels.team == "backend" && state == "RUNNING"` enforces both predicates of the conjunction.

- Fix the "OUTPUT FORMATS" tutorial's custom-format example: `mngr list --format '{agent.name} ({agent.state})'` referenced a non-existent `agent.` namespace and rendered empty fields (` ()`). The list item *is* the agent, so fields are referenced bare; the example is now `mngr list --format '{name} ({state})'`, which renders `<name> (<STATE>)`.
- Strengthen the `test_list_custom_human_format` e2e release test: it now creates a real agent and asserts the template expands to the agent's actual name and state, plus an edge case confirming that unknown template fields render as empty strings.

Fixed the `test_list_exclude_filter` e2e tutorial test. It carried a
`@pytest.mark.modal` mark, but the documented `mngr list --exclude ...` command
never shells out to the `modal` CLI (it only performs in-process SDK discovery,
which the resource guard cannot observe across the subprocess boundary), so the
guard failed the test with "marked with @pytest.mark.modal but never invoked
modal". Removed the mark and rewrote the test to create two labeled command
agents and assert that the exclusion filter actually drops the matching agent
while keeping the others.

- Fix the WORKING WITH GIT tutorial to reference the real `initial_branch` field (the branch mngr creates for each agent) instead of the non-existent `git.original_branch` field in its `mngr list --fields` example, and harden the corresponding e2e test (correct its resource marks and assert on the command's actual output).

Fixed the `test_list_filter_by_label_cel` e2e tutorial test. It was marked `@pytest.mark.modal`, but a `mngr list` against an empty environment never creates Modal state and so never invokes the Modal CLI -- the only Modal usage the resource guard can track across the e2e subprocess boundary -- causing a spurious "marked with @pytest.mark.modal but never invoked modal" failure. The mark was removed, and the test now creates local agents with distinct `priority` labels to verify that the CEL filter actually includes matching agents and excludes non-matching ones.

Fixed the `test_list_filter_by_state` and `test_multiple_agents_coexist` e2e release tests, which could never pass: they lacked a per-test `@pytest.mark.timeout` override and so inherited the global 10s timeout (too short for creating/stopping agents), and they carried a spurious `@pytest.mark.modal` mark even though they only ever create local-provider agents (tripping the resource guard's "marked modal but never invoked modal" check). Also strengthened `test_list_filter_by_state` to assert on each agent's actual reported `state` (STOPPED vs RUNNING/WAITING), not just name membership in the filtered list.

Fixed the `test_list_filter_project_cel` e2e tutorial test, which exercises filtering agents by project with a CEL expression (`mngr list --include 'project == "my-project"'`). The test carried an unsatisfiable `@pytest.mark.modal` mark: `mngr list` reaches Modal only through the in-process gRPC SDK, which the e2e subprocess harness cannot track (the modal resource guard is only satisfied by the `modal` CLI binary, which list never invokes). Removed the spurious mark so the local CEL-filtering behavior is tested without a false guard failure.

Also strengthened the happy-path assertion (the CEL filter is evaluated and matches no agents in a fresh environment) and added an unhappy-path test that verifies a syntactically invalid `--include` expression is rejected with a clear `Invalid include filter expression` error.

Fixed the `test_list_format_json_recap` e2e test (covering `mngr list --format json`): removed an incorrect `@pytest.mark.modal` marker (a bare `mngr list` against an empty environment never invokes Modal, so the resource guard failed the test) and strengthened it to parse the JSON document and assert the documented `agents`/`errors` array structure.

Fixed the `test_list_format_jsonl_recap` e2e tutorial test (covering `mngr list --format jsonl`). It was failing for two reasons: the bare command exceeded the 10s default pytest timeout because Modal discovery is slow when credentials are present, and `@pytest.mark.modal` deterministically tripped the resource guard's "never invoked modal" check (a read-only `mngr list` reaches Modal only via in-process gRPC inside the `mngr` subprocess, which the in-process SDK guard cannot observe, and it never shells out to the guard-observable `modal` CLI). Added a `@pytest.mark.timeout(180)` override and removed the unenforceable `@pytest.mark.modal`. Also strengthened the assertion to verify the JSONL contract (every emitted line parses as a JSON object).

Removed the unsatisfiable `@pytest.mark.modal` mark from the `test_list_format_jsonl` e2e tutorial test. `mngr list` against an empty environment never shells out to the `modal` CLI binary (it only does in-process SDK discovery), so the modal resource guard reported the mark as never-invoked and failed the test. Also strengthened the test to verify that the JSONL output is well-formed (each emitted line is a standalone JSON object).

Fixed the `mngr list --host-label` e2e tutorial test so it no longer carries a
`@pytest.mark.modal` mark it cannot satisfy: `mngr list` on a fresh environment
skips the Modal provider (the environment does not exist yet) and never runs the
`modal` binary, so the resource guard correctly reported the mark as superfluous.
Also strengthened the test to verify the empty-result output and added an
error-path test for an invalid `--host-label` value (missing `KEY=VALUE`).

- Fix the tutorial's jq label-filtering example. `mngr list --format json` emits a top-level object (`{"agents": [...], "errors": [...]}`), not a bare array, so the documented `mngr list --format json | jq '.[] | select(.labels.priority == "high")'` failed with `jq: error: Cannot index array with string "labels"` (because `.[]` iterates the object's values -- the two arrays). The example now uses `.agents[]` to iterate the agent objects. Updated both the `mega_tutorial.sh` resource and the corresponding e2e test (`test_list_jq_filter`).

Fixed the LABELS tutorial e2e test `test_list_jsonl_jq_stream` so it exercises the streaming jq filter against a real fleet: it now seeds two local command agents labeled `priority=high` and `priority=low`, then asserts `mngr list --format jsonl | jq 'select(.labels.priority == "high")'` emits the high-priority agent and drops the low-priority one. Removed the superfluous `@pytest.mark.modal` (plain `mngr list` exercises Modal only via the in-process SDK, which is not tracked across the `mngr` subprocess) and added the `rsync`/`tmux` marks plus a longer timeout that local agent creation requires.

- Remove a superfluous `@pytest.mark.modal` from the e2e tutorial test `test_list_label_filter`. The resource guard correctly flagged it as never invoking modal: `mngr list` skips the Modal provider (via `ProviderEmptyError`) when the per-user Modal environment does not exist yet, which is always the case in these no-agent e2e tests, so the `modal` binary is never invoked.
- Strengthen the happy-path assertion to verify the label filter actually runs and produces an empty result ("No agents found"), and add an unhappy-path test (`test_list_label_filter_invalid_format`) covering the `KEY=VALUE` validation error for a malformed `--label` value.

Fixed the `test_list_limit` e2e tutorial test: removed the superfluous
`@pytest.mark.modal` mark. `mngr list --limit 10` in a fresh environment never
invokes the Modal CLI (the only Modal usage the e2e resource guard can observe
in a subprocess), so the mark tripped the guard's `NEVER_INVOKED` check.

Strengthened the same test so it actually exercises `--limit`: it now creates
two agents, asserts that `mngr list --limit 10` shows both, and that
`mngr list --limit 1` truncates the result to a single agent (previously it
only ran the command against an empty environment and checked the exit code).

Fixed the `test_list_local_filter` e2e release test (covering the `mngr list --local` tutorial block). The test runs `mngr` as a subprocess, where the real Modal SDK call happens during full provider discovery; the resource guard's in-process Modal SDK monkeypatch cannot observe that subprocess call, so `@pytest.mark.modal` was failing as "never invoked". The test now records the subprocess Modal usage with the guard explicitly (mirroring the lima release test's approach). It also carries an explicit `@pytest.mark.timeout(60)` because `mngr list`'s discovery path routinely runs ~10s, past the default 10s per-test timeout (the release CI lane already overrides this globally to 90s). Strengthened the assertion to verify the command reports "No agents found" in the fresh environment.

Fixed the `test_list_pipe_stdin` e2e tutorial test (covering `mngr list --format "{id}" | head -n 2 | mngr list --stdin`): removed the incorrect `@pytest.mark.modal` mark (the list pipeline never invokes Modal) and gave it a realistic timeout, then improved it to create a real agent and verify that ids piped through `mngr list --stdin` actually filter the listing.

- e2e tests: fixed the PROJECTS tutorial test `test_list_project_dot` by removing the
  superfluous `@pytest.mark.modal` mark. `mngr list --project .` runs in a subprocess
  and only touches Modal via the SDK (gRPC), which the resource guard cannot track from
  the subprocess (it only tracks the Modal CLI there), so the mark tripped the guard's
  "marked but never invoked" check. Also strengthened the test to assert the command
  emits a clean "No agents found" listing, confirming `.` is expanded to the current
  project rather than rejected or treated literally.

- `mngr list --fields` now accepts `project` as a short alias for the `labels.project` field (mirroring the existing `--project` filter flag and the `host.provider` field alias). Previously `mngr list --fields "name,project,state"` rendered an empty `PROJECT` column because `project` resolved to a nonexistent top-level attribute; it now shows each agent's project label.

Removed the superfluous `@pytest.mark.modal` from the `test_list_project_filter`
e2e tutorial test. A read-only `mngr list --project ...` against a fresh
environment never invokes the `modal` CLI binary (the only Modal usage trackable
from a spawned subprocess) and never creates the Modal environment, so the
resource guard's "marked modal but never invoked modal" check failed the test.

Also strengthened the test to assert the filtered listing renders the expected
empty result ("No agents found") instead of only checking the exit code, so a
command that errors but still exits 0 would be caught.

- Fix the `test_list_provider_docker` e2e tutorial test so it actually exercises the docker provider. The test was marked `@pytest.mark.docker` but only ran `mngr list --provider docker`, which reads the state volume via the docker SDK in a subprocess (invisible to the CLI-binary resource guard) and never invokes the `docker` CLI -- so the resource guard flagged the mark as "never invoked". The test now first creates a lightweight docker command agent (which shells out to `docker build`/`docker run`, satisfying the guard) and then asserts that the created agent appears in `mngr list --provider docker` output, making the listing assertion meaningful instead of just checking a clean exit. Added `@pytest.mark.rsync` and `@pytest.mark.timeout(300)` to match the other docker-create tests. The tutorial block itself is unchanged.

Removed the spurious `@pytest.mark.modal` mark from the `mngr list --provider`
e2e tutorial test. The mark is unsatisfiable for these subprocess-based e2e
tests (the Modal SDK guard only observes in-process calls, and `mngr list`
skips the Modal backend entirely when its per-user environment does not exist
yet), so the resource guard failed them with "marked modal but never invoked".
Also strengthened the test to assert the empty-listing output and added an
unhappy-path test covering an unknown provider name.

Fixed the `test_list_remote_filter` e2e tutorial test. It was marked
`@pytest.mark.modal`, but `mngr list --remote` only performs read-only Modal SDK
discovery (which short-circuits when no Modal environment exists) and never
invokes the Modal CLI, so the resource guard correctly reported that the test
never exercised Modal. Removed the unsatisfiable mark and strengthened the test
to actually verify the `--remote` filter: it now creates a local agent and
asserts that the agent is excluded from `mngr list --remote` while remaining
visible under `mngr list --local`.

Tightened the `mngr list --running` e2e tutorial test (`test_list_running_filter`). It previously carried `@pytest.mark.modal` but only ran `mngr list --running` against an empty environment, which never reaches Modal; the resource guard now flags such superfluous marks. The test now creates real agents and asserts that `--running` includes a genuinely running agent and excludes a stopped one.

- Fix the release test `test_list_stopped_filter` (in `imbue/mngr/e2e/tutorial/test_list.py`) by removing its superfluous `@pytest.mark.modal`. In an isolated, empty environment, `mngr list --stopped` discovers via the provider SDKs (Modal gRPC) and never shells out to the `modal` CLI binary -- the only Modal usage the resource guard can observe across the `mngr` subprocess boundary -- so the mark tripped the guard's "marked with @pytest.mark.modal but never invoked modal" check. Also strengthen the test to assert the command reports `No agents found` rather than only checking the exit code. No production behavior change.

Fixed the `test_list_watch_mode` e2e tutorial test. It was marked
`@pytest.mark.modal` even though `watch -n5 mngr list` against a fresh,
empty environment never exercises Modal (the listing pipeline skips the
Modal provider when its environment does not exist), which made the resource
guard fail the test for a superfluous mark. Removed the mark and strengthened
the assertion to verify that `watch` actually renders the wrapped `mngr list`
output.

- Remove the superfluous `@pytest.mark.modal` mark from the `test_list_with_no_agents` e2e tutorial test. In a fresh, isolated environment `mngr list` correctly short-circuits Modal host discovery (the Modal environment does not exist yet, so the provider raises `ProviderEmptyError` without making any Modal API call). Because the test never actually invokes Modal, the resource guard failed it with "marked with @pytest.mark.modal but never invoked modal". The test still runs under `@pytest.mark.release` and verifies that `mngr list` succeeds and prints "No agents found".

- Add a `-a`/`--all` flag to `mngr message` (alias `mngr msg`) to broadcast a message to every agent, matching the documented tutorial usage (`mngr msg -a -m "..."`). Previously the flag was undocumented in the CLI and rejected with "No such option: -a", even though the underlying API already supported it. `--all` cannot be combined with explicit agent names, and on its own it no longer requires naming an agent.

Fixed the `test_message_commit_request` git tutorial e2e test (`mngr msg`): it
now has a longer per-test timeout to accommodate cross-provider agent discovery,
and no longer carries a superfluous `@pytest.mark.modal` mark (the command only
contacts Modal via the mngr subprocess SDK, which the resource guard cannot
attribute to the test, so the mark always tripped the "never invoked" check).
The test also asserts that `mngr msg` actually reports delivery to the target
agent, not just that the command exits cleanly.

Fixed the `test_message_filtered_backend` e2e tutorial test (LABELS AND FILTERING
section). The test now creates a real backend-labeled agent and a frontend-labeled
agent before running the `mngr list --include ... --ids | mngr message -` pipeline,
and asserts the message reaches only the backend agent. Replaced the incorrect
`@pytest.mark.modal` marker (the command never invokes Modal) with the markers that
match the resources actually used (`tmux`, `rsync`) plus an explicit per-test timeout.

Fixed the `test_message_filtered_via_stdin` e2e tutorial test. It now carries a
`@pytest.mark.timeout(90)` (plus a longer per-command timeout) so the two-stage
`mngr list | mngr msg` pipeline has enough headroom on slow filesystems, and the
superfluous `rsync`/`tmux`/`modal` resource marks were dropped because the
empty-filter pipeline is a no-op that never invokes those resources.

Also hardened the test to assert the filtered id list really is empty and that
no message is reported as delivered, and added a happy-path companion test
(`test_message_filtered_via_stdin_delivers_to_matching_agents`) that pipes a
non-empty id list (local-provider agents) into `mngr msg -` and verifies the
message is actually delivered to each matched agent.

- Fixed the `test_message_multiple_agents_by_name` e2e tutorial test: added `@pytest.mark.timeout(120)` so creating three local command agents over tmux+rsync no longer trips the default 10s pytest-timeout, and removed the superfluous `@pytest.mark.modal` mark (the test only messages local agents by name and never invokes Modal, which the resource guard correctly rejected). Also strengthened the assertions to verify each named agent is actually reached (`Message sent to: agent-N`) and that the aggregate success count is reported.

- Fix the `test_message_one_agent` e2e tutorial test: messaging a single named local agent never invokes Modal, so its superfluous `@pytest.mark.modal` was removed (the resource guard was failing the otherwise-passing test). Strengthened its assertions to verify the message is actually delivered to the named agent (`Message sent to: my-task`, exactly one successful delivery). Added an unhappy-path `test_message_nonexistent_agent` covering the same tutorial block, which asserts that messaging a non-existent agent is a no-op (`No agents found to send message to`, exit 0) rather than an error.

Fixed the `test_message_short_form` e2e tutorial test. It previously timed out
under the default 10s pytest timeout while creating the local command agent, and
it carried an incorrect `@pytest.mark.modal` marker even though messaging a
single named local agent never invokes Modal (the resource guard fails such
tests). Added `@pytest.mark.timeout(180)` and removed the `modal` marker. Also
strengthened the test to assert the message was actually delivered ("Message
sent to: my-task" / "Successfully sent message to 1 agent(s)") rather than only
checking the exit code. Test-only change; no user-facing behavior change.

Fixed the multi-agent e2e release tests (`test_multiple_agents_coexist`,
`test_list_filter_by_state`) so they pass outside of offload: added explicit
`@pytest.mark.timeout` markers (the global 10s default killed them in plain
`pytest` runs) and removed the spurious `@pytest.mark.modal` mark. These tests
create only local-provider command agents and never invoke Modal through a
tracked code path, so the Modal resource guard failed them with "marked with
@pytest.mark.modal but never invoked modal".

Fixed the release e2e test `test_observe_discovery_pipe_python` (which exercises the tutorial's `mngr observe --discovery-only | python` pipe). It had been failing the resource-guard check because it carried `@pytest.mark.modal` while a discovery-only command never invokes the `modal` CLI binary (it reaches modal only via the in-process gRPC SDK, whose guard monkeypatch does not cross the `mngr` subprocess boundary). Removed the inapplicable mark and strengthened the test to warm the discovery cache and assert the discovery snapshot actually flows through the python one-liner. Test-only change; no user-visible behavior change.

Fixed the `test_observe_discovery_recap` e2e release test, which covers the
`mngr observe --discovery-only` tutorial block. The test was marked
`@pytest.mark.modal`, but on a fresh test environment (no Modal agents created)
the discovery stream deliberately skips the Modal provider because its
environment does not exist yet, so no Modal call is ever made. The resource
guard therefore failed the test for declaring a Modal dependency it never
exercised. Removed the superfluous mark and strengthened the test to assert that
the stream actually emits a `DISCOVERY_FULL` JSONL snapshot, rather than only
checking that the command exits cleanly.

Strengthened the `test_plugin_add_by_git` e2e tutorial test. It now asserts the
command exits with code 1 and emits a clean "Aborted:" message (rather than just
a non-zero exit code), confirming that `--git` is accepted as a source specifier
and the command reaches an intentional error path instead of a click usage error
or an uncaught traceback.

Raised the per-test timeout for the `mngr plugin add <name>` e2e tutorial test so it accounts for mngr's ~10s cold-start cost, and strengthened its assertions to verify the command exits with a clean, controlled error (non-zero exit plus an "Aborted" message, no traceback) instead of merely checking the exit code.

Strengthened the `mngr plugin add --path` e2e tutorial test to assert a clean abort (exit code 1) with an `Aborted` message and no traceback or click usage error, rather than only checking for a non-zero exit code. This verifies the `--path` option is recognized and the command fails gracefully when the path cannot be installed.

Fixed the `test_plugin_disable_user_scope` e2e tutorial test to match the actual
behavior of `mngr plugin disable my-plugin --scope user`: disabling a not-yet-installed
plugin is a soft operation that succeeds (with a warning) and persists the setting. The
test now asserts the command succeeds, emits the "not currently registered" warning, and
verifies the disabled state is persisted by reading it back via `mngr config get
plugins.my-plugin.enabled --scope user`.

Fixed the `test_plugin_enable_project_scope` e2e tutorial test to match the actual behavior of `mngr plugin enable --scope project`. Enabling a not-yet-installed plugin is a soft pre-configuration that succeeds, records `plugins.<name>.enabled = true` in the project `settings.toml`, and warns that the setting applies once the plugin is installed. The test now verifies this concrete effect instead of incorrectly expecting a failure.

Strengthened the `test_plugin_list_active_to_see_types` e2e tutorial test to verify that `mngr plugin list --active` actually lists the built-in agent types (claude, codex, command) the tutorial discusses, instead of only asserting the command exits successfully.

Strengthened the `mngr plugin list --active` e2e tutorial test to verify the actual filtering behavior: it now parses the JSON output to assert every listed plugin is enabled, disables a real plugin, and confirms it disappears from `--active` while still appearing (as disabled) in the unfiltered `mngr plugin list`.

Corrected the plugin-management tutorial: `mngr plugin list --fields` now demonstrates the real `enabled` field instead of the non-existent `active` field (which silently rendered as `-` for every plugin). Strengthened the corresponding e2e release test to assert that the requested fields render real values.

Strengthened the `mngr plugin list` e2e tutorial test (`test_plugin_list_shows_installed`) to assert the listing renders its column headers (NAME, VERSION, DESCRIPTION, ENABLED) and includes core always-shipped plugins (`claude`, `modal`), rather than only checking for the substring `claude`.

Strengthened the `mngr plugin remove` e2e tutorial test to assert that removing
a non-installed plugin fails cleanly with a user-facing "Aborted" error and
never a Python traceback, and added an unhappy-path test that verifies an
invalid package name is rejected with a clear argument-validation error.

Fixed the `test_recipe_launch_check_cleanup` release e2e test (COMMON TASKS recipe block) so it passes against the current CLI. The test substitutes a local `command` (sleep) agent for the recipe's modal claude agent, and several recipe steps behave differently for that stand-in:

- Added the missing `@pytest.mark.timeout(300)` marker (all sibling tutorial e2e tests carry one); without it the test fell back to the global 10s default and timed out partway through.
- The `mngr transcript` step now asserts the real behavior for a `command` agent: command agents do not produce a common transcript, so the exact recipe command (`mngr transcript fix-bug --tail 3`) exits non-zero with a clear "does not produce a common transcript" message.
- The `mngr conn` step runs in the e2e harness without a TTY, so the interactive `tmux attach` cannot complete; the test now verifies the command resolves the named agent and reaches the connect step rather than asserting a clean exit.
- Removed the superfluous `@pytest.mark.modal` marker: the recipe substitutes a local command agent and never invokes Modal, which tripped the resource-guard "marked modal but never invoked modal" check.

Also strengthened the test's verifications: it now confirms the created agent is genuinely alive in its own worktree (`mngr exec fix-bug pwd`) -- the concrete intent of the "check what agents are running" step, which `mngr list --running` alone could not show for the idle `sleep` stand-in (it reports WAITING, not RUNNING) -- and confirms `mngr destroy --remove-created-branch` actually removed the agent's branch.
</content>

Fixed the `test_recipe_multi_agent_parallel_workflow` e2e release test for the
MULTI-AGENT WORKFLOWS tutorial recipe:

- Added a `@pytest.mark.timeout(120)` override so the multi-step recipe (which
  creates three command agents plus list/wait/exec/msg/merge/destroy) is not
  killed by the default 10s per-test timeout, and removed the superfluous
  `@pytest.mark.modal` mark since the test exercises local command agents only.
- Corrected the tutorial's "message all agents" step: `mngr msg -a` is not a
  valid option. It now uses the documented idiom
  `mngr list --ids | mngr msg - -m "..."` (updated in `mega_tutorial.sh` too).
- Strengthened the test to verify actual behavior rather than just exit codes:
  all three agents are created and isolated in distinct worktrees, the
  broadcast message reaches all three, and cleanup removes them.

- Fix the very first `mngr create --provider modal` (or any create that bootstraps a fresh per-user Modal environment) so it no longer aborts with `Provider 'modal' has no state yet`. The create path's teardown-guard provider resolution was running with read-only semantics and raising `ProviderEmptyError` before the environment could be created; it now resolves the provider in host-creation mode, creating the environment exactly once.
- Extend the `mngr snapshot create -` (snapshot-all-via-stdin) e2e test to verify the snapshot is actually recorded for the agent and visible via `mngr snapshot list`, rather than only checking the command's exit code.

- Add a `--dry-run` flag to `mngr snapshot destroy`. It resolves and reports exactly which snapshots would be destroyed (honoring `--snapshot` / `--all-snapshots`) without deleting anything, in human, JSON, JSONL, and `--format` template output. This matches the behavior already documented in the tutorial.
- Fix a regression where the very first `mngr create --provider modal` (or `mngr create NAME@.modal`) against a brand-new Modal environment failed with "Provider 'modal' has no state yet" instead of bootstrapping the per-user Modal environment. The create path now resolves the new-host provider with `is_for_host_creation=True`, consistent with how the host itself is resolved.

- Fix the `test_start_all_via_stdin` e2e tutorial test (STARTING/STOPPING section). It was failing two ways: the global 10s `pytest-timeout` budget was too short for a test body that runs multiple `mngr` subprocess invocations (rsync + tmux), and it carried a superfluous `@pytest.mark.modal` mark even though it only exercises a local-provider agent and never invokes the `modal` binary (which tripped the resource-guard "marked modal but never invoked modal" check). Added `@pytest.mark.timeout(120)` and removed the modal mark. Also strengthened the test to stop the agent and verify it transitions back to running when started via stdin, rather than only checking the command exit code. Test-only; no runtime behavior change.

Fixed the `test_start_connect` e2e tutorial test (covers `mngr start <agent> --connect`):
raised its per-test timeout so the full local create + start round-trip fits, and
dropped the inapplicable `@pytest.mark.modal` mark (starting a named local agent never
enumerates Modal). Also strengthened the test to verify the `--connect` path actually
ran the connect command (rather than only checking the command's exit code).

- Add a `--dry-run` flag to `mngr start`. With it, the command reports which agents *would* be started (e.g. `mngr list --ids | mngr start - --dry-run`) and returns without contacting any host or starting anything. This matches the `--dry-run` already supported by `mngr archive`, `mngr destroy`, and `mngr gc`, and the dry-run usage shown in the tutorial.

- Fix the `test_start_idempotent` e2e tutorial test (STARTING AND STOPPING AGENTS) so it can run standalone, not only under the offload release harness. Added an explicit `@pytest.mark.timeout(180)` (the test creates and starts a real agent over tmux, which exceeds the 10s default that applies outside offload) and removed the inaccurate `@pytest.mark.modal` mark (the test exercises a local command agent and never invokes Modal; the resource guard correctly flagged the mark as superfluous).
- Strengthen the same test to verify actual behavior beyond a zero exit code: after the idempotent start it now confirms the agent was not torn down by exec'ing into it and checking the command lands in the agent's own worktree. Added a companion `test_start_stopped_agent` covering the primary documented path -- stopping a running agent and starting it back up -- asserting on the `Stopped agent` / `Started agent` output and post-restart reachability.

- Remove the incorrect `@pytest.mark.modal` from the `test_start_multiple_agents` e2e tutorial test. The test only creates and starts local `--type command` agents and never runs a Modal-touching command (e.g. `mngr list`), so the resource guard failed it for declaring a `modal` mark that was never exercised. Also strengthened its assertions to verify the single `mngr start agent-1 agent-2 agent-3` invocation actually reports all three agents as started, rather than only checking the exit code.

- Fix the `test_stop_all_via_stdin` lifecycle e2e test (and improve its assertions). It was marked `@pytest.mark.modal` but creates a default (local-provider) command agent, so it never invokes the Modal CLI; the resource guard correctly failed it for declaring a Modal mark it never exercised. Removed the spurious `modal` mark (keeping `rsync`/`tmux`, which the local create path genuinely uses) and added behavioral assertions that verify the agent actually transitions to a stopped state after `mngr list --ids | mngr stop -`.

Fixed the `test_stop_archive` e2e lifecycle test: added a `@pytest.mark.timeout(120)` mark (the test was hitting the global 10s pytest timeout during the create + stop flow) and removed the superfluous `@pytest.mark.modal` mark, since the test only exercises a local command agent and never invokes Modal. Also strengthened the test to verify that `mngr stop my-task --archive` actually stops and archives the agent (the agent now appears under `mngr list --archived` and is excluded from `mngr list --active`).

Corrected the tutorial comment for `mngr stop --archive`: it previously claimed the command "creates a snapshot before stopping", but `--archive` only marks the agent archived (sets the `archived_at` label) while preserving its state -- no snapshot is created.

- Fix the `test_stop_basic` e2e tutorial test (STARTING AND STOPPING AGENTS section). It inherited the global 10s pytest timeout, which was too short for a real agent create + stop and caused the test to time out; it now sets an explicit `@pytest.mark.timeout(180)`. The spurious `@pytest.mark.modal` mark was also removed because the test creates a local command agent and never invokes Modal (its commands run entirely against the local provider). The test now also verifies the concrete effect of `mngr stop` by asserting the agent is reported as `STOPPED` via `mngr list --stopped` and no longer appears in `mngr list --running`.

- Fix the `test_stop_by_session_name` e2e tutorial test (covering `mngr stop --session`) so it reliably passes: add a `@pytest.mark.timeout(60)` override (the default 10s function timeout was too tight for the mngr subprocess cold start, matching the timeout overrides used by every other e2e tutorial test file) and drop the inaccurate `@pytest.mark.modal` mark (the malformed placeholder session name is rejected by the prefix-format guard before any provider/Modal scan runs). Also tighten the assertions to verify a clean non-zero exit with a clear validation error and no Python traceback.

- Add a `--dry-run` flag to `mngr stop`. It reports which agents (or, with `--stop-host`, which hosts) would be stopped without actually stopping anything, matching the `--dry-run` flag already offered by `mngr archive`, `mngr destroy`, and `mngr cleanup`. This makes the documented `mngr list --ids | mngr stop - --dry-run` tutorial example work.

Fixed the create-template e2e tutorial tests (`test_templates.py`). The
`test_templates_setup_via_config_edit` test now opts the project `settings.toml`
that `mngr config edit --scope project` creates into the pytest run (via
`is_allowed_in_pytest = true`) before invoking `mngr create`, so the config
loader no longer refuses the freshly created project config. Also added the
missing `@pytest.mark.tmux` to all three template tests, which create local
command agents that use tmux.

- Fix the `test_tips_exec_env_inspect` e2e tutorial test so it passes: it creates a local `--type command` agent and execs a specific agent by name, which only invokes `tmux` and `rsync` (never Modal), so the spurious `@pytest.mark.modal` was dropped and a `@pytest.mark.timeout(120)` was added (the default 10s pytest timeout was killing the legitimately slow `mngr exec` call). Also strengthened the assertion to confirm `mngr exec my-task -- env` actually runs inside the agent's environment (checking for the injected `MNGR_AGENT_NAME`/`MNGR_AGENT_ID` variables) rather than relying on the pipeline's exit code, which the `| sort` pipe would otherwise mask.

- Fix the `test_tips_transcript_tail_assistant` e2e tutorial test (TIPS AND TRICKS section). The test previously created a `command`-type `my-task`, but `mngr transcript` now rejects agent types that do not produce a common transcript, so the command exited non-zero. The test now seeds a known common transcript on a `claude`-typed agent and asserts that `mngr transcript my-task --tail 5 --role assistant` emits exactly the last five assistant messages and no user messages. Also dropped the superfluous `@pytest.mark.modal` mark (the local-only flow never invokes modal). The tutorial block is unchanged.

- Fix the `test_transcript_assistant_only` e2e tutorial test (`mngr transcript my-task --role assistant`). It previously created a `command`-type agent, which `mngr transcript` correctly rejects because command agents do not emit a common transcript, so the test failed before exercising any transcript behavior. The test now creates a transcript-capable (`claude`-typed) agent and seeds a representative common transcript (user, assistant, and tool-result events), then asserts that `--role assistant` shows only the assistant message and filters out the user message and tool result. No production behavior changed.

Fixed the `mngr transcript` e2e tutorial tests (`test_transcript.py`). They
previously created a `command`-type agent, which `mngr transcript` rejects
(command agents produce no common transcript), so the tests could never pass.
They now create a real local `claude` agent with an initial message, wait for
the first assistant reply, and assert on the actual transcript content for each
variant (`--role`, `--tail`, `--format jsonl`).

- Fixed the `mngr transcript` e2e tutorial tests, which previously created a `command`/sleep agent as a Claude stand-in. `mngr transcript` only works for agent types that emit a common transcript (Claude, Antigravity), so those tests failed with "does not produce a common transcript". They now create a real headless Claude agent with an initial message and wait for its transcript before exercising `mngr transcript`. Added a `setup_claude_trust` helper to the e2e session, added per-test `@pytest.mark.timeout(300)` markers, removed the spurious `@pytest.mark.modal` mark (these tests run entirely on a local agent), and added an assertion that the JSONL output parses and contains the sent user message.

- Fix the `test_transcript_tail_one` e2e tutorial test so it actually exercises `mngr transcript my-task --tail 1`. The test previously created a `command`-type agent (a `sleep` stand-in), but `command` agents legitimately produce no common transcript, so `mngr transcript` always exited non-zero. The test now materializes a stopped `claude` agent together with its `claude/common_transcript` events file on disk and asserts that `--tail 1` shows exactly the most recent message. Its `@pytest.mark.modal`, `@pytest.mark.tmux`, and `@pytest.mark.rsync` marks were dropped because the test no longer creates an agent and therefore never touches those resources (the resource guard flags such superfluous marks).
- Add `test_transcript_tail_one_on_command_agent_errors`, an error-path test for the same tutorial block that confirms `mngr transcript --tail 1` on a `command` agent fails with a clear "does not produce a common transcript" message.

Fixed the `test_troubleshoot_check_agent_state` e2e tutorial test: added the
`@pytest.mark.timeout(120)` override that every other create-based e2e tutorial
test uses (the global 10s function-body timeout was killing the create + list)
and removed the spurious `@pytest.mark.modal` mark, since the test only creates a
local command agent and never exercises Modal. Also strengthened the assertions to
verify the just-created agent actually appears in `mngr list` output with its
resolved `local` provider, rather than only checking the command's exit code.

- Fixed the `test_troubleshoot_follow_events` e2e release test, which was failing reliably. It inherited the global 10s per-test timeout, but each `mngr` subprocess invocation costs several seconds of cold-start, so two invocations (create plus follow) could not fit. Added an explicit `@pytest.mark.timeout(120)` (matching the other e2e tutorial tests) and removed a stale `@pytest.mark.modal` mark -- the test exercises a local command agent and never invokes Modal, which the resource guard flagged once the test ran to completion.
- Strengthened the same test to bound `mngr event --follow` with a window that outlasts mngr's startup and to assert the command stays in the follow stream loop until killed (exit code 124), instead of masking the exit status with `|| true`. Added a companion unhappy-path test that verifies `mngr event <unknown-agent> --follow` fails fast with an agent-not-found error rather than hanging.

Fixed the `test_troubleshoot_gc_dry_run_then_gc` release test so it no longer
trips the default 10s pytest timeout: `mngr gc` walks every configured provider
(including Modal), which routinely takes longer than 10s. The test now carries an
explicit `@pytest.mark.timeout(120)` and gives each `mngr gc` invocation a
generous per-run timeout, matching the pattern used by the sibling
`test_troubleshoot_destroy_and_recreate_modal` test.

Fixed the host-diagnostics block in the mega tutorial. `mngr exec` takes the
command to run as a single argument (its last positional), so the previous
`mngr exec my-task -- ps aux` form parsed `ps` as a second agent name and
failed with "Agent not found". The tutorial now quotes each command, e.g.
`mngr exec my-task "ps aux"`, which also makes the `cat ... | tail -20` pipe
run on the agent's host as intended.

- Fix the `test_troubleshoot_recent_events` e2e tutorial test for `mngr event my-task --tail 20`. The test created a local `command` agent and viewed its events, so it never provisions Modal (the event-stream discovery optimization resolves the known agent to the local provider only). The spurious `@pytest.mark.modal` was therefore failing the resource guard's "marked modal but never invoked modal" check. Removed the mark and added a `@pytest.mark.timeout(120)` since the create-plus-view sequence runs longer than the default 10s per-test timeout.
- Strengthen the `mngr event` tutorial coverage: the happy-path test now asserts the printed output is well-formed JSONL (each event record is a JSON object), and a new `test_troubleshoot_recent_events_missing_agent` covers the unhappy path where viewing events for an unknown agent fails with a clear "Could not find agent" error.

Fixed the `test_troubleshoot_stop_restart` e2e tutorial test (TROUBLESHOOTING section). The test created a local command agent and ran `mngr stop` / `mngr start --connect`, but never exercised Modal, so the superfluous `@pytest.mark.modal` mark tripped the resource guard; it also exceeded the default 10s function timeout. Removed the unused `modal` mark and added `@pytest.mark.timeout(120)`. Added behavioral verification that asserts the agent's state transitions to stopped after `mngr stop` and back to running after `mngr start`.

Fixed the `test_troubleshoot_transcript_for_errors` e2e tutorial test. The
troubleshooting block substitutes a lightweight `command` agent for the
tutorial's claude agent, and `mngr transcript` correctly rejects command
agents (they produce no common transcript). The test now asserts that clear
diagnostic instead of expecting success, adds a timeout override to cover the
agent-create latency, and drops the superfluous `modal` mark (the transcript
diagnostic is a local, client-side agent-type check that never scans modal).

- Fix the `test_usage_wait_and_create` e2e tutorial test: give it a `@pytest.mark.timeout(180)` (the default 10s is far too short for `mngr usage wait`, whose first discovery poll over all providers takes ~30s), raise the command run timeout to 120s, and drop the inapplicable `@pytest.mark.modal` (the timeout-capped `usage wait` short-circuits the chained `mngr create`, so Modal is never invoked in a guard-trackable way). The test now also asserts the command exits 2 (usage wait timed out, create skipped) and that no `chore` agent was created.

## 2026-06-07

# Add a public `set_command` setter on agents

Agents now expose a certified `set_command(command)` setter (on `AgentInterface` / `BaseAgent`)
alongside the existing `get_command`, mirroring the other certified field getters/setters
(`set_labels`, `set_is_start_on_boot`, ...). It persists the agent's stored launch command through
the same atomic write + external-storage save path as the other setters. This lets callers update
the command that an agent re-runs on its next start/restart without reaching into the agent's
on-disk `data.json` directly.

Added per-agent tmux window sizing and resize policy.

- `mngr create` accepts `--tmux-width`, `--tmux-height`, and `--tmux-window-size` (`manual|latest|largest|smallest`). These set the agent's tmux window dimensions at session creation and its resize policy.
- Defaults are unchanged from before: a `200x50` window with tmux's default resize-on-attach behavior. `manual` pins the window to its configured size so it is never resized when a client attaches.
- The options are persisted on the agent (in `data.json`) and applied on every (re)start, so they survive `stop`/`start`, `clone`, `migrate`, and `snapshot`. They are provider-agnostic (local, docker, modal, remote).
- `mngr connect` skips its post-attach resize for a `manual`-window agent (decided on the remote host at attach time), so the pinned dimensions survive an interactive attach.

## 2026-06-06

Regenerated the `mngr robinhood` CLI help doc (`docs/commands/secondary/robinhood.md`) to document the new `--include-partial-messages` and `--stream-plain-text` streaming flags (implemented in `imbue-mngr-robinhood`).

## 2026-06-05

Two small shared core helpers were added/extracted to support the antigravity per-agent isolation work (reusable by other plugins):

- `imbue.mngr.utils.git_utils.find_git_source_path` -- the per-agent source-repo trust resolution now delegates to this, eliminating logic duplicated byte-for-byte between the `antigravity` and `claude` plugins (no behavior change).
- `imbue.mngr.hosts.common.symlink_or_copy_on_host(host, source, dest, *, symlink, ensure_source_parent=...)` -- a one-round-trip helper that symlinks (always, even to a not-yet-existing source -- a write-through symlink) or copies (only if the source exists) a path on the host, centralizing the symlink-vs-copy credential/cache pattern.

`mngr dependencies` was reworked so that "which dependencies count" and "whether to install" are now two orthogonal options instead of being conflated into the old `-c`/`-a`/`-i` flags. The old flags were removed and replaced by:

- `--scope core|all` (default `all`): which dependencies determine the exit code (and which `--install auto` targets). `--scope core` exits non-zero only when a *core* dependency is missing -- missing optional dependencies are tolerated and the command exits 0. `--scope all` exits non-zero if anything is missing.
- `--install none|interactive|auto` (default `none`): `none` only checks; `interactive` shows the same prompt as before; `auto` installs the in-scope missing dependencies without prompting.

Mapping from the old flags: old `-c` becomes `--scope core --install auto`, old `-a` becomes `--install auto`, and old `-i` becomes `--install interactive`.

`ssh` was reclassified from a core to an optional dependency. mngr's remote-host connectivity runs through paramiko (pure-Python, no `ssh` binary), so the `ssh` binary is only needed to attach an interactive session to a remote agent and as the transport for rsync/git over SSH -- all remote-only features, putting it in the same category as `rsync` and `unison`. The core dependencies are now `git`, `tmux`, and `jq`. Every path that shells out to the `ssh` binary -- `mngr connect` to a remote agent, `mngr git push`/`pull` and `mngr rsync` to a remote host, and the git-mirror/rsync source transfer when creating a remote agent -- now raises a clear `BinaryNotInstalledError` if `ssh` is missing, instead of an opaque "ssh: command not found".

Updated documentation references for the `mngr_uncapped_claude` plugin rename to
`mngr_robinhood`: the PyPI README sub-projects list now points to
`libs/mngr_robinhood/`, and the auto-generated CLI docs entry moved from
`docs/commands/secondary/uncapped-claude.md` to
`docs/commands/secondary/robinhood.md` (command renamed from
`mngr uncapped-claude` to `mngr robinhood`).

## 2026-06-04

- Fix `mngr rsync` (and any other command resolving a host-location address) so that a *relative* `:PATH` on an agent endpoint resolves against that agent's workdir rather than the ambient working directory of the process running mngr. Previously `mngr rsync ./src/ my-agent:runtime/foo` passed the bare `runtime/foo` straight to rsync, which resolved it against the caller's cwd -- on a local-provider host that silently targeted the *caller's* checkout instead of the agent's worktree (and, when source and destination collapsed to the same directory, transferred nothing). An absolute `:PATH` is still honored verbatim, and the by-name-only form (no `:PATH`) still resolves to the workdir itself.

- Replace `@pytest.mark.flaky` with a modest `@pytest.mark.timeout(30)` bump on a set of `libs/mngr` CLI/API tests whose only observed flakiness was teardown/setup latency tripping the 10s default (not transient errors). A CI audit across recent runs found that every `@flaky`-marked test that actually flaked did so via `pytest-timeout`, all in the agent-create/clone/destroy/list/cleanup families that do real `mngr` work over tmux + subprocess. Affected tests: `test_cli_create_via_subprocess`, `test_clone_creates_agent_from_source`, `test_cleanup_destroy_single_agent`, `test_cleanup_destroy_with_provider_filter_matches`, `test_destroy_without_remove_created_branch_leaves_branch`, `test_list_command_with_{limit,limit_json_format,sort_descending,running_filter_alias,remote_filter_alias}`, `test_extras_claude_plugin_yes_flag`, `test_execute_cleanup_stop_on_online_host`, `test_list_agents_with_include_filter_excludes_non_matching`, `test_send_message_to_agents_with_include_filter`, and `test_create_with_update_flag_updates_existing_agent`. Reruns don't address latency, so a timeout bump is the appropriate remedy.

- Refresh a stale comment in `test_docker_state_transitions.py` that described the release tests as running "on release branch only". There is no `release` branch; these tests are marked `@pytest.mark.release` and run via the dedicated Release Tests workflow and TMR. No behavior change.

`mngr forward` gained an `--observe-via-file` flag: instead of spawning its own `mngr observe --discovery-only` subprocess, it tails the shared discovery events file written by another observer (e.g. the one `mngr latchkey forward` runs), so a host can run a single discovery observer. A new `tail_discovery_events_file` helper (a pure consumer that emits the latest cached snapshot then tails the log, without polling providers or writing snapshots) backs this and is shared with `run_discovery_stream`. The discovery event log now always lives under the default host dir: the internal `events_base_dir_override` config field was removed, and passing `--events-dir` together with `--discovery-only` to `mngr observe` is now a usage error (it never affected the discovery log in that mode). `--events-dir` still relocates the full observer's agent-state events.

Added `get_local_host(mngr_ctx)` to `imbue.mngr.api.providers` as the canonical way to obtain the local host (e.g. as an rsync/`copy_directory` source). Removed the duplicate `get_local_host` that lived in `imbue.mngr.cli.headless_runner`; callers now import it from `api.providers`. No user-facing behavior change -- this consolidates several copies of the same helper that had been independently reimplemented across plugins.

Added a `created_host(provider, host_name, **create_kwargs)` test context manager to `imbue.mngr.api.testing` that creates a host and destroys it on exit, replacing the create-host / try-finally-destroy boilerplate that recurs across provider tests. Test-only; no runtime impact.

Added a shared `upload_files_in_bulk` helper (`imbue.mngr.hosts.file_upload`) that transfers many files to a host in a single rsync (remote) or direct write (local), and routed `Host.provision_agent`'s user-upload and agent file-transfer loops through it instead of one `write_file` per file (a per-file SSH round-trip that did not scale -- github issue 1825). The rsync runs from the local machine via a new `copy_local_directory` host primitive (the source is implicitly the local machine, so no source-host object is threaded through `provision_agent` -- its signature is unchanged -- avoiding the host-layer-to-`api.providers` import cycle). The shared shell-library writes keep their per-file path because they need the executable bit, which the rsync staging helper does not preserve.

Fixed a deterministic `mngr create` regression on Modal (`AgentNotFoundOnHostError` immediately after provisioning). On Modal the host directory (`/mngr`) is a symlink into the mounted volume, and rsync without `--keep-dirlinks` deletes a receiver-side symlink-to-directory and replaces it with a real directory on the ephemeral filesystem when the source has a real directory at that path -- stranding `agents/<id>/data.json` (and all volume-backed state) behind the now-replaced symlink. `copy_local_directory` now passes `--keep-dirlinks` so rsync writes *through* such symlinks to the underlying storage, and `upload_files_in_bulk` rsyncs into the tightest common-ancestor directory of its destinations (hygiene, so it never stamps perms/mtimes on directories it is not deliberately writing into).

Fixed a `possibly-missing-submodule` type-checker warning in
`utils/cel_utils.py` by importing `MapType` directly from `celpy.celtypes`
instead of accessing it as `celpy.celtypes.MapType` (which relied on the
submodule being imported as a side effect). No behavior change.

## 2026-06-04

Discovery snapshots are now authoritative only for providers that succeeded on a given poll. The `FullDiscoverySnapshotEvent` contract changed: agents/hosts whose provider is in `error_by_provider_name` must be retained from prior consumer state (and surfaced as unknown/stale) rather than dropped, and are only removed on an explicit destroy event or a subsequent successful poll that omits them. Added a shared `partition_removed_agents_by_provider_error` helper that all discovery-snapshot consumers use to make this decision consistently. No discovery-event schema change.

- The e2e test suite under `libs/mngr/imbue/mngr/e2e/tutorial/` now has a release-marked pytest function for every executable command block in `mega_tutorial.sh`. `scripts/tutorial_matcher.py libs/mngr/imbue/mngr/resources/mega_tutorial.sh libs/mngr/imbue/mngr/e2e/tutorial` reports zero unmatched blocks and zero unmatched functions. Many new tests substitute lightweight command-type sleep agents for the tutorial's modal/claude examples so each test stays fast; substitutions are documented inline next to the `write_tutorial_block()` call.
- Additionally: pruned 5 stale duplicates from `tutorial/test_basic.py` (their canonical versions live in the per-section files), refreshed drifted `write_tutorial_block()` text in three existing modal/create tests, removed two tests whose corresponding tutorial blocks had been deleted or reduced to comments only (`test_create_modal_retry`, `test_create_with_transfer_git_mirror`), and moved `test_create_and_rename_agent` out of the tutorial subdir to the top-level e2e directory since the RENAMING AGENTS section is now informational-only.

- The tutorial-tied e2e tests under `libs/mngr/imbue/mngr/e2e/` (the ones with `write_tutorial_block()` calls corresponding to blocks in `mega_tutorial.sh`) have moved into a `tutorial/` subdirectory, so other e2e tests can live at the top level without the tutorial-matcher script (and other tools) treating them as tutorial gaps.

## 2026-06-03

`mngr create --format jsonl` (and every other command) now emits a structured
error record when a command fails:

```json
{"event": "error", "error_class": "FastPathUnavailableError", "message": "..."}
```

Previously a failing command only printed a human-formatted `Error: <message>`
line with no machine-readable type, so subprocess callers (e.g. minds) had to
substring-match the error text to detect specific failures -- which silently
broke when the error surfaced cleanly without the class name in a traceback. The
top-level CLI exception handler now calls `emit_error_event(...)` for real
errors (not control-flow exits like Ctrl-C / `--help`) when the resolved output
format is JSONL, attaching the exception's class name. `on_error` likewise
includes `error_class` in its JSONL error event when given the exception.

`mngr create --new-host` now tears down the host it just created if a later step
fails (provisioning, agent start, etc.), so a failed create never leaks the
host. Previously the only cleanup was removing the host lock so idle-shutdown
providers could reclaim the host on their own -- which never helped providers
that disable idle shutdown (e.g. imbue_cloud pool leases), leaving the host (and
its lease) stranded. The teardown is gated by the existing
`MNGR_DEBUG_RETAIN_LOCK_FOR_FAILED_HOSTS_DURING_CREATE=1`, which now retains the
failed host (not just its lock) for debugging.

`mngr observe --discovery-only` now honors `--events-dir`. Previously the flag was silently ignored for discovery-only mode, which always wrote and read the single shared discovery event log under `MNGR_HOST_DIR`. Now `--events-dir DIR` relocates the whole discovery stream (snapshots, host SSH info, and discovery errors) under `DIR`, so multiple discovery-only observers can run against the same host without their snapshots cross-contaminating each other's streams. Implemented via a new internal `MngrConfig.events_base_dir_override` field that only changes where event files live; provider/account/auth resolution still uses `default_host_dir`.

Regenerated the `mngr imbue_cloud` CLI reference docs to include the new operator-only `mngr imbue_cloud admin paid domain|email add|remove|list` commands for managing the connector's paid-user lists.

## 2026-06-02

Internal refactor with no user-visible behavior change. Removed the `emit_final_json` helper from `imbue.mngr.cli.output_helpers`, which was a one-line pass-through to the private `_write_json_line`. The underlying primitive is now public as `write_json_line` (sibling to the existing `write_human_line`), and all call sites use it directly.

The old name was misleading: despite "final JSON" implying the single terminating object emitted in `--format=json` mode, it was also called from the streaming JSONL callbacks (e.g. `mngr list`). `write_json_line` honestly describes what it does -- write one JSON object as a line -- for both the JSON and JSONL paths.

New `mngr stop --stop-host` flag: stops an agent's whole host (every
agent on it) instead of just the named agent.

- For container-backed providers `--stop-host` stops the container while
  the underlying machine keeps running; it is rejected up front on
  providers that do not support stopping hosts, and cannot be combined
  with `--archive`.
- `--stop-host` is idempotent: if the host is already offline it reports
  success instead of raising an error, so restarting an already-stopped
  workspace works.
- `--stop-host` now resolves the target host without SSH. Previously it
  failed with an `SSH error (Error reading SSH protocol banner...)` when
  the host's container was still running but its sshd was unreachable
  (sshd crashed, or PID exhaustion blocked new SSH sessions) -- one of
  the cases `--stop-host` is meant to handle. The host_id is now resolved
  from the discovery event stream and then fetched through the provider's
  own SSH-free `get_host` (which validates that the host still exists and
  supplies its name), so `mngr stop <agent> --stop-host` followed by
  `mngr start <agent>` reliably bounces an unresponsive workspace. A
  single SSH-free lookup against the one relevant provider both validates
  and names the host, so resolution does not scan every provider's hosts
  up front, and does not replay host discovered/destroyed events.
- This supports the minds tiered workspace-restart recovery flow, which
  uses a full host restart as its heavier recovery tier.
- When `--stop-host` targets multiple hosts, they are now stopped
  concurrently (via a concurrency-group executor) instead of one at a
  time, so the command no longer serializes on the slowest host. Output
  order is unchanged. If one host fails to stop, the others are still
  stopped before the error is raised (the previous sequential version
  aborted on the first failure, leaving later hosts running).

Fix `mngr list --format json` crashing on `ProviderErrorInfo` when no
agents were returned. With `--on-error continue` and a per-provider
failure, the empty-agents path passed raw `ErrorInfo` pydantic models to
`json.dumps`, which crashed with `TypeError: Object of type
ProviderErrorInfo is not JSON serializable`. The empty-agents path now
goes through the same `_emit_json_output` serializer as the non-empty
path, so `mngr list --on-error continue --format json` produces a clean
`{"agents": [], "errors": [...]}` payload instead of a traceback.

- Fix indefinite hang in git-push / rsync over SSH on macOS hosts where `SSH_AUTH_SOCK` routes to 1Password's biometric SSH agent. The shared `build_ssh_transport_command` (used by git push and rsync) now pins authentication to the explicit `-i` key via `-o IdentitiesOnly=yes -o IdentityAgent=none`. Without these flags, OpenSSH consults `SSH_AUTH_SOCK` first; in BatchMode (no TTY) the biometric prompt can never fire and ssh blocks forever on the agent reply.

`AgentError` (and all of its subclasses, e.g. `NoCommandDefinedError`, `AgentNotFoundOnHostError`,
`SendMessageError`, `AgentStartError`) now inherit from `MngrError` instead of `BaseMngrError`.
The remaining `BaseMngrError`-only error types -- `PluginSpecifierError`,
`DiscoverySchemaChangedError`, `MalformedJsonlLineError`, `TolerantPathError`, and
`IssueSearchError` -- were moved the same way. This completes the consolidation of the error
hierarchy under a single user-facing parent class: every mngr error is now a `ClickException`,
so when one reaches the CLI it renders as a clean `Error: ...` message (plus any help text)
instead of a Python traceback, and `except MngrError` handlers treat them as the user-facing
errors they are.

The now-redundant `MngrError` mix-in on `AgentNotFoundError` and `DuplicateAgentNameError`
(which already reached `MngrError` via `AgentError`) was removed; both still behave identically.

The `BaseMngrError` base class has been removed entirely. `MngrError` now inherits directly
from `click.ClickException`, and every mngr error inherits from `MngrError`. There is no longer
a separate non-user-facing error tier: all mngr errors render as a clean `Error: ...` message
at the CLI (plus any help text) rather than a traceback. This is a no-op for users -- prior
commits had already moved every error class under `MngrError`; removing `BaseMngrError` simply
finalizes that consolidation.

`except` clauses that listed an error type already covered by another type in the same clause
were collapsed (e.g. `except (MngrError, UserInputError)` -> `except MngrError`,
`isinstance(e, (MngrError, BaseMngrError))` -> `isinstance(e, MngrError)`). Clauses pairing
`MngrError` with unrelated types (`OSError`, `docker.errors.*`, etc.) are unchanged.

`HostError` (and all of its subclasses, e.g. `HostConnectionError`, `HostOfflineError`,
`HostAuthenticationError`, `CommandTimeoutError`, `HostDataSchemaError`) now inherit from
`MngrError` instead of `BaseMngrError`, consolidating the error hierarchy under a single
user-facing parent class. Host errors are now `ClickException` instances, so when one reaches
the CLI it renders as a clean `Error: ...` message (plus any help text) instead of a Python
traceback, and `except MngrError` handlers treat them as the user-facing errors they are.

The base `get_host_and_agent_details` now re-raises `HostConnectionError` from its per-agent
guard so that, even though `HostConnectionError` is now a `MngrError`, a connection failure
still reaches the host-level handler that clears the connection cache and falls back to the
offline view instead of being swallowed per-agent.

Install Node.js in the shared mngr image (`resources/Dockerfile`), pinned to `apps/minds/package.json`'s `engines.node` (24.15.0). Node is a runtime dependency of the mngr_latchkey gateway's `.mjs` extensions, and is also used by minds Python tests that evaluate `apps/minds/todesktop.js` via `node`. With Node in the image, those tests run on offload instead of being silently skipped.

## 2026-06-01

# Make `CreateAgentOptions.agent_type` required

- `CreateAgentOptions.agent_type` is now a required field (previously
  `AgentTypeName | None` defaulting to `None`). Following the removal of
  the CLI's implicit `claude` default, the residual `agent_type or
  AgentTypeName("claude")` fallbacks in `api.create.create` and
  `Host.create_agent_state` were the last places that silently defaulted
  an unset type to `claude`. Both fallbacks are gone, and the type system
  now guarantees every agent-creation path supplies a concrete type. The
  now-dead `if options.agent_type is not None:` guard around agent-type
  provisioning merging in `Host.provision_agent` was also dropped.

Marked `test_list_command_with_limit` as flaky so offload retries it automatically.

The `on_before_create` and `on_before_host_create` plugin hooks now receive the `MngrContext` as a parameter, giving plugins access to config, the plugin manager, and the concurrency group. Plugins implementing these hooks must add a `mngr_ctx` parameter to their signatures.

Plugins can now contribute standalone help topic pages via the new `register_help_topics` hook. Topics from an installed plugin show up in `mngr help` and are viewable via `mngr help <topic>`, just like mngr's built-in topics. Each topic is a `TopicHelpPage` with explicit metadata (key, description, aliases, see-also) whose body is either `InlineContent(markdown=...)` or `DocFile(path=...)`. Plugin topics that collide with a built-in topic key or alias are skipped so built-in topics always win.

`mngr help <topic>` now renders markdown nicely in an interactive terminal (headings, bold, code, links, and tables) via `rich`, with paragraphs wrapped to the terminal width; the same rendering is applied to command `--help` description and sections. Non-interactive output (pipes, scripts) stays plain. `rich` is imported lazily so it does not affect CLI startup time.

Built-in topic docs are now shipped inside the wheel (`force-include` of the topic doc dirs), fixing a bug where `mngr help <topic>` showed no doc-based topics in a PyPI/wheel install (only the top-level `docs/` tree, which is not packaged, was previously read at runtime).

Tab completion suggests every command and help topic as an argument to `mngr help` (e.g. `mngr help <TAB>` lists `create`, `address`, and any plugin-contributed topics).

Internally, the plugin-facing `TopicHelpPage` model lives in `imbue.mngr.interfaces.help_topic` (so the plugin hookspec can reference it without the plugins layer importing the CLI), while the runtime topic registry lives in `imbue.mngr.cli.help_topics`. mngr's own built-in topics are registered through this same hook (as a built-in plugin) from an explicit registry -- no directory scanning or heading parsing.

In an interactive terminal, `mngr help <topic>` now makes relative and anchor links inside doc-backed topics clickable. Previously a link like `[Idle Detection](idle_detection.md)` or `[a section](#user-input-tracking)` rendered as a dead terminal hyperlink (its relative target means nothing to a terminal or browser).

Each topic's `DocFile` now carries a `source_url` (the doc's canonical GitHub blob URL, pinned to the installed release tag, e.g. `.../blob/v0.2.9/...`, falling back to `main` when the version can't be read). At display time, relative and anchor links are resolved against that URL with `urljoin` (`#anchor` -> `<doc-url>#anchor`, `sibling.md` -> the sibling's URL, `../README.md#x` -> the parent's URL), so the rendered terminal hyperlinks open the right GitHub page/section. Already-absolute links (`https:`, `mailto:`) are left untouched, and plain non-terminal output (pipes, scripts, the doc generator) keeps the original relative links.

Plugins get this for free: a plugin that builds its `DocFile` with a `source_url` (in-repo plugins can use the new `imbue_mngr_doc_url` helper) gets the same clickable-link rewriting; one that omits it simply renders its links unchanged.

# Offline agent field generators

Implemented the plugin hook previously documented as the planned `get_offline_agent_state`, now named `offline_agent_field_generators` to mirror the existing online `agent_field_generators` hook.

- Plugins can now contribute `plugin.<plugin_name>.<field>` data for agents whose host is offline or unreachable. Each generator receives the offline `(DiscoveredAgent, HostDetails)` (rather than the live `(agent, host)` the online hook gets) and computes fields from the cached `data.json` exposed via `DiscoveredAgent.certified_data`. `None` field values are omitted and empty plugins are dropped, exactly like the online path.
- `mngr list` collects these generators and threads them through `get_host_and_agent_details` to `build_agent_details_from_offline_ref`, so offline plugin fields are usable in `mngr list` columns and CEL filters just like online ones.
- Discovery snapshots now preserve plugin fields: `discovered_agent_from_agent_details` carries `AgentDetails.plugin` into the reconstructed `certified_data`, so offline generators can still read plugin state for fully-unreachable hosts that fall back to a persisted snapshot.
- Updated the plugins concept doc to document `offline_agent_field_generators` and remove the `[future]` `get_offline_agent_state` placeholder.
- Test infrastructure: the `assert_home_is_temp_directory` safety check now also accepts `/private/tmp`, so tests run when `TMPDIR` points into `/tmp` (which macOS realpath-resolves to `/private/tmp`) rather than only the launchd `/var/folders` default.

## 2026-05-30

Tolerate per-host SSH failures during provider agent enumeration.

A single unreachable host (sshd hang, banner reset, auth failure) used to make the default `discover_hosts_and_agents` raise a `HostConnectionError` from the per-host futures loop. That bubbled up to `_construct_and_discover_for_provider` and was recorded as a whole-provider failure, so the resulting `DISCOVERY_FULL` event reported `agents=[]` / `hosts=[]` for the entire provider. Downstream, `mngr_forward`'s resolver blanked its known-agents set and every workspace on that provider became unreachable through the forward plugin -- so one broken Docker container could 503 every other workspace on the same daemon and trip minds' recovery page even for perfectly healthy workspaces.

The default `discover_hosts_and_agents` now catches `HostConnectionError` (and its `HostAuthenticationError` / `HostOfflineError` subclasses) per host and recovers the broken host's agents from the provider's offline view (described below) so the rest of the provider's hosts (and their agents) come through normally. The except block also calls `self.on_connection_error(host_id)` -- matching the contract honored elsewhere in the file -- so providers that cache per-host state (docker's container cache, modal/lima/vps_docker host caches) drop the wedged entry instead of replaying the same stale handle on the next discovery cycle.

Per-host connection errors fall back to the provider's offline view (`to_offline_host(host_id).discover_agents()`). This means a docker container whose sshd has died but whose process is still RUNNING preserves its agents in discovery, mirroring the behavior of a fully-stopped container -- the workspaces stay visible on minds' landing page instead of vanishing on the first SSE poll after sshd dies. The fallback assumes any provider whose hosts can raise `HostConnectionError` implements `to_offline_host`: the local provider never opens a connection (so it never reaches this path), and remote providers persist host/agent state. If `to_offline_host` instead fails -- `NotImplementedError` (no offline view) or `HostNotFoundError` (no persisted record for a host that was just discovered) -- that signals a broken invariant and is allowed to propagate as a per-provider `ProviderDiscoveryError` rather than being masked as an empty agent list. The SSH provider does not yet implement `to_offline_host` (tracked by a FIXME on the class), so an unreachable SSH host currently surfaces as such an error.

## 2026-05-29

- Docker provider: the per-host build image (`mngr-build-<host_id>`) is now untagged when a host is destroyed (and again, defensively, when it is deleted), so built images no longer pile up in `docker images`. Snapshot restore is unaffected -- snapshot images keep their own layers.

Regenerated the CLI reference docs to include the new `mngr imbue_cloud bucket`
command group (R2 bucket + scoped-key management) added by the mngr_imbue_cloud
plugin.

`mngr destroy` now actually destroys the host when the last agent on it
is destroyed -- the documented contract -- regardless of how recently the
host was created. Previously this fired through the post-destroy GC pass,
which gates on `min_online_host_age_seconds` (default 10 minutes), so any
host destroyed within minutes of creation leaked its cloud-side resources
(e.g. an active imbue_cloud lease, a Vultr VPS) until the 7-day
destroyed-host grace period eventually triggered `provider.delete_host`.

Two changes in `destroy.py`:

1. **Partition step reconciles discover-vs-on-host disagreement.** When
   every matched agent is a "ghost" -- returned by the provider's discover
   but absent from the host's own `get_agents()` -- the destroy CLI now
   escalates to host-level destruction (`provider.destroy_host`) instead
   of silently dropping the match. This is what was producing the
   "No agents found" message when the same agent was destroyed twice on
   an imbue_cloud-leased host: the first destroy removed `/mngr/agents/<id>/`
   on the VPS but the connector's lease list still reported the agent.

2. **Post-loop sweep destroys hosts whose last agent was just destroyed.**
   For each online host that had at least one agent destroyed in this
   invocation, the destroy CLI now re-checks `host.get_agents()` and, if
   empty, calls `provider.destroy_host` directly. Bypasses the GC's
   `min_online_host_age_seconds` filter; the GC pass that runs immediately
   after is the safety net for transient failures.

Net effect: cloud-side resources are released the moment `mngr destroy`
returns, and the destroyed-host grace period only retains historical
state -- aligning all provider types with the same semantic that the
docker / mngr_vps_docker / imbue_cloud `destroy_host` implementations
already implement individually.

## New: `--post-host-create-command`

`mngr create` learns a new repeatable flag, `--post-host-create-command`,
that runs one or more shell commands inside a newly-created host
synchronously after the host is online but before any agent work_dir is
touched. Each command runs in order via the host's normal exec path; a
non-zero exit aborts the create. Stackable from `create_templates.<name>`
via `post_host_create_command__extend = [...]`.

Motivation: an image may need first-boot setup (e.g.
forever-claude-template seeds a baked workspace from `/docker_build_code`
onto its `/mngr/` volume) that must complete before mngr's git mirror
push or any other work_dir setup. Until now this had to be encoded in the
container's `CMD`, which raced against mngr's `docker exec` calls and
required an FCT-specific `use_image_default_cmd` opt-out in
`mngr_vps_docker` / `providers.docker`. The opt-out and the
defensive `--workdir /` exec override (from commits `d77714cdf` /
`55c420c35`) are reverted in the same commit -- the new generic hook
replaces both.

Install `restic` in the mngr Docker image (`libs/mngr/imbue/mngr/resources/Dockerfile`).

This is the offload test image; the minds app now requires `restic` on the
machine running it (it initializes each workspace's backup repository
itself), and its tests exercise a real local restic repository, so restic
must be present in the test environment.

`build_check_and_install_packages_command` (in `providers/ssh_host_setup.py`) now
`mkdir -p` the symlink target before creating the `host_dir` symlink. The local
`docker` provider already creates that subdirectory eagerly so it's a safe no-op
there; the new docker_vps unified-volume layout relies on the in-script mkdir to
seed `<volume>/host_dir` before pointing `/mngr` at it.

## 2026-05-28

# `is_allowed_in_pytest` now defaults to False, and the `MNGR_ALLOW_PYTEST` escape hatch is gone

The `is_allowed_in_pytest` config field now defaults to `False` (previously
`True`). During a pytest run, `load_config` refuses to run when a config file is
loaded that does not set `is_allowed_in_pytest = true` -- and every config layer
(user/project/local) is checked individually, so a real config can't ride in
under a test config that opts in. If no config file is picked up at all, there
is nothing to protect against and mngr runs normally. This makes the guard
secure by default: a real config (the developer's `~/.mngr` or the repo's
`.mngr/settings.toml`) loaded by a poorly-scoped test now trips the guard
instead of being used to perform real operations, while configs written
specifically for tests opt in explicitly.

The `MNGR_ALLOW_PYTEST=1` environment variable, which used to bypass the guard
entirely, has been removed. It had a single user, and the existence of such a
variable was not worth the risk of it being reached for as a quick bypass
instead of properly fixing a test with a leaky environment.

# Corrected `is_error_reporting_enabled` config field description

Separately, the `is_error_reporting_enabled` field description was out of date
(it described prompting to file GitHub issues); it now matches the actual
behavior -- suggesting a diagnostic agent on an unexpected interactive error.

## Clearer settings-narrowing errors

- The "settings narrowing detected" error now names **both** implicated layers for each offending key: the file doing the (narrowing) assignment and the lower-precedence file whose value would be dropped. Each side shows the resolved file path and the matching `mngr config set --scope <user|project|local>` flag, so it is immediately clear which configs conflict (instead of just an opaque layer label and the dotted key). The `MNGR__*` env-var layer is named as such (it has no `config set` scope).
- `--setting allow_settings_key_assignment_narrowing=...` is now rejected with a clear error. That flag controls the narrowing guard, which runs while the settings files and env vars are loaded — before `--setting` is applied — so a `--setting` value could not take effect there. The error points you to set it in a `settings.toml` or via `MNGR__ALLOW_SETTINGS_KEY_ASSIGNMENT_NARROWING=true` instead. (Previously the narrowing error misleadingly suggested `--setting` as a remedy, and a `--setting` value for the flag was silently accepted without affecting that guard.)

### Restructure `mngr push` and `mngr pull` into `mngr rsync` and `mngr git push`/`mngr git pull`

The experimental `mngr push` and `mngr pull` commands combined three different
primitives (rsync, git push, git pull) behind `--sync-mode={files,git}` and
`--rsync-only` flags. They are replaced by three thin primitives that each wrap
a single operation:

- `mngr rsync SOURCE DESTINATION` — wraps rsync. Exactly one of `SOURCE` /
  `DESTINATION` must reference a remote agent or host; the other must be a
  local path. Local-to-local and remote-to-remote transfers are rejected.
- `mngr git push TARGET [-- GIT_ARGS...]` — thin wrapper around `git push`
  from the current working directory's repo to a remote agent or host's repo.
  Anything after `--` is passed verbatim to the underlying `git push`.
- `mngr git pull SOURCE [-- GIT_ARGS...]` — thin wrapper around `git pull`
  from a remote agent or host's repo into the current working directory's
  repo. Anything after `--` is passed verbatim to the underlying `git pull`.

The git push/pull commands are thin pass-through wrappers: mngr resolves the
endpoint, builds the SSH URL with mngr's managed credentials, sets
`receive.denyCurrentBranch=updateInstead` on push targets, and adds a
`safe.directory` entry — then runs vanilla `git push` / `git pull` with any
flags the user supplies after `--`. The mngr-side flags
`--source-branch`/`--target-branch`/`--mirror`/`--uncommitted-changes`/`--dry-run`
are gone; use the corresponding git flags directly (`feature:main` refspec
syntax, `--force --tags refs/heads/*:refs/heads/*` for a mirror push,
`--dry-run`, `--rebase`, etc.).

`mngr push` and `mngr pull` are removed (no compatibility shim).

API-level changes in `imbue.mngr.api.sync`: `pull_files`/`push_files`/`pull_git`/`push_git`
are replaced by `rsync_from_remote`, `rsync_to_remote`, `git_pull`, and
`git_push`. There is also a top-level `rsync(source_host, source_path,
destination_host, destination_path, ...)` for the two-endpoint shape used by
the CLI. `git_push`/`git_pull` now take an `extra_args: Sequence[str]`
parameter and have no structured return value (raise `GitSyncError` on
failure). The `SyncMode` enum, `GitSyncResult`, and `NotAGitRepositoryError`
are gone; `SyncFilesResult` is renamed to `RsyncResult`.

# Offload pin bump to v0.9.6

Bumped the offload version baked into `libs/mngr/imbue/mngr/resources/Dockerfile`
from `0.9.5` to `0.9.6` to keep the in-image offload binary in lockstep
with the CI pin. v0.9.6's headline feature is the new
`offload run --override-image-id <ID>` CLI flag (Modal provider only),
which lets a test run skip offload's image-setup pipeline entirely and
boot from a pre-built Modal image. See
https://github.com/imbue-ai/offload/releases/tag/v0.9.6 for the full
release notes.

# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

No user-facing behavior change.

## 2026-05-27

## Settings narrowing safety net: two false-positive fixes

- A brand-new `[create_templates.<name>]` block whose only ``<opt>__extend = [...]`` entry was introduced in a single layer used to silently lose its `__extend` suffix at config-load time (`resolve_extends` resolved against a `None` base lookup and stored a bare assign). At `mngr create --template <name>` time the option was then treated as a bare assign and tripped the narrowing guard against the create command's runtime params, even though the user had written `__extend`. The suffix is now preserved verbatim inside a template's options when the base has nothing to extend, so `apply_create_template` can still apply it as an extend against the runtime params.
- `agent_types.<name>.cli_args = "..."` (string form) used to be shell-tokenized into a tuple before the narrowing check ran, so two layers that each supplied a string with different tokens tripped the narrowing guard against each other's individual tokens. Strings represent a coherent single value (scalar replacement intent), not an aggregate, so the narrowing check now exempts tuples that were normalized from string-form values. This applies to every `tuple[str, ...]` field on `AgentTypeConfig` that accepts the string shorthand (`cli_args`, `env`, `env_file`, `extra_provision_command`, `upload_file`, `create_directory`).

- Added `isolate_host_volumes` to the Docker provider config. When set to `True`, each host container only sees its own per-host sub-folder of the shared state volume (via `--mount ... volume-subpath=...`, requires Docker Engine >= 25.0), fixing the cross-host visibility that today's shared mount has. The choice is persisted per host so existing hosts keep the strategy they were created with.
- Left at its default of unset (treated as today's shared-volume behavior), the provider now emits a one-shot deprecation warning at startup noting that the default will flip to `True` in a future release. Set `isolate_host_volumes = false` explicitly to silence the warning while keeping today's behavior, or `isolate_host_volumes = true` to opt in to isolation now.

# ty 0.0.39 / paramiko 4.0 / coolname 5.0 type fixes

- Converted bracketed `# type: ignore[...]` suppressions to `# ty: ignore[...]`, as required by `ty` 0.0.39.
- coolname 5.0 widened `RandomGenerator`'s config type; the name-generator config dicts are now annotated with `coolname.CoolnameConfigT` so they type-check (coolname's value type is invariant).
- The new `types-paramiko` stubs (from the paramiko 4.0 bump) surfaced several paramiko usages in `outer_host`:
  - `_get_paramiko_transport` / `_create_sftp_client` are now typed as returning/accepting `paramiko.Transport` (was `object`).
  - The private `_put_file*` helpers are narrowed from `str | IO[str] | IO[bytes]` to `str | IO[bytes]`; only `IO[bytes]` was ever passed, and `SFTPClient.putfo` requires bytes.
- The e2e `pytest_runtest_makereport` hookwrapper's generator send type is now annotated as `pluggy.Result[pytest.TestReport]`, so `outcome.get_result()` resolves.
- Intentional reaches into paramiko internals (the `Transport._log` logging monkeypatch and a `Channel._send` access in a test that manufactures a traceback) are annotated with `# ty: ignore[unresolved-attribute]`.

- Tightened this project's `test_ratchets.py` violation counts to their exact current values (`--inline-snapshot=trim`).

No user-facing behavior change.

## 2026-05-26

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

# Regenerated CLI docs

The generated `mngr latchkey` command reference
(`libs/mngr/docs/commands/secondary/latchkey.md`) was regenerated to match the
command's current help metadata. A new CI check now fails if any generated CLI
doc drifts from `uv run python scripts/make_cli_docs.py` output.

Replaced the `gemini` entry in `PLUGIN_CATALOG` and `plugin_isolation` test fixtures with `antigravity`. The `AntigravitySignalCheck` detects the new CLI via `agy --version`. Touched-up some prose references in `mngr` resource scripts and docs to drop or refresh mentions of `mngr_gemini` now that the plugin has been renamed.

## Breaking: unified settings overrides

Any mngr config field can now be overridden from a single, unified mechanism:

- **`MNGR__X__Y__Z=value` env vars** (note the double underscores) target the dotted path `x.y.z`. This replaces the narrow `MNGR_COMMANDS_<CMD>_<PARAM>` scheme and frees plugin / CLI command names to contain multiple words. `MNGR_COMMANDS_*` is **removed**.
- **`--setting x.y.z=value`** and **`mngr config set x.y.z value`** continue to work and now go through the same resolver.
- **`__extend` operator suffix on a leaf key** (e.g. `MNGR__AGENT_TYPES__MY_CLAUDE__CLI_ARGS__EXTEND='["--model","opus"]'`) opts into additive behavior: append for lists/tuples, shallow key-merge for dicts, union for sets. The bare key is always assignment.
- **`mngr config extend KEY VALUE`** writes the `__extend` form; **`mngr config set KEY__extend VALUE`** is accepted as an alias.
- **`mngr config schema`** lists every settable key with type and current effective value; **`mngr config list --all`** includes default-valued fields too.

### Breaking changes you'll notice

- **Layer merging is now assign-by-default for every aggregate** (list, tuple, dict, set). Older configs that relied on implicit concat across user/project/local files (e.g. `cli_args` accumulating) now need an explicit `cli_args__extend = [...]` to keep the additive behavior. The five top-level container dicts on `MngrConfig` (`agent_types`, `providers`, `plugins`, `commands`, `create_templates`) keep their per-key merge — adding `[agent_types.foo]` in one scope still doesn't drop another scope's `[agent_types.bar]`. `disabled_plugins` is a separate carveout: it is populated by `--disable-plugin` CLI flags rather than TOML files, and an empty override preserves the base value (use `[plugins.<name>] enabled = false` to disable a plugin per-scope).
- **Agent-type parent-type inheritance** likewise stops auto-concatenating `cli_args` / `extra_provision_command` / `upload_file` / `create_directory` / `env` / `env_file`. Use `field__extend` to inherit-and-extend.
- **Removed env vars:** `MNGR_COMMANDS_<CMD>_<PARAM>`, `MNGR_ENABLE_PARAMIKO_LOGGING`, `MNGR_AGENT_READY_TIMEOUT`. These are promoted to first-class config fields (`logging.enable_paramiko_logging`, `agent_ready_timeout`) and remain settable via `MNGR__*`.
- **`MNGR_COMPLETION_CACHE_DIR` stays as-is** (single underscore). It's read by the tab-completion lightweight pre-reader path that intentionally skips full config loading, so it joins the "special" env vars (`MNGR_ROOT_NAME` / `MNGR_PREFIX` / `MNGR_HOST_DIR`) rather than becoming a config field. The double-underscore `MNGR__COMPLETION_CACHE_DIR` form is not recognised.
- **Renamed:** `MNGR_RETAIN_LOCK_FOR_FAILED_HOSTS_DURING_CREATE` → `MNGR_DEBUG_RETAIN_LOCK_FOR_FAILED_HOSTS_DURING_CREATE`.
- **Preserved aliases:** `MNGR_ROOT_NAME`, `MNGR_HOST_DIR`, `MNGR_PREFIX`, and `MNGR_HEADLESS` continue to work. Setting both an alias and its canonical `MNGR__*` form to different values raises `ConfigParseError`.
- **Field name restrictions:** field names can no longer contain `__` (reserved as the env-var segment separator and `__extend` operator). Sibling keys that lowercase-collapse to the same env-var segment now raise at config-load time.

No compatibility shim is provided; the major-version bump is the migration signal.

## Narrowing safety net and CLI-flag extension

- CLI tuple/list flags (e.g. `--env`, `--label`, `--extra-window`) now extend the merged settings value rather than replace it, with the config-supplied entries first and the CLI-supplied values appended. Matches the "settings file → CLI" precedence so users can layer additional values on top of a settings-supplied list.
- `--setting commands.<name>.<param>__extend=...` and `--setting create_templates.<name>.<param>__extend=...` now correctly extend the merged value stored inside the per-entry `defaults` / `options` mapping (previously fell through to `None` and silently acted as a plain assign for these wrapper models).
- The `__extend` operator and the narrowing safety net both apply uniformly to `agent_types.<name>.<field>`, `providers.<name>.<field>`, `create_templates.<name>.<field>`, and `plugins.<name>.<field>` -- not just to top-level `MngrConfig` fields. Adding a brand-new entry in a higher layer is always a pure addition (no narrowing); replacing or clearing a non-empty aggregate value within an existing entry follows the same opt-in / `__extend` workaround rules as the top-level fields.
- Create templates (applied at command time via `--template <name>`) also follow the assign-by-default / `__extend` / narrowing rules. Previously templates concatenated tuple options by default, which silently mixed CLI values into the template's narrowing base and made `--template a --template b` chains hard to reason about when both wrote the same key. Now templates assign-by-default and a template's bare-assign over a non-empty value raises `ConfigParseError` unless `allow_settings_key_assignment_narrowing = true`; opt-in to additive behavior with `[create_templates.<name>] env__extend = [...]`. The pipeline order is now `config_defaults -> templates -> CLI`, so a CLI flag value always appends at the end of the merged list, after any template extension.
- Added a new top-level `allow_settings_key_assignment_narrowing` setting (default `false`). When `false`, a higher-precedence settings layer that would assign over a non-empty list/tuple/dict/set value from a lower-precedence layer with anything that doesn't preserve every prior entry raises `ConfigParseError` instead of silently dropping entries. The error tells the user how to opt in (set the field to `true`) or keep the additive behavior for the specific key (use the `__extend` suffix). The default is expected to flip to `true` in a future version, and support for `false` may be removed entirely once the migration is complete. Only no-ops (override equals base) and supersets (every base entry survives, e.g. an `__extend` result) pass without flagging — clearing (`env = []`) is treated as the most extreme form of data loss and must be explicitly opted in. Layers that don't write the field at all never trigger the guard.

Fix a class of bugs where tmux commands silently misroute to the wrong agent's session under prefix collision.

When `tmux ... -t name` is invoked and no session named exactly `name` exists, tmux falls back to *session-name prefix matching* and routes the command to any live session whose name starts with `name`. If two agents have names where one is a prefix of the other (e.g. `gemini` and `gemini-to-antigravity`), then when the shorter-named agent is torn down, every subsequent `-t gemini` lookup silently lands on `gemini-to-antigravity` instead of failing. Possible consequences include:

- `kill-window` / `kill-session` tearing down the wrong agent's session
- `send-keys` / `paste-buffer` delivering input to the wrong agent
- `capture-pane` reading the wrong agent's screen
- Lifecycle checks misreporting a stopped agent's state (the symptom that first surfaced this — a stopped agent shown as `WAITING` because the check landed on a live sibling's pane)
- Background-task polling loops never terminating

Changes:
- Introduce `TmuxSessionTarget` and `TmuxWindowTarget` Pydantic classes in `imbue.mngr.hosts.tmux` whose `.as_shell_arg()` renders the `-t` argument with a leading `=` (tmux's exact-match prefix), and for window/pane commands the required explicit `:window` component.
- Route every tmux `-t` call site through the helpers: lifecycle check, send-keys / paste-buffer / capture-pane in `BaseAgent`, post-attach resize script in `connect.py`, `_build_start_agent_shell_command` in `host.py`, rename / kill / has-session paths, the `listing_utils` remote-listing script, and the TUI input pipeline.
- `build_post_attach_resize_script` now iterates windows so SIGWINCH reaches every pane in every window (previously only the active window's). Side effect of the refactor; not strictly required for the prefix-matching fix.
- Update `cleanup_tmux_session` (in `utils/testing.py`) to match the new `=<session>:0` exact-match form when pkill-cleaning orphaned activity monitors — the old substring no longer appeared in the monitor's command line after the helper refactor.
- Add unit tests in `hosts/tmux_test.py` covering the helpers' rendering contract. Live behavioral coverage of the polling-loop-never-terminates failure mode lives in the per-project regression tests under `libs/mngr_claude` and `libs/mngr_gemini`.

## 2026-05-22

## Discovery snapshots now carry per-provider state

- `FullDiscoverySnapshotEvent` (the JSONL event emitted by `mngr observe --discovery-only`) has two new fields: `providers` (providers that loaded successfully) and `error_by_provider_name` (providers whose discovery raised). Old snapshots parse cleanly (the new fields default to empty); new snapshots will trip `DiscoverySchemaChangedError` in older builds of `mngr_forward` / `mngr_latchkey` / `mngr_notifications` until those are rebuilt.
- `mngr observe --discovery-only` now emits a `FullDiscoverySnapshotEvent` on every poll, even when zero providers succeeded. Per-provider failures land in `error_by_provider_name`; consumers treat the snapshot as authoritative and drop any previously-known agents/hosts whose provider is now errored.
- `mngr list` no longer skips its side-effect snapshot when some providers failed; the snapshot now includes the per-provider error info. Snapshots are still skipped when a non-provider-attributable error happens at the top level of `list_agents`.
- Bug fix: the outer `mngr observe` (the multi-host observer) used to spawn its inner `mngr observe --discovery-only` child with an unsupported `--on-error continue` flag, killing the child on every startup. The flag is now gone.

## New `UNKNOWN` agent / host lifecycle state

- `AgentLifecycleState` and `HostState` both grow an `UNKNOWN` value, defined as "the provider that owns this agent/host could not be accessed during the most recent discovery attempt."
- `AgentObserver` now emits an UNKNOWN entry in its `FullAgentStateEvent` for any previously-observed agent whose provider just failed discovery (sticky: agent stays UNKNOWN until it reappears in a snapshot or its provider is removed from config). Agents whose provider falls out of configured set entirely are dropped from tracking instead.
- `mngr list` does NOT show UNKNOWN -- it remains stateless and only shows what its own listing returned.

## Retry semantics

- The discovery polling path no longer retries failures at the top level. Providers are responsible for retrying their own transient failures before raising; the snapshot reflects whatever they reported.

## 2026-05-21

Removed the `mngr provision` (aka `mngr prov`) subcommand and its docs. Provisioning still runs automatically during `mngr create`; the `--extra-provision-command`, `--upload-file`, and env-related flags on `mngr create` continue to work as before.

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

- `mngr create --provider lima` docs now show `--memory=N` / `--disk=N` (plain integers, no `GiB` suffix), matching what `limactl start` expects.

Show the full agent name in the tmux status bar.

User-visible changes:

- mngr's generated tmux config (`~/.mngr/tmux.conf`) now sets
  `status-left-length` to 20 so a full `mngr-...` session name shows in the
  status bar. Previously tmux's default of 10 truncated names like
  `mngr-tmux-display` to `[mngr-tmux`, with the window list mashed onto the end.
- The widening is written before the user's `~/.tmux.conf` is sourced, so a
  `status-left-length` set in the user's own config overrides it.

`Host._get_all_descendant_pids` now tracks a `visited` set so a PID-reuse cycle in the process tree (a long-lived pid X dies and is recycled as a descendant of one of its own descendants) can no longer drive the walker past Python's recursion limit. This unsticks `host.stop_agents` on long-lived agents' cleanup paths, which previously crashed with `RecursionError` and skipped the actual stop.

## 2026-05-20

Renamed the mngr-side "workspace server" feature to "system interface", matching the upstream rename of the `minds_workspace_server` package to `system_interface` in `forever-claude-template`. The HTTP endpoint `/api/agents/{id}/restart-workspace-server` became `/api/agents/{id}/restart-system-interface`, and the SSE event type `workspace_server_status` became `system_interface_status`.

## Provider gating: only `mngr create` may bootstrap host-creation state

`mngr list`, `mngr gc`, and other read flows no longer silently bootstrap
provider-side state just because a provider is enabled. Plumbed through a new
`is_for_host_creation: bool = False` parameter on
`ProviderBackendInterface.build_provider_instance` / `api.providers.get_provider_instance`,
which all backends accept and ignore by default. `mngr create` passes `True`;
every other path leaves the default. Providers that can't initialize without
their environment (e.g. Modal) now raise `ProviderUnavailableError`, which
higher-level loaders skip.

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

# Consistent agent address resolution across single-agent subcommands

Refactored how single-agent subcommands turn an `AgentAddress` into the live
interfaces they operate on. The "find" stage (discovery + matching against
the address) is now strictly separate from the "ensure live" stage (bringing
the host online, looking up the live agent, optionally starting it).

Two new helpers in `imbue.mngr.api.find` replace the previous
`is_start_desired` / `skip_agent_state_check` flags on
`find_one_agent` / `find_agent_for_command`:

- `resolve_to_started_host_and_agent`: bring the host online and resolve
  the agent ref to an `AgentInterface` without checking the agent's
  lifecycle state. Used by `push`, `pull`, `provision`, and `rename`.
- `resolve_to_started_host_and_running_agent`: as above, but also
  require / auto-start the agent process. Used by `connect` and `capture`.

Both helpers take a single `allow_auto_start` flag (driven by `--start`).

User-visible changes:

- `push`, `pull`, and `provision` no longer require the agent to be
  running. Previously they failed when targeting a stopped agent on an
  online host; now they operate on stopped agents directly.
- `push`, `pull`, `provision`, and `rename` gain a `--start/--no-start`
  flag (default `--start`) that controls whether an offline host is
  started automatically.
- The `--start` help text on `connect`, `capture`, and `exec` has been
  reworded to reflect what `--start` actually starts in each command.
- `mngr connect` no longer falls back to "most recently created agent"
  when run non-interactively without an explicit agent. It now matches
  every other single-agent command: pass an agent name, or run it from
  an interactive terminal to use the selector.
- Cancelling the interactive agent selector now exits cleanly via
  `click.Abort` instead of printing nothing and returning silently.

- `mngr list` no longer aborts with "Provider 'modal' is not available"
  when the Modal per-user environment hasn't been created yet. The
  Modal backend now raises a new `ProviderEmptyError` (distinct from
  `ProviderUnavailableError`) when its env doesn't exist, and the
  listing pipeline silently skips empty providers in every mode
  (streaming + batch, ABORT + CONTINUE). Semantically: empty means
  "the backend answered that there's nothing here" and is always safe
  to drop from a listing; unavailable means "we couldn't ask" and may
  still warrant an error.

Support a shared Modal env across an offload-acceptance / offload-release
run (opt-in via `MNGR_TEST_SHARED_MODAL_ENV_NAME`). `imbue.mngr.utils.testing`
gains a `read_shared_modal_env_name` helper that returns the shared env
name when the env var is set (and a non-empty dash-suffixed value), or
`None` otherwise. Used by the modal test fixtures to skip per-sandbox env
creation/deletion and route all tests into a single pre-created env, so
fanned-out offload runs stay well under Modal's per-workspace env cap.
Local pytest behavior (no env var set) is unchanged.

# `HasTranscriptMixin` formalises the raw-capture contract for `mngr transcript`

A new `HasTranscriptMixin` on `AgentInterface` formalises the raw-capture
contract; `HasCommonTranscriptMixin` extends it with the (gated) common
converter on top. Future agent types get `mngr transcript` support for free
by implementing `get_raw_transcript_scripts` + `get_common_transcript_scripts`
and shipping the matching per-agent scripts.

Fix `mngr config` help text and docs example: the example showed `--user` but the actual option is `--scope user`.

# Rename `HostedLocation` to `HostLocationAddress`

Renamed the address-side `HostedLocation` type to `HostLocationAddress` so its
name matches its peers (`HostAddress`, `AgentAddress`) and makes its
relationship to the runtime `HostLocation` type explicit.

Cascading internal renames:

- `parse_hosted_location` -> `parse_host_location_address`
- `resolve_hosted_location` -> `resolve_host_location_address`
- `ResolvedHostedLocation` -> `ResolvedHostLocationAddress`
- `HostedLocationParamType` -> `HostLocationAddressParamType`
- `HOSTED_LOCATION` (Click param type instance) -> `HOST_LOCATION_ADDRESS`
- Click param-type display name `hosted_location` -> `host_location_address`
  (visible in command-line help / docs for `mngr push`, `mngr pull`,
  `mngr pair`)

No behavior change.

Add `--restart` and `--no-resume` flags to `mngr start`.

- `mngr start my-agent --restart` stops a running agent and starts it fresh. If the agent is already stopped, it is simply started.
- `mngr start my-agent --no-resume` skips sending the resume message after starting. Can be combined with `--restart`.

### `mngr create` honors the adopt scenario (for imbue_cloud lease flows)

- minds passes `--reuse` for IMBUE_CLOUD agent creates. The bake's services agent is now named `system-services` too, which mngr's pre-flight "agent already exists on this host" check would otherwise reject. `--reuse` is necessary to signal that the lease's pre-baked agent isn't a duplicate-name collision. (`--update` is intentionally NOT passed: the adopt path in `ImbueCloudHost.create_agent_state` already patches labels + command in place; running standard provisioning on top would re-do the file-transfer + provisioning round the bake already paid for.)
- `mngr` core's duplicate-agent-name check in `api/create.py` now honors `host.pre_baked_agent_id`. With just `--reuse` the check still fired because `--reuse`'s lookup runs BEFORE `resolve_target_host` fires the lease, so the leased host's agent isn't in the operator-local mngr state yet to be reused. The pre-flight check now skips the raise when the existing agent's id matches the host's `pre_baked_agent_id` -- that's the lease-adopt scenario by design and `host.create_agent_state` knows how to hydrate the existing agent in place.
- `pre_baked_agent_id` is hoisted onto `HostInterface` as a `None`-defaulted frozen field, so the check in `api/create.py` reads `host.pre_baked_agent_id` directly (no `getattr` shim that would trip the `prevent_getattr` ratchet). Providers whose `create_host` returns a host with a baked-in agent (`ImbueCloudHost` is the only one today) populate it; every other provider's hosts default to `None` and the duplicate-name check's prior behavior is preserved.

- `ProviderError` now carries `provider_name` on the base class. Every subclass (`HostNotFoundError`, `HostNameConflictError`, `HostNotRunningError`, `HostNotStoppedError`, `SnapshotNotFoundError`, `TagLimitExceededError`, `ImageNotFoundError`, `LocalHostNotStoppableError`, `LocalHostNotDestroyableError`, `LimaHostCreationError`, etc.) now requires `provider_name` as its first constructor argument. Handlers that catch `ProviderError` can read `e.provider_name` without isinstance-narrowing to a specific subclass.

Removed the unused notion of agent "permissions" from `mngr` itself. The `Permission` primitive, `AgentPermissionsOptions`, `NoPermissionsAgentMixin`, and `get_permissions`/`set_permissions` methods on host and agent interfaces have all been deleted, along with the `--grant`/`--revoke` flags on `mngr limit` and the `--grant` flag on `mngr create`. Agent type configs no longer accept a `permissions` list. Higher-level libraries (latchkey, minds) keep their own (real) permissions concepts.

`mngr rename` now works against offline hosts: when the agent's host is
not online, the rename (and any `-l KEY=VALUE` labels) are written to
the provider's persisted agent data without starting the host. The
`--start/--no-start` flag still exists but now defaults to `--no-start`;
pass `--start` to force the host online first so tmux and the env file
are updated alongside data.json.

Add `@pytest.mark.timeout(60)` to two flaky tests in `libs/mngr/imbue/mngr/cli/test_destroy.py`: `test_destroy_via_stdin` and `test_destroy_multiple_agents`. Both spin up two `sleep` agents, wait for tmux sessions, run a parallel destroy, and wait for cleanup -- a workload that lands at or over the global 10s pytest-timeout under modal-offload contention. The first exhausted all 5 `@pytest.mark.flaky` retries on PR #1652; the second has flaked in the same CI runs. The multi-agent shape is the point of both tests (piping multiple names through stdin is the main use case for `destroy -`), so the workload cannot be shrunk. Matches the existing precedent on `test_destroy_transfer_none_keeps_shared_worktree`.

Adds shell-level integration tests for `scripts/install.sh`. The existing install tests build a venv that simulates what install.sh produces, but never invoke the script itself. The new `test_install_script.py` runs `bash scripts/install.sh` against mock `uv` and `mngr` binaries on a synthetic PATH and verifies the control flow: `uv tool upgrade` vs `uv tool install` branches, the PATH-not-set error path, and the continue-on-failure (`|| warn`) behaviour of `mngr dependencies -i` and `mngr extras -i`. No real PyPI install or system dependencies are required, so the tests run in under three seconds with no network access.

`mngr create --type X` now fails fast with `UnknownAgentTypeError` when `X` does not resolve to a registered agent class (either directly via a plugin/built-in registration, or via a `[agent_types.X]` block whose `parent_type` points to a known type), instead of silently resolving to a generic `BaseAgent` + empty config. A bare `[agent_types.X]` block without `parent_type` is also rejected. Use `--type command -- <shell command>` to run an arbitrary shell command. The `--type X -- ...` form is no longer a hidden alias for `--type command -- ...`.

## 2026-05-15

Restore Modal compatibility for the standard mngr Dockerfile and adopt offload's `post_patch_cmd` (introduced in v0.9.4). The Dockerfile is back to a single `FROM python:3.12-slim` stage (mngr's Modal image builder rejects multi-stage Dockerfiles), and all source-dependent setup (tarball extraction, git normalization, `image_commit_hash`, `uv sync`) lives in `scripts/post-source-setup.sh`, called both as the final Dockerfile RUN and as offload's `post_patch_cmd` so the two paths stay in sync. Bumps the offload pin from 0.9.2 to 0.9.5.

## 2026-05-14

`mngr create` no longer hard-codes `claude` as the default agent type. The agent type must now come from a positional argument, `--type`, or `[commands.create] type` in user settings. If none of those is supplied, `mngr create` exits with a clear error listing the registered agent types and pointing at `mngr config set commands.create.type <name> --scope user`. (Supersedes the `--type` source-default introduced in `mngr-fix-default.md` from this same release.)

New subcommand `mngr extras config`: walks through user-scope config settings the installer would otherwise leave blank. Each step short-circuits if its setting is already configured, so re-running only prompts for the gaps. Today this just covers the default agent type for `mngr create`; future config-related setup will be added as additional walk steps under the same subcommand. With an interactive terminal, presents an urwid single-select picker of every available agent type plus a "Keep no default" option, and writes the selection to `[commands.create] type` in user settings. With `-y` or without a terminal, prints the suggested `mngr config set` command and lists available agent types -- writes nothing.

`mngr extras completion` and `mngr extras claude-plugin` also use the new urwid picker (Install / Skip) instead of the old `[y/n]:` text prompt -- the entire `mngr extras -i` walkthrough now uses a consistent TUI rather than mixing the plugin-wizard's full-screen TUI with bare-text confirmation prompts.

`mngr extras -i` now also walks through the default agent type prompt as a final step, alongside the existing plugins / completion / Claude-plugin steps. `mngr extras` (no flag) reports the current default agent type as part of the status block.

`scripts/install.sh` no longer contains custom shell logic for the default agent type -- step 5 is gone, since the default agent type prompt now runs as part of `mngr extras -i` (step 4). The new subcommand is also re-runnable via `mngr extras config` if you skip it the first time.

`mngr plugin list` gains a `--kind` filter with two values, `agent-type` and `provider`, that project the plugin list to the canonical set of agent type names or provider backend names (with version/description metadata when entry-point names match).

Migration: existing users who upgrade and have no `[commands.create] type` set will see an error from `mngr create` until they run `mngr config set commands.create.type <name> --scope user` (or pick one via `mngr extras config`). The error message includes the registered agent types so you can copy-paste a value.

Bumped the pinned Claude Code CLI version from `2.1.116` to `2.1.141` in `libs/mngr/imbue/mngr/resources/Dockerfile`.

`mngr schedule add --verify quick|full` now works when the trigger's `mngr create` produces an agent that lives inside the cron-runner's local provider (i.e. inside the ephemeral Modal container). Verification now runs inside the container itself and reports the result back to the deploy machine over a structured sentinel line.

Fix tmux argv-parsing footguns for arguments starting with `-`:

- `tmux send-keys -l` now uses the `--` end-of-options separator, so agent commands and messages that start with `-` (e.g. `--model gemma`, `--help`) are no longer misparsed by tmux as flags.
- `tmux rename-session` now uses `--` before the positional new-name argument, so renaming an agent under a custom prefix that starts with `-` works correctly.

## 2026-05-14

`mngr list --format json`: the redundant `address` field on agent and host
records is no longer emitted. The same value is still reachable on the
parsed Python objects as `AgentDetails.address` / `HostDetails.address`,
and removing it from the wire format lets the output round-trip cleanly
through `AgentDetails.model_validate_json` (which previously rejected the
extra key).

`Volume`: gains a `path_exists(path)` method implemented across all
providers (local, Docker, Modal) and the `ScopedVolume` wrapper. Callers
no longer need to fall back to `listdir` and catch
provider-specific not-found errors to probe for a single file's
existence.

## 2026-05-12

# Address parsing refactor

The four shapes of address strings that mngr accepts on the command line are now
represented as separate typed values, parsed once at the CLI boundary and
threaded through the API layer as typed objects rather than raw strings.

## New address types

In `imbue.mngr.primitives` (with parsers in `imbue.mngr.api.address_parsers`):

- `HostAddress` — `HOST[.PROVIDER]` (or bare `.PROVIDER` for the new-host hint
  used by `mngr create`).
- `AgentAddress` — `NAME[@HOST[.PROVIDER]]`. Used by `mngr connect`,
  `mngr destroy`, `mngr exec`, etc. The agent component is required.
- `NewAgentLocation` — `[NAME][@HOST[.PROVIDER]][:PATH]`. The positional
  argument of `mngr create`. The name is optional (auto-generated if omitted)
  and is parsed strictly as `AgentName` (not an agent ID).
- `HostedLocation` — `[NAME[@HOST[.PROVIDER]]][:PATH]` or a bare path.
  Designates "a location on any host", local or remote. Used as the source
  argument of `mngr create --from`/`mngr pair` and as the target argument of
  `mngr push`/`mngr pull`.

Two new type aliases (in `imbue.mngr.primitives`) capture the
"name-or-id" notion at the type level:

- `AgentNameOrId = AgentId | AgentName`
- `HostNameOrId = HostId | HostName`

## CLI parses at the Click level

The Click `ParamType` adapters in `imbue.mngr.cli.address_params` (`AGENT_ADDRESS`,
`HOST_ADDRESS`, `NEW_AGENT_LOCATION`, `HOSTED_LOCATION`, `AGENT_NAME_OR_ID`,
`HOST_NAME_OR_ID`, `AGENT_NAME`) attach to `@click.argument` and
`@click.option`, so command bodies receive typed addresses directly. The
api/ layer takes the typed objects; the cli/ layer no longer holds parsing
logic that the api/ layer also needs.

## User-visible behavior changes

- **`mngr push`/`mngr pull` now accept `@HOST[.PROVIDER]:PATH` syntax** in their
  `TARGET`/`SOURCE` argument. Previously these commands had a bespoke parser
  that only understood `AGENT[:PATH]`, so a fully-qualified address like
  `mngr push my-agent@m1.modal:/path` failed with a "host filter not supported"
  error. The shared `HostedLocation` parser unifies this with the rest of the
  CLI.
- **`HostName` no longer permits dots.** Host names are validated as
  `SafeName` (alphanumeric + dashes/underscores). The `HOST.PROVIDER`
  qualifier is now exclusively the parser's responsibility -- previously
  `HostName` also carried `.provider_name` and `.short_name` properties that
  split on the dot, which contradicted the principle that "host names do not
  contain dots, so the dot is a deterministic separator."

## Internal cleanup

The following functions / types are removed; callers should use the typed
replacements:

- `parse_address_part`, `parse_host_qualifier`, `parse_source_string`,
  `parse_identifier_as_address`, `ParsedSourceLocation` -> replaced by the
  composite parsers in `api/address_parsers.py` and FrozenModel types in
  `primitives.py`.
- `find_and_maybe_start_agent` -> deleted; was redundant with
  `find_one_agent` (callers all did `discover_*` + the call). Its
  matching logic now lives in `filter_one_agent` (now also raises a
  helpful "Multiple agents found ... disambiguate using NAME@HOST.PROVIDER"
  message listing each colliding agent). Its materialization logic now lives
  in a small `materialize_agent(host_ref, agent_ref, mngr_ctx)` helper that
  callers can reuse when they already have discovered refs.
- `find_one_agent` no longer takes a `command_name`; the disambiguation
  hint in the multi-match error no longer embeds the CLI command name.
- `find_agent_for_command` no longer takes a `command_usage` argument and
  now merges its optional `host_filter` into the agent address upfront
  (raising if address and filter pin different hosts).
- `parse_agent_spec` (cli helper for `mngr push`/`pull`) -> deleted; use
  `parse_hosted_location` / the `HOSTED_LOCATION` ParamType.
- `_host_matches_filter` -> replaced by the `HostAddress.matches` method.
- `api/agent_addr.py` was folded into `api/find.py` and `api/discover.py`; with
  typed addresses as the norm, there is no longer a reason to segregate
  address-accepting functions.
- The api-level `find_one_agent`, `find_all_agents`,
  `discover_by_address`, `filter_one_host` (formerly
  `resolve_host_reference`, now tightened to require a non-None
  `HostAddress`), `filter_one_agent` (formerly
  `resolve_agent_reference`, now tightened to require a non-None
  `AgentNameOrId`), `filter_all_hosts`, `filter_all_agents`,
  `exec_command_on_agent(s)`, etc., all take typed addresses now instead of
  raw strings.
- `AgentDetails.address` and `HostDetails.address` expose the corresponding
  typed addresses as cached properties, so callers can pass them directly to
  api functions instead of reconstructing addresses from individual fields.

Improve the error message when an `[agent_types.<name>]` block in `mngr.toml` contains an unknown field. Previously the message only listed the unknown fields and valid fields, which looked like a typo report even when the real cause was a missing plugin. The message now includes a hint suggesting that the plugin providing the agent type may not be installed (mirroring the existing hint shape used for providers), and lists currently disabled plugins when relevant.

Encode the actual defaults for `mngr create` options that previously listed a default in their help text but were stored as `None` and resolved at runtime: `--type` now defaults to `"claude"` directly, and `--start-on-boot` defaults to `False`. Also corrects the `--worktree-base-folder` help text to reflect the actual default location (`<host_dir>/worktrees`).

Behavior change: when a config file (`[commands.create]`) or template sets `type` and the user passes a positional `AGENT_TYPE` on the command line, the positional now wins (matching the general "CLI > config" precedence). Previously the config-supplied `type` won, and a mismatch raised a "Conflicting agent types" error.

- Fixed: `mngr clone <agent> <new-name>@.<provider>` (and `mngr migrate` for cross-host moves) now succeeds when the source and destination agents live on different hosts. Previously the plugin-state rsync passed the destination host as both source and target, so rsync ran on the destination sandbox and failed with `change_dir "/.../plugin" failed: No such file or directory` because the source plugin dir only exists on the source agent's host. `CreateAgentOptions` now carries the source agent's location (host + state dir) as a single `HostLocation`, and `_transfer_source_plugin_data` rsyncs from that host to the destination -- so the Claude transcript, session history, and memory carry over for local->remote, remote->local, and remote->remote clones alike.

## 2026-05-11

- Fixed: `mngr list` / `mngr kanpan` no longer log a per-agent
  `WARNING: Error evaluating ... no such member in mapping: 'X'` when an
  `--include` / `--exclude` filter references a key on a schemaless
  field (`labels`, `plugin`, `host.tags`, `host.plugin`) that some
  agents happen not to have. The filter now quietly evaluates to false
  on those agents.
- `has(labels.foo)` (and the same for keys under `plugin`, `host.tags`,
  `host.plugin`) is now the recommended presence-check idiom for those
  schemaless fields, and is shown in the `mngr list --help` examples.
  Note: `labels.foo != null` does NOT work as a presence check on
  tolerant fields -- use `has(...)`.
- Filters against typoed strict fields (e.g. `host.providr` instead
  of `host.provider`) still surface a warning so users can see the
  typo.
- Bumps the `cel-python` minimum to `>=0.5.0` so the dev environment
  matches the version the global `mngr` install picks up. Earlier
  versions (e.g. 0.4.0) folded `host.providr == "local"` style misses
  silently to false instead of warning, so the strict-typo warning
  surface was narrower than intended on the locked version.

Remove a stale "(NOT IMPLEMENTED YET)" marker on the `provider_names` parameter of `imbue.mngr.api.list.list_agents`. The filter has long been wired through both batch and streaming codepaths into `list_provider_names_to_load`; the parameter-doc inline comment had not caught up.

Demote two internal trace log lines emitted during `mngr create` from INFO to DEBUG. The `_setup_per_agent_config_dir: agent=... host.is_local=True ...` and `_write_generated_files: host.is_local=True, ...` lines were leaking into user stdout on every create; they are diagnostic-only and now only appear under `MNGR_LOG_LEVEL=debug` or `-v`. The normal create output is now just `Creating agent state... / Starting agent X... / Sending initial message... / Done.`.

## 2026-05-10

- Fixed a spurious "Duplicate host name '127.0.0.1' found on provider 'docker'" warning that fired on `mngr list` when multiple Docker containers were running against a local Docker daemon. `Host.get_name()` now returns the mngr-assigned host name from the host's certified data instead of the SSH connector hostname. Use the new `get_connector_host_name()` accessor when the literal connector address is needed.

## 2026-05-09

- `mngr message <agent> -m /clear` (and `-m /compact`) no longer hangs for 90 s before returning. The `mngr-submit-<session>` tmux signal that `mngr message` waits on is now also fired from the SessionStart hook when the source is `clear` or `compact`, since those TUI-local slash commands do not trigger UserPromptSubmit.

- Changed: bumped the default `docker build` timeout for the docker provider from 5 to 10 minutes, and made it configurable per provider instance via the new `build_timeout_seconds` field (e.g. `mngr config set --scope user providers.docker.build_timeout_seconds 1800`).
- Improved: when a `docker build` exceeds the configured timeout, mngr now raises a clear `DockerBuildTimeoutError` that names the timeout and points at the config knob, instead of surfacing a generic non-zero-exit `ProcessError` that hid the cause behind a wall of build output.

Fixed `mngr gc` crashing on hosts whose SSH host key is missing from `known_hosts`. Such errors now raise `HostAuthenticationError` (a trust failure) instead of the generic `HostConnectionError`, so existing per-host gc handlers skip them with a warning instead of aborting the whole run.

- `mngr plugin add` no longer warns about unknown config fields and unknown provider backends when the local config references plugins that haven't been installed yet (those warnings would resolve themselves the moment the install completes; they were just noise)
- New `silent` flag on `_check_unknown_fields`, `_parse_providers`, `_parse_agent_types`, `_parse_plugins`, `_parse_retry_config`, `_parse_logging_config`, and `parse_config` suppresses the warnings when paired with `strict=False`. `load_config` and `setup_command_context` expose the same via a `silent_unknown_fields` parameter. Default behavior on every other command is unchanged.

## 2026-05-08

- Fixed: `mngr stop` no longer leaves orphaned child processes alive on Linux when the agent's pane process (e.g. `claude`) was killed abruptly (SIGKILL, OOM). The pane-descendant walk previously missed grandchildren that had reparented to PID 1 -- typically `playwright-mcp`, `node`, or other long-lived helpers -- so they survived `mngr stop` and accumulated, consuming memory across stop/start cycles. `Host.stop_agents` now also enumerates processes by their inherited `MNGR_AGENT_ID` env var via `/proc/<pid>/environ`, catching these orphans regardless of process tree.

Accept the `host.provider` qualifier consistently anywhere mngr takes a host
identifier (previously only the positional `agent@host.provider` form worked).
For example, `mngr create --from @m1.modal:/some/path`, `mngr limit --host
m1.modal`, `mngr snapshot create --host m1.modal`, and the `--host` filter on
list-style commands now all resolve `host.provider` deterministically.

Also fix `mngr create --from` against a remote source: the current-branch
lookup, and the related git-author/origin-URL lookups in remote-source flows,
now run through the host interface so they work for any provider rather than
raising `NotImplementedError`.

`mngr create --from` between a source and a target on the same remote host
now works: the git-mirror push short-circuits to a single in-host `git push`
between two local paths (no SSH), and `--transfer=git-worktree` is allowed
whenever source and target are on the same host -- previously it was
restricted to local-only agents. The default transfer mode for same-host
remote git sources is now `git-worktree` instead of `git-mirror`.

The internal rsync helper used for `work_dir_extra_paths` and the rsync
transfer mode also short-circuits when source and target are on the same
host, running a single rsync between two local paths on that host instead
of routing through a laptop temp directory.

- Fixed: `mngr create` now provisions credentials correctly inside nested sandboxes (e.g. a Linux lima VM running on a macOS host). `get_user_claude_config_dir()` previously returned `$ORIGINAL_CLAUDE_CONFIG_DIR` even when that path (a host-side path like `/Users/<user>/.claude`) did not exist inside the VM, causing `_provision_local_credentials` to log "No .credentials.json found to provision" and silently no-op. Spawned child agents then failed Claude sessions with "Not logged in". The helper now falls back to `$CLAUDE_CONFIG_DIR` when `$ORIGINAL_CLAUDE_CONFIG_DIR` does not resolve to an existing directory, so credential provisioning (and every other call site that resolves user-scope config) finds the live per-agent credentials.

Add a Concise spec under `specs/expose-outer-host/concise.md` for
exposing each host's outer machine (the VPS / docker daemon host /
local machine hosting a container). Restructures the host class
hierarchy so `OuterHost` becomes the base class with the minimal safe
API (file ops, command execution, locking, env vars, SSH info) and the
existing `Host` extends it to add agent / lifecycle / snapshot / tag
machinery. Adds an optional context-manager-based accessor on
`OnlineHostInterface` and `ProviderInstanceInterface` that yields
`OuterHostInterface | None`. Surfaces via a new `mngr exec --outer`
flag that dedups by outer host so the command runs once per unique
outer; `--missing-outer abort|warn|ignore` (default `warn`) controls
behavior when targeted agents have no accessible outer. The existing
one-off SSH paths (`mngr_imbue_cloud/vps_admin.py`,
`mngr_vps_docker/docker_over_ssh.py`) will be deleted and migrated to
the new abstraction. Modal, `local`, `ssh`, and docker-over-tcp return
`None` (no accessible outer). No code changes yet — spec only.

## 2026-05-07

- Fixed a perpetual one-event lag in remote (online-host) event
  follow-mode tailing. When reading `events.jsonl` over the wire,
  pyinfra's `CommandOutput.stdout` is built by `"\n".join(...)` after
  each line is `rstrip("\n")`-ed, so a file ending in `\n` and one not
  ending in `\n` produced identical strings. The follow loop's
  partial-write guard then treated the most recent complete line as
  in-flight and held it back until a later line forced it out. The
  remote read now wraps `cat` as `{ cat <file> && printf '%s' <sentinel>; }`,
  asserts the sentinel is present, and strips it back off to recover
  the file's exact byte tail.

## 2026-05-04

- Fixed: local-host shell commands issued from worker threads (e.g. inside `mngr observe --discovery-only`, `mngr list`, `mngr message`, `mngr destroy`, `mngr gc`, `mngr discover`) no longer crash with `TypeError: child watchers are only available on the default loop` on Linux. `Host._run_shell_command` now bypasses pyinfra's gevent-backed `LocalConnector` for local hosts and runs commands via the `ConcurrencyGroup` process runner.

## 2026-05-02

- JSONL parsers now surface upstream corruption rather than silently dropping bad lines
  - `MalformedJsonLineWarner.parse` raises `MalformedJsonlLineError` on lines that parse but aren't JSON objects (e.g. `[1,2,3]`); valid-but-incomplete JSON is still buffered as a possible end-of-file partial write
  - `parse_event_line`, `parse_discovery_event_line`, `parse_agents_from_mngr_output`, and `_parse_batched_json_files` (vps_docker) all raise on malformed input instead of returning `None`; rationale: stdout is for JSON data, stderr is for logs, and silently skipping garbage hides real upstream bugs
  - New `MalformedJsonlLineError` exception in `imbue.mngr.errors`
- Fixed: `resolve_provider_names_for_identifiers` no longer silently returns partial results when an identifier is unknown; it returns `None` to signal a full discovery scan is needed (regression introduced in the merge that combined the two parsing-fix branches)
- Fixed: `mngr connect` no longer fails type-checking; the two `build_agent_filter_cel` call sites now pass the required `cg` and `project_root` arguments to match `mngr list` and `mngr kanpan`
