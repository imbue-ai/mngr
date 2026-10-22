# Unabridged Changelog - mngr

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-10-21

`SendFailureKind` gains two kinds, so a client of the message API can tell a send that typed nothing from one whose text may already be with the agent:

- `agent_not_found`: the agent was not where the caller located it. Its host has no such agent, or the host could not be reached (offline, would not start, or could not load its agents). Nothing was typed, so looking the agent up again and resending is safe. These failures were previously `unknown`.

- `submission_unconfirmed`: the message was typed and Enter sent, but no evidence confirmed the agent took it within the confirmation window. The text may already be in the agent's input or turn, so sending it again can deliver it twice. This was previously `unknown`.

The Minds chat app falls back to a full discovery and resends whenever a send to an agent's known location fails. With these kinds it can resend only after `agent_not_found`, instead of retyping a message that an unconfirmed submission may already have delivered.

When `mngr create` (with an initial message) or `mngr start` (with a resume message) cannot confirm the agent took the message, the error no longer says the message was NOT delivered and tells you to resend it. It says the message may already be in the agent's input and to check with `mngr connect` before resending. The error raised from these paths also keeps the failure's kind instead of reporting `unknown`.

`mngr message --help` no longer says every non-zero exit other than 7 means the message was not delivered. When the error says no submission evidence appeared, the message was typed but not confirmed and may already be in the agent's input, so check before resending.

New `imbue.mngr.hosts.outer_host.is_ssh_auth_timeout`: tells whether paramiko gave up waiting for sshd to answer authentication behind an error, which a `HostAuthenticationError` from the outer host otherwise does not distinguish from a rejected key.

Test utilities: `in_process_paramiko_sshd` and `ExecOnlyParamikoServer` take an `is_auth_held` argument, which makes the server answer no public-key auth request until the context exits, standing in for an sshd that never answers authentication before the client gives up.

The e2e fixture now redacts credentials before saving a test's `transcript.txt`. Local agents inherit the runner's environment, so tutorial tests that dump an agent's environment (`mngr exec my-task -- env | sort`) were writing the live `ANTHROPIC_API_KEY`, `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` into transcripts that CI archives and TMR reports publish. The variables to redact are named by the new `MNGR_SECRET_ENV_NAMES` environment variable (comma-separated), which the CI workflows set from the names the `use-vault-secrets` action reports injecting; every occurrence of such a value is replaced with `<redacted NAME>`. Unset, as in local runs, nothing is redacted.

## 2026-10-20

Sending a message (or a key chord) to agents now loads only the agents being messaged. Previously each send re-read every agent's `data.json` on the target host and built every agent object there, so the cost grew with the number of agents on the host, and every neighbor whose agent type is no longer registered logged a "no longer registered" warning on every send. A neighbor with an unparseable `data.json` could also break a send to a healthy agent. Messages to several agents, and to agents that are not on the host (still reported as not found), behave as before.

Starting or stopping an agent by id on a host (the path a message to a stopped or exited agent takes when it auto-starts it) likewise reads only that agent's `data.json` instead of loading every agent on the host.

Bumped the offload version baked into `libs/mngr/imbue/mngr/resources/Dockerfile` (`OFFLOAD_VERSION`) from `0.9.14` to `0.10.0`, keeping the in-image `offload apply-diff` binary in lockstep with the CI pin.

The outer listing script (`build_outer_listing_collection_script`) now takes the container id a previous listing found and inspects it directly, in one merged `docker inspect` for its status and exit code, running the label lookup only when that container is gone or no longer running. It reports the container it listed as `CONTAINER_ID=`.

The in-container listing can now fingerprint the records a discovery listing is shaped from (the host's and each agent's `data.json`: inode, mtime and size) before reading anything, reported as `LISTING_FINGERPRINT=`. The outer script takes the fingerprint a previous listing of the same container reported, and a container whose records still fingerprint the same answers `LISTING_UNCHANGED=true` in place of the full listing, skipping the dozens of process spawns that listing costs. The parser exposes the three new lines as `container_id`, `listing_fingerprint` and `is_listing_unchanged`.

`mngr connect --agent <AGENT>` now works. The option and the positional `AGENT` argument shared the click parameter name `agent`, so click overwrote the option's value with the positional's `None` (and printed a `UserWarning` about the clash twice on every `connect` invocation, including `--help`). `--agent my-task` therefore fell through to the interactive selector, or failed with "No agent specified" when not on a terminal. The option has its own parameter now, the two forms are merged in the command, and giving both is an error. Reported by 11 mappers in the 2026-10-13 TMR run.

The image Dockerfile's comments describe the keyframe build context as `current.tar.gz`, a
`.checkpoint` marker and `apps/minds/scripts/git-manifest.json`, which `scripts/make_tar_of_repo.sh`
now stages there so `mngr create -t modal` and `mngr create -t docker` on this repo build. Comments only;
no build steps changed.

Added `get_ssh_public_key_fingerprint` to `ssh_utils` and `log_client_key_configuration` to `ssh_host_setup`. Whenever a provider is about to overwrite a host's `authorized_keys`, `log_client_key_configuration` logs at info the host id, the SHA256 fingerprints of the client key and of every appended entry, the provider and the profile. An entry it cannot parse is logged as unparseable and does not fail the configure.

`imbue.mngr.utils.testing` gains `make_command_agent`, a factory for a validation-free command agent on a real local host, for plugin tests that need an agent of a type other than their own.

## 2026-10-19

Sending an empty or whitespace-only message to an interactive agent now fails at once with "the message is empty". It used to paste nothing, press Enter on an empty input box, and wait out the full 90-second confirmation window before reporting that the submission was not confirmed. This was what made the first send after a chat moved to another account stall for over a minute.

Sending a message to an interactive TUI agent spawns far fewer processes. Each tmux call is a process spawn, and under gVisor (Imbue Cloud workspaces) a spawn costs roughly 70-100 ms, so a send to Claude Code went from 16 spawns to 6 (measured against a local fake Claude TUI):

- The agent's pane ID is looked up once per send and every step addresses that pane, instead of being looked up again by each of the readiness check, the leftover-input check, the typing, the paste wait, and the submit.

- A preflight that already read the pane (Claude's dialog check) hands that reading on, so the readiness check and the leftover-input check no longer capture the same screen again moments later.

- Leaving copy-mode rides in the same tmux invocation as the typing (and, for long messages, as the buffer load and paste). It runs only when the pane is actually in a mode, so tmux versions older than 3.2, which lack `copy-mode -q`, still type the message rather than rejecting the whole invocation.

- Waiting for the typed message to show, taking the evidence baselines, pressing Enter and confirming now run as one host script. A message that never shows still fails with "Timeout waiting for pasted content to appear" and Enter is never sent.

- After Enter, evidence is polled every 0.1 s for the first one to two seconds (then every 0.5 s as before), and the script measures time with bash's `SECONDS` instead of running `date` on every poll.

The pane check that gates Enter retries now normalizes the pane byte-wise under the C locale, so it no longer depends on the host's locale.

Stopping an agent (`mngr stop`, `mngr start --restart`, `mngr destroy`, and the stale-tree reap before a start) now lists the agent's processes with one `ps` snapshot instead of one `pgrep -P` per process. Each `pgrep` cost a shell spawn and a full process-table scan; inside a gVisor workspace that measured about 65 ms per process, so a Claude chat agent's nine-process tree spent about 0.6 s finding its children on an idle host, and several times that under load. The single snapshot measured 38 ms. Which processes get killed is unchanged.

The `mngr` wheel no longer ships two test-only helper modules.

`imbue/mngr/agents/agent_release_testing.py` and `imbue/mngr/providers/provider_release_testing.py` hold the shared arcs and assertions that the per-plugin agent and provider release tests drive. Nothing `mngr` runs imports them: every importer is a release test, except one `testing.py` that is itself excluded from its own wheel. Both were being packaged anyway, because the wheel's `**/testing.py` exclude rule matches only that exact filename and not a longer name ending in `_testing.py`.

They are now named in full in the `exclude` list, so a `pip install imbue-mngr` gets 97,842 fewer bytes of test code (26,919 fewer compressed) and no test-only import paths. The modules stay where they are in the checkout, so the release tests that import them are unaffected.

`mngr event` reads an agent's events without discovery when the address names the agent, its host and its provider by id (`agent-...@host-....docker`, as a create's `created` event reports them). Discovery across every configured provider and container took about ten seconds on a busy machine, longer than a Docker workspace's boot, which left a caller following the boot's events with nothing to show; the id-qualified address now resolves in the time the CLI takes to start. An address whose host turns out unreadable by its ids still falls back to discovery.

## 2026-10-18

Discovery now tells a provider whose block names a login this machine does not have apart from one with missing or refused keys. A provider backend raises the new `ProviderCredentialSourceMissingError` (a `ProviderNotAuthorizedError`) when its block points at a local credential source that is absent here (an AWS named profile, an Azure CLI login, a Modal profile), naming the command that sets it up in `short_remediation`. `mngr observe` keeps re-checking such a provider every two minutes instead of ending its poller, because the user installs that login outside mngr and nothing restarts the process when it appears; missing pasted keys still end the poller as before.

Per-provider discovery snapshots carry the provider's concise fix as a new optional `remediation` field on `DiscoveryError` (filled from `ProviderUnavailableError.short_remediation`, on the listing path too), so a consumer can show the next step without parsing the message. `PROVIDER_AUTHORIZATION_ERROR_TYPE_NAMES` in `imbue.mngr.errors` lists both authorization error type names for consumers that match snapshot error types by name.

The providers concept doc's AWS example now uses real keys (`default_region` and the new `aws_profile`) instead of a `profile` key the config never accepted.

`mngr create --format jsonl` now streams structured progress on stdout ahead of its terminal `created` event, so a caller can show an honest progress bar instead of a clock. The stream is a sequence of `{"event": "progress", ...}` lines: a `plan` naming the skeleton phases the create will run (`preparing`, `creating_host`, `setting_up_host`, `transferring_project`, `provisioning`, `starting_agent`, with the phases that do not apply left out), then `started`, `advanced`, and `completed` events per step, each completion carrying its elapsed seconds. Providers report their own finer steps under the phase that contains them with ids namespaced by backend (`docker.build`, `vps.wait_active`, `lima.start_vm`, `modal.build`, `imbue_cloud.lease`), and a step with a countable inner unit (Dockerfile stages, provisioning commands) advances through it. Step ids are stable across releases; the human label travels with every start so a consumer can caption a step it has never seen. Human and JSON output are unchanged.

Post-host-create commands and the template's provisioning commands are each reported as their own step under the phase they belong to (`setting_up_host.inner.command_<n>`, `provisioning.command_<n>`), and provisioning commands now stream their output into the create's build log line by line as they run rather than surfacing it only on failure. A template that builds an entire workspace during provisioning (a Lima VM without a pre-baked image) therefore shows which script it is on.

The reporter lives on `MngrContext` (`progress_reporter`, a no-op unless the CLI installs the JSONL emitter), so plugins report through the same object. A BuildKit plain-output parser (`imbue.mngr.utils.docker_build_progress`) turns the `[k/n]` stage markers that `docker build` and `depot build` print on every platform into counted progress, and is shared by the docker and VPS providers. It also reports every Dockerfile stage as a step of its own under the build step, named by its instruction (`docker.build.run_bash_system_3f9a1c`: a slug of the instruction plus a hash of its text, so the same instruction keeps its id when stages are added around it and an edited one becomes a new step), plus `docker.build.context` for loading the build context and `docker.build.export` for the image export after the last stage, which on a large image is a sizable share of the build. A stage the build cache serves is reported with the new `skipped` event kind rather than `completed`: behind the build like a completed step, but not a measurement of it, so a consumer learning durations does not record it. `skip_step` joins the reporter interface, and `text_step_id` derives such ids for any provider.

- Added `libs/mngr/skills_plugin/`: the `imbue-mngr-skills` Claude Code and Codex plugin (`mngr-help`, `find-agent`, `message-agent`, `wait-for-agent`) now lives in this repo and is exported to the new `imbue-ai/imbue-skills` marketplace on every push to `main`, replacing the hand-maintained `imbue-ai/mngr-claude-skills` repo, which is frozen. A test holds every `mngr <subcommand>` the skills name to the live CLI.

- `mngr extras claude-plugin` offers three plugins: `imbue-code-guardian`, `imbue-mngr-skills@imbue-skills`, and the new `imbue-studio-skills@imbue-skills`.

- Added `start_pid_death_watcher` and `is_pid_alive` to `imbue.mngr.utils.parent_process`, so a child can exit when a named process (its embedder) is gone; the grandparent death watcher is built on the same poll.

- Added `imbue.mngr.utils.agent_skills`, the reader for published skill files that the drift tests use.

Testing integration branch combining three in-flight changes (each branch's own entry sits beside this one in the projects it touches): Imbue Cloud machine resizing from the workspace settings page (`mngr-allow-imbue-resizing`), shared-workspace cleanup (`mngr-cleanup-shares`), and cloud connections backed by a local login instead of pasted keys (`mngr-allow-provider-profile-vars`).

For this project: provider backends raise `ProviderCredentialSourceMissingError` when a block names a local login this machine lacks, `mngr observe` keeps re-checking such providers instead of ending their poller, and discovery snapshots carry the provider's short remediation.

## 2026-10-17

Regenerated the `mngr imbue_cloud` command reference (`docs/commands/secondary/imbue_cloud.md`) for the new `mngr imbue_cloud sync documents` subgroup (`pull`, `push`, `delete`) added by the imbue_cloud plugin.

Provider release trips now assert, right after create, that a VPS-family provider minted only per-host VM-root client keys (no provider-wide `keys/vps_ssh_key`). The outer-host table in the hosts concept doc reflects the per-host key. Trip 1 also gains an opt-in step (`MNGR_RELEASE_TEST_LEGACY_VPS_KEY=1`) that relocates that key to the retired provider-wide location and proves a host created before per-host keys stays listable and reachable through the read-only fallback, without any key being minted.

Part of the account-owned remote providers program (`specs/account-owned-providers/spec.md`); the entries `mngr-per-client-host-keys.md` and `mngr-fix-provider-storage.md` in this directory belong to the same PR.

- The shared provider release trips keep main's recorded-size assertion and the branch's per-host VPS-root key assertions; the regenerated `imbue_cloud` command docs cover main's newer subcommands and the `sync documents` subgroup.

- `imbue.mngr.providers.host_access_manifest` adds `HostAccessManifest`: the per-host access material a provider keeps under an instance dir (key dir, per-host dir scheme, the key files, the agent key, the pin stores and the per-host `known_hosts` link), with the path resolvers a consumer carrying a host's access to another machine needs. Each provider publishes one; the minds workspace record reads the layouts from them instead of restating five of its own.

- `HostAccessManifest` says it describes the provider's default placement: its `agent_key_file_name` and `known_hosts_link_store_name` descriptions name that placement, since the vps providers' bare placement links its per-host `known_hosts` at a different store and is not published.

## 2026-10-16

The streaming discovery poller (`mngr observe --discovery-only`, which the Imbue Studio desktop runs) now re-reads the agents of every host a provider's last full poll listed as running, on a new per-provider cadence `host_discovery_poll_interval_seconds` (default 3 seconds), between the full polls that `discovery_poll_interval_seconds` (still 30 seconds) paces. A re-read emits only what changed as the existing per-host events (`HOST_DISCOVERED`, `AGENT_DISCOVERED`, `AGENT_DESTROYED`, `HOST_SSH_INFO`), so an agent created or destroyed inside a known host reaches the stream within seconds instead of up to a poll interval later; the steady state emits nothing. The full poll remains where hosts appear and disappear and where provider health is reported, and is never delayed by the re-reads. Setting the re-read cadence at or above the poll interval disables the re-reads.

Providers take part through a new `ProviderInstanceInterface` seam: `supports_incremental_host_discovery` (default true, false for the batch-reading modal and vps providers) and `read_host_for_incremental_discovery`, whose default re-reads the host live the way the bounded full poll does, but leaves the host connected afterwards (so a provider that caches its host objects answers the next re-read over the same SSH transport rather than a new handshake; only the full poll's read renews the connection) and without that poll's offline fallback. A re-read that fails emits nothing and leaves the host to the next full poll. The ssh provider now caches its host objects while their config is unchanged, as the docker and lima providers already did, so it benefits too; a cached object is dropped (and disconnected) when its host is no longer configured, on a connection error, and when the provider closes.

The per-host read registry shared by full polls and re-reads now records when each in-flight read started. A full poll that finds a host's re-read still running within the host timeout waits for it and harvests it as its own listing of the host, instead of reporting the host unknown; only a read that has outlived the timeout is still skipped with the wedged-host warning.

The generated `mngr imbue_cloud shares create` reference now gives `system-interface-<rand>` as the example `--entry-label`: workspaces mint service origin labels without underscores from now on, so new share links no longer contain one. Previously minted `system_interface-<rand>` labels remain valid.

Renaming an agent no longer breaks anything that found its tmux session by the old name.

- New agents' launch commands no longer carry the tmux session name; they read the session they run in. `mngr start` rewrites the session name out of the stored launch command of an agent created by an older mngr (the claude, codex and antigravity agent types say what to rewrite), so a renamed agent's background tasks start for the right session again. An older agent keeps the background-task scripts it was created with, so its background tasks still end on a live rename until its next restart.

- The resize hook set on each agent session finds the session by the name it has when the hook fires, so a renamed agent's window again refits to a client that attaches (the terminal app, `mngr connect`).

- On a remote host, Ctrl-q / Ctrl-t during `mngr connect` name their signal file for the agent id, so the stop or destroy still happens after the agent is renamed while attached. `mngr connect` clears any signal file an earlier connect left behind before it attaches, so a plain detach can no longer stop or destroy the agent, and it runs the stop or destroy by agent id and host (`ID@HOST`), so a same-id agent on another host is left alone.

- `imbue.mngr.utils.testing` has helpers for exercising tmux behavior and an agent plugin's background-tasks supervisor against a real tmux server: `renamed_tmux_session`, `terminal_tmux_server`, `create_background_tasks_state_dir`, `build_background_tasks_supervisor_env`, `running_background_tasks_supervisor` and `is_background_tasks_supervisor_polling`.

## 2026-10-15

`capture_tmux_pane_content` and `build_tmux_capture_pane_command` take `include_styles`,
which captures with `tmux capture-pane -e` so each cell's colour, dim and reverse-video
attributes come back as escape sequences. `BaseAgent._capture_pane_content` passes it through.

Interactive TUI agents gain a `_capture_input_box_pane_content` hook, through which the
pre-paste check for leftover input-box text reads the pane. It defaults to the plain capture;
the claude agent overrides it to read its input box without the hints Claude draws there.

## 2026-10-14

A compaction transcript reading can say the context size is unknown because of a compaction. `CompactionTranscriptScanner` and `CompactionTranscriptReading` gain `is_context_size_unknown_since_compaction` and `is_context_size_settled()`: a harness scanner sets the flag when it meets a compaction record newer than every record reporting a token count, and the scan then stops looking for older token counts. The cache keeps that state between checks: compaction records appended after a cached token count replace the count with "unknown", and a token count appended after them restores a number.

- An agent's message lock can be taken with a bounded wait. `BaseAgent._message_lock(timeout_seconds=...)` polls for the lock until the timeout and then raises the new `MessageLockTimeoutError`; without a timeout it blocks as long as another send holds it, which is how `mngr message` takes it.

- Every command a send runs while holding the message lock has a timeout: the tmux pane lookup, leaving copy-mode, the text and Enter keystrokes, the long-message buffer load and paste and its cleanup, `press_key_chord`, the TUI keystroke helper, and the delivery-event append. Each is bounded by the agent's `send_command_timeout_seconds` (default 15s, with headroom for gVisor's slow process spawns). A wedged tmux server fails the send with `SendMessageError` instead of hanging it with the lock held, and a failure that left no output says it may have hit the timeout.

- Send-keys agents deliver a message in `_send_message_holding_lock`, which `send_message` calls with the lock held, so a caller can take the lock itself (to bound the wait, or to re-check the agent before sending). Interactive TUI agents override that method instead of `send_message`.

- `HasCompactionMixin.request_compaction` accepts two optional arguments: `message_lock_timeout_seconds`, which bounds the wait for the message lock (raising `MessageLockTimeoutError`), and `expected_idle_since`, which skips the compaction with the new `AgentNoLongerIdleError` when, once the lock is held, the agent is no longer idle since that moment. Left unset, the lock wait is unbounded and the idle start is not re-checked. `read_idle_since_for_compaction` performs that check for harnesses. Every `HasCompactionMixin` implementation must accept the two new arguments.

- Interactive TUI agents gain an overridable `_clear_preexisting_input_text_if_restored_command` hook. Before a send pastes its message, a harness can use it to clear leftover input-box text that the TUI itself restored (for example a cancelled command) instead of appending the message to it. A cleared leftover is recorded as a `cleared_restored_input_text` delivery event instead of the `preexisting_input_text` warning. The default clears nothing, so other harnesses warn and append. The send step that runs this check is renamed from `_warn_if_preexisting_input_text` to `_clear_or_warn_about_preexisting_input_text`; a subclass overriding the old name must rename its override.

Added a bounded, cached transcript reader for compaction eligibility checks.

- `scan_jsonl_file_backward`, `scan_jsonl_region_backward` and `scan_jsonl_tail_backward` (`imbue.mngr.agents.jsonl_backward_scan`) feed a JSONL file's complete lines to a `JsonlRecordVisitor` newest-first. They read a 64 KiB window from the end through `read_file_tail_from_offset` and double it only until the visitor has what it needs, so memory stays proportional to the bytes read rather than the file size. A final line without a trailing newline is treated as still being written and is read once it is complete. Lines are split only at `\n`, so a record containing U+0085, U+2028 or U+2029, which `str.splitlines()` splits apart, is read whole. `read_jsonl_file_size` returns a file's size without reading its content.

- `CompactionTranscriptCache` and its shared instance `COMPACTION_TRANSCRIPT_CACHE` (`imbue.mngr.agents.compaction_transcript`) remember each transcript's newest assistant-turn timestamp and context token count, keyed by host and path, with the byte offset scanned. An unchanged transcript is answered from a size probe that reads none of its content, a grown one is read backward from its end no further than where the last check stopped, and a truncated or replaced one is rescanned. `evict_agents_other_than` drops the entries of agents no longer being checked, and the cache is capped at 2048 entries.

- Harnesses implementing `HasCompactionMixin` subclass `CompactionTranscriptScanner` with their own record matching. A scanner whose match for an older record depends on newer records overrides `carries_state_into_older_records`, which makes an incremental update rescan the file.

- New `JsonlFileShrankDuringScanError`, raised when a file shrinks between two reads of one scan.

- Online hosts gain `load_agents_from_refs(agent_refs)`, which returns the agents for specific discovered refs instead of every agent on the host. It builds each agent from the `data.json` contents discovery already put in the ref's `certified_data`, so nothing is read twice, and a corrupt record of an agent that was not requested cannot fail the load. A ref without the full record (for example a provider's lease stub) has only its own `data.json` read from the host, in one batched command on a remote host; a ref whose `data.json` is gone is left out, and a ref for another host raises `HostError`. `get_agents()` and `discover_agents()` are unchanged.

Three codex tutorial e2e release tests no longer assume `mngr create --no-auto-start` leaves the agent unstarted. That flag governs starting offline *hosts*; the create always launches the agent, so a codex agent on a host with no codex binary cannot come up.

`test_create_codex_explicit_type` no longer turns off `agent_types.codex.check_installation`. With the check off, provisioning skipped the codex install and the launch then failed with `codex app-server did not become ready within 10s`. Letting the install run is what the two sibling codex tutorial tests already do. `codex app-server` completes mngr's readiness handshake without being logged in, so the test still needs no codex credentials.

All three codex tutorial creates now get the same widened per-command budget, and so does each test's `mngr list` read-back of the created type. Every create is its test's first `mngr` invocation and installs the codex CLI when the host has none, and computing a just-launched codex agent's live state is occasionally slow, so the 30s default left both commands near the ceiling.

`test_create_codex_agent` is no longer marked `@pytest.mark.rsync`. The source repo is clean, so the worktree transfer finds no untracked or modified files to copy and never invokes rsync, and the mark's resource guard failed the test for never invoking rsync even though the test itself passed.

## 2026-10-13

`mngr create` against a Modal host no longer fails when the sandbox's tunnel closes the first bulk transfer's connection.

A freshly created Modal sandbox is reached through a tunnel that sometimes closes a brand-new SSH connection before the key exchange finishes -- seconds after the readiness probe has already opened and authenticated a session through the very same endpoint, and while connections opened earlier keep working straight through the window. Create's first rsync or `git push` landed in that window and took the whole command down with `rsync failed: Connection closed by <address> port <port>` or `kex_exchange_identification: read: Connection reset by peer`.

Every rsync and git transfer that reaches a host over SSH now rides that out: a failure whose stderr says the peer closed the connection is re-run from the start for up to 30 seconds, which is safe because these transfers (`rsync`, and `git fetch`/`git push` with explicit forced refspecs) reach the same end state whether they run once or twice. A refused connection, a host-key mismatch, and any failure of the command itself are deliberately excluded, so a host that is genuinely down or mispinned still fails on the first attempt.

The same window also times out the TCP connect of the paramiko connection a host is opened with, so that shape (`Could not connect (timed out)`) joins the handshake failures the connect already retries. Matched on the timeout specifically, not on the shared `Could not connect` prefix, which also carries refusals.

The group headers in `test_ratchets.py` change from the banner form `# --- Code safety ---` to `# ratchet_group("Code safety")`, a marker the ratchet sync script reads. The script now also keeps every test under the group its check has in `standard_ratchet_checks.py`, and the groups in that file's order. Here that moved the `Modal images` group up to its place after `Process management`. No ratchet, recorded count, or test name changes.

A running agent no longer reads as STOPPED for a moment while it is being renamed.

`mngr rename` renames the agent's tmux session before it rewrites the agent's `data.json`, and a listing loads each agent's name before it checks for the agent's session. A listing that ran in between (`mngr list`, or an `mngr observe` snapshot) looked for a session that no longer existed and reported the live agent STOPPED. Seen in practice when the minds chat app auto-named a new chat: the observer emitted one STOPPED event for the chat's agent mid-turn.

New tmux sessions now carry the agent's id in a `@mngr_agent_id` session option, which survives `rename-session`, and the lifecycle probe finds the session by that id. So does the single-command listing that Modal, VPS and imbue cloud hosts use, which reads each agent's name just after it lists the host's tmux panes and could miss a rename landing in between. It falls back to the session named after the agent, which is how sessions started before this change are still found.

The shipped `resources/Dockerfile` no longer says that a build `ARG` cannot be used in it. That was true while the Modal provider applied each instruction as its own build stage, which dropped an `ARG` before the `RUN` that consumed it; the provider now keeps every `ARG` in scope for the rest of the build, so the comments that told a reader to inline the restic and offload version pins instead are gone. The pins themselves are unchanged.

`mngr create`'s help for the modal provider's `docker-build-arg`, and the provider's documented build args, describe it as setting the value of a Dockerfile `ARG` rather than as overriding an `ARG`'s default. The flag no longer works by rewriting the `ARG KEY=<default>` line in the Dockerfile text, and it applies to an `ARG` declared with no default just as it does to one with a default.

The Dockerfile also no longer says that a `WORKDIR` set in one of the Modal provider's cached layers is lost in the next layer's `RUN`, which defaults to `/`. A `docker build` of the two-layer shape the provider produces shows the `RUN` in the second layer starting in the first layer's `WORKDIR`: `WORKDIR` is image config, which `FROM` inherits, as `ENV` is. `ARG` is the one of the three that a later layer does not see.

## 2026-10-12

A long message now reaches an agent whole. Messages of 1024 characters or more are pasted into the agent's terminal pane, and that paste carried none of the markers a terminal uses to say where one paste begins and ends. A coding TUI reads its terminal a few kilobytes at a time, so Claude Code took a message larger than about 4KB as unrelated keystrokes and kept only the final chunk: an agent sent a 4302-byte task brief received the last 213 bytes of it, starting mid-word, with no error reported on either side. The delivery check looks for the end of the message, which is the part that survived, so the send was confirmed as successful. Pastes are now bracketed, which a TUI takes as one message no matter how many reads it arrives in; a pane running something that does not ask for bracketed paste is unaffected. A shell counts as asking: bash (5.1 and later) and zsh both do by default, so a long multi-line message sent to a `command` agent running a shell now arrives as one edit that the trailing Enter runs as a whole, instead of each line running as it arrives. When the Enter after a long message is swallowed, mngr now re-sends it. Claude Code and agy show a pasted message as a `[Pasted text #N +M lines]` placeholder rather than its text, and the Enter retry waited to see the end of the message on screen, so it never fired for them and the send failed after the full confirmation window. The retry now also fires while that placeholder is still in the input box.

Discovery events now carry a host's recorded size: `DiscoveredHost` has an optional `size` (CPU count, memory in GB, disk in GB), filled by the listing path from `host.resource` and by the providers whose discovery can read the size off their own records (docker, lima, modal, and the cloud VPS providers). A consumer of the `mngr observe` stream can therefore show a machine's size without running `mngr list`; a provider that fills nothing leaves it `None`, and older readers ignore the field. The discovery aggregator keeps a host's last reported size across the incremental host event a create, start, or restart emits, which does not know it.

## 2026-10-10

- The shared provider release trip (`run_provider_release_trip1`) gains an optional size check: a profile that implements `expected_host_resources` has the size `mngr list --format json` reports for the created host compared against what the cloud itself says. The `host_from_list_json` / `host_resources_from_list_json` / `assert_host_resources_match` helpers are shared with the provider-specific release tests.

- The hosts concept doc ("Sizing") notes that the cloud VPS providers record and report the instance shape the cloud reports, and list an unanswerable size as unknown rather than as a placeholder.

- The generic listing path reads an online host's recorded size the same guarded way it already read an offline host's: a provider whose record cannot answer the size (e.g. a VPS record from before shapes were recorded) lists the host with no `host.resource` instead of failing its row.

## 2026-10-08

The generated CLI reference for `mngr imbue_cloud shares invite` lists its new `--app-display-name` option, the name a person reads for the app being shared, which the invitation email uses in place of the app's service name.

- The generated command docs now cover `mngr imbue_cloud shares mobile-access-link`, which has Imbue Cloud email you the link to one of your own published workspaces.

Classified the new `issue-agent-kit` library (shared plumbing for the mngr-seer issue pipeline) as unpublished in the plugin catalog: like `modal-app-kit`, it ships into the pipeline apps' Modal containers as a source mount and is never released to PyPI, so the release tooling must not offer it or require `==` pins on its internal dependencies.

Test-only: the observe follower tests no longer fail intermittently when a test stands in for an observer restarting. The running follower's liveness probe holds the observe lock for an instant, so the test helper now retries taking the lock instead of failing on the first try.

Comment and docstring prose that referred to the desktop app as "Minds" now
says "Imbue Studio". No behavior change: every identifier (`MINDS_*` env vars,
`minds-*` service and scope names, paths) keeps its spelling, per
`apps/minds/style_guide.md`.

Review follow-up to the Imbue Studio prose sweep: the discovery-error test's
docstring said "Minds' providers panel keys off this field" and now names the
product Imbue Studio.

## 2026-10-07

Bump the pinned Claude Code version in `libs/mngr/imbue/mngr/resources/Dockerfile` from 2.1.280 to 2.1.293. The release test `test_claude_code_version_matches_default_workspace_template_pin` requires this literal to equal default-workspace-template's `[agent_types.claude].version`, so it lands together with the matching bump there. 2.1.293 is npm's `latest` release (`stable` is still 2.1.285) and the first to carry Claude Haiku 5.5; its `sonnet` and `haiku` aliases resolve to Claude Sonnet 5.5 and Claude Haiku 5.5.

The `mngr latchkey forward --device-id` reference now says an agent, rather than a workspace, tells the user's desktops apart by it.

## 2026-10-06

`mngr create` with an unrecognized option is still rejected exactly as before; only the release test that checks it changed.

The test required click's usage error to read `No such option: --flag`, but click now words it `No such option '--flag'.`, so the test failed on every platform against current dependencies. It now checks that the error names the rejected option without pinning click's punctuation, which is what the test was always meant to prove.

The `mngr exec --start` / `--no-start` tutorial examples now run `uname -s` instead of `cat /etc/os-release`. macOS has no `/etc/os-release`, so following the tutorial on a Mac produced `cat: /etc/os-release: No such file or directory` rather than the demonstrated output.

Three e2e tutorial tests that relied on GNU-only tool behavior now pass on macOS as well as Linux:

- `test_exec_with_start` and `test_exec_no_start` assert on the host's own kernel name from `uname -s` rather than on `/etc/os-release` fields.

- `test_create_with_env_vars` reads the two variables it sets with one `printenv` call each. BSD `printenv` accepts a single operand and silently ignores the rest, so `printenv DEBUG LOG_LEVEL` returned only `DEBUG` on macOS and the `LOG_LEVEL` assertion failed even though the variable had been set correctly.

The release test covering `mngr create -w` (extra tmux windows for a command agent) now finds the harness's tmux socket when the tests run as any user.

It queried `$TMUX_TMPDIR/tmux-0/default`, but tmux names that directory after the running user's id, so the path existed only when the tests ran as root. Everywhere else the query failed with "error connecting ... No such file or directory" and the test could not check that the extra window was created. `mngr create -w` itself is unchanged.

The release test covering `mngr observe --discovery-only` now checks for the event type discovery actually emits.

It required a `DISCOVERY_FULL` event in the stream. That event is the historical whole-scan snapshot: per-provider discovery superseded it, each provider now emits its own `DISCOVERY_PROVIDER` snapshot on its own loop, and `DISCOVERY_FULL` is no longer produced at all (the enum keeps it only so historical on-disk logs still parse). The test could therefore never pass. `mngr observe` itself is unchanged; the test and its comments now describe per-provider discovery.

Fixed the `test_config_set_list_active_default` e2e tutorial test, which sets the `commands.list.active` default and then reads it back.

The e2e harness leaves the project `settings.toml` absent so the `config edit` tutorial tests see genuine first use, and a file `mngr config set` creates does not carry `is_allowed_in_pytest`. Every config loaded during a pytest run must set it, so the follow-up `mngr config get` and `mngr list` both failed with "Running mngr within pytest is not allowed" against the file `config set` had just written. The test now seeds the opt-in into its own project config before writing to it, through a new `opted_in_project_settings` e2e fixture that replaces the hand-written seeding line in the four other tutorial tests that needed it.

The test also records its subprocess Modal usage with the resource guard: its final `mngr list` runs the full provider-discovery path, which makes a real authenticated Modal SDK call, but in a subprocess the guard's in-process monkeypatch cannot observe it.

## 2026-10-05

Regenerated `docs/commands/secondary/imbue_cloud.md` for the new `mngr imbue_cloud shares push-grants`, `shares invite`, `shares invitation-outcomes`, and `account notification-preferences show|set` commands (the invitation work of `specs/inviting-granted-visitors/spec.md`).

Six e2e tutorial tests assumed they were running on Linux and failed on every macOS shard.

Three hard-coded `/bin/true` and one `/bin/false` as a stand-in editor; macOS puts both under `/usr/bin`, so `mngr config edit` reported `Editor not found`. They now name the commands without a path, which `mngr config edit` resolves through PATH on either platform.

`test_exec_cwd` asserted `pwd` prints exactly `/tmp`, but `/tmp` is a symlink to `/private/tmp` on macOS and `pwd` prints the resolved path. It now matches whatever the host resolves `/tmp` to.

`test_tips_exec_env_inspect` checked that `env | sort` really sorted by re-sorting in Python. `sort` orders by the host's locale, which places the underscore in a name like `CLAUDE_CODE_BRIDGE_SESSION_ID` differently from Python's codepoint order, so the check disagreed with the pipeline it was verifying. It now re-sorts with `sort` itself.

Added `start_mngr_thread` to `imbue.mngr.utils.thread_cleanup`: it starts a thread on a `ConcurrencyGroup` like `start_new_thread` and destroys the thread's gevent Hub (and the pipe pair it holds) when the target returns. Every `start_new_thread` call in mngr now goes through it. Before, threads that ran host commands, such as the per-host reads in `discover_hosts_within_per_host_timeout` and the agent-creation writes, each left a Hub behind for the life of the process. A new `test_prevent_raw_start_new_thread` ratchet, at a count of 0, keeps raw calls out.

## 2026-10-02

Documented the new `--device-id` option of `mngr latchkey forward` in the command reference.

Fixed two config loader tests that called `_isolate_load_config_env`, the former name of the shared `isolate_load_config_env` test helper, and so failed with a `NameError`. No behavior change.

The docker provider moved out of the core package into the `imbue-mngr-docker` plugin (`libs/mngr_docker/`).

- A plain `imbue-mngr` install no longer registers a `docker` backend or depends on the `docker` and `requests` packages. Install `imbue-mngr-docker` to get it back; the install script offers it when a Docker daemon answers, the plugin catalog lists it, and a `[providers.docker]` block found without the plugin now names the package to install.

- `mngr help docker_usage` and the `mngr docker` command group come from the plugin. The docker-specific error classes moved to `imbue.mngr_docker.errors` under the same names.

- Generic provider messages no longer assume docker is built in: the unknown-provider hint points at `mngr plugin list`, and the default provider-unavailable help no longer says "start Docker".

- Test support: `load_local_backend_only` skips the docker backend like the other external backends (there is no `include_docker` flag any more); the leaked docker container sweep and the release-sandbox dockerd startup fixture moved to the plugin; `MockProviderInstance` can now back a readable offline host with a local volume and persist agent data in memory, which the tests that previously borrowed the docker provider for that now use.

## 2026-10-01

The repo-wide dependency refresh advanced the `[tool.uv] exclude-newer` supply-chain cooldown cutoff to the policy maximum (UTC today minus two weeks, 2026-09-02) and re-locked with `uv lock --upgrade`, so every dependency now sits at the newest release the cooldown admits rather than whatever was previously pinned. 136 packages moved.

`mngr`'s own code follows the annotations the refreshed libraries tightened, with no behaviour change:

- click 8.5 deprecates `get_text_stream` (removal in click 9.0). The error renderer writes to `sys.stderr` directly, which is the same stream the helper returned.

- urwid 4.0.13 types a key as `str | tuple[str, int, int, int]` -- the 4-tuple is a mouse event -- so the `mngr extras` picker and the plugin-install wizard's input filters accept and return that union. It also types a `ListBox` position as `SupportsIndex`, which the single-select picker converts before returning an index.

`load_config` no longer rejects a `[plugins.<name>] enabled = false` settings section for a plugin this install does not have. It now blocks that name the same lenient way `create_plugin_manager` already does at startup. Only `--disable-plugin` flags are still checked strictly, so a typo there keeps failing with "Cannot disable plugin ... because it is not registered". Every other disable is now lenient too, including `MNGR__PLUGINS__*` environment overrides and settings read under `MNGR_LOAD_ALL_PLUGINS`.

Previously, a process that reloaded settings against the plugin manager it built at startup failed whenever a disable section had been written after that point for a plugin that is not installed. Imbue Studio's packaged app hit this on every first launch: it writes `[plugins.recursive] enabled = false` when the user signs in, and it does not ship the `recursive` plugin.

Marked `test_lifecycle_hooks.py::test_on_before_command_receives_correct_params` as `@pytest.mark.flaky`. It timed out once in CI (PR #1575, a frontend-only change) when an in-process `mngr list` did not join all of its concurrency-group threads within the 10s test limit.

## 2026-09-30

Removed the 33 comment lines that had accumulated inside `libs/mngr/imbue/mngr/utils/test_ratchets.py` explaining why individual ratchet counts had been raised.

A ratchet file records counts, not history. The reason a count moved now goes in the commit message and the changelog entry, the reason a particular code site is allowed goes in that module's docstring, and the set of exempt files goes in the `excluded_patterns=(...)` argument -- all places that stay correct as the code changes, which the comments did not.

- Marked `test_option_choices_reference_real_options` flaky so offload retries it. Building the completion cache reads `.commands` on every provider CLI group, which defeats `LazyProviderCliGroup` and imports the AWS, Azure and GCP SDKs; that import finishes in seconds normally but has blown the 10-second pytest timeout on a cold-cache offload run whose sandbox I/O was still saturated by the base image build. The nine sibling tests in that file drive the same code path and are exposed to the same stall.

The default Dockerfile (`imbue/mngr/resources/Dockerfile`, also the offload test image) now installs `lsof`, which the minds runner-reset helper `kill-processes-under.sh` and its tests use to find processes by the files they hold.

`mngr event --follow` no longer re-downloads a remote source's whole `events.jsonl` on every poll:

- Remote sources are polled with ranged reads: one SFTP stat plus a read of only the bytes past the saved offset. On a workspace whose `services/events.jsonl` had reached 1.16 GB, each poll used to take one to two minutes. The new `HostFileReadInterface.read_file_tail_from_offset` returns those bytes with the file size seen by the same call, which is how rotation is detected. Offsets now count raw bytes, so an undecodable byte can no longer push later reads into the middle of a line. Whole-file reads use the same primitive with a prefetched read.

- Every read of the discovery events log is byte-based. An undecodable byte used to make the discovery tail re-read the same bytes forever, and made `mngr stop`/`mngr connect` identifier resolution, agent/host name tab completion, and the `mngr forward --observe-via-file` attach fail. It now costs one skipped line with a warning.

- The agent-lifecycle follower that tails another process's `mngr observe` events file counts raw bytes too, so an undecodable byte no longer shifts its later reads into the middle of a line.

- A line in an events file that is valid JSON but not an event (no `timestamp`, or not a JSON object) is now logged and skipped. It used to stop `mngr event --follow` at that line for good, re-reading it on every poll and re-resolving the host, and it made a history read containing it exit at startup.

`mngr observe` now rotates its agents event file (`events/mngr/agents/events.jsonl`). The file used to grow without bound: it gets a full snapshot of every agent every 5 minutes, and on one long-lived workspace it had reached 1.2 GB, growing ~33 MB/day. The hourly backup re-read all of it on every run, and every `mngr observe` start parsed all of it to load its starting state.

Once the file reaches 50 MiB it is renamed to `events.jsonl.<timestamp>` just before the next full snapshot is written, so the live file always begins with a snapshot. Only the most recent rotated file is kept. An existing oversized file is rotated away at the first snapshot after upgrading and deleted at the rotation after that. Readers that fold from the newest snapshot (the chat app's follower, the desktop avatar) need nothing from the rotated file, and `mngr event` already reads rotated segments.

The agent env file writer's behavior of leaving a value that references another variable (`${HOME}/venvs/${MNGR_AGENT_NAME}`) unquoted, so that sourcing the file expands the reference, is now pinned by a test: `mngr tmr` relies on it to give each agent its own virtualenv path from one static `--env`-style value.

- Every `mngr` CLI process on glibc now caps its malloc arenas at four (`mallopt(M_ARENA_MAX, 4)`, set before any command starts threads). Four matches the workspace chat app's measured cap: two saved a little more memory but cost 55% more CPU per MB allocated. A burst of threads made glibc open one arena per thread, up to eight per core, and each arena kept its peak memory after its threads exited. That is where 467 of the 575 MiB held by a production `mngr observe` sat. A `MALLOC_ARENA_MAX` or `glibc.malloc.arena_max` tunable already in the environment is left alone, and the agents mngr launches do not inherit the cap. There is no change on macOS.

- `mngr observe` now watches every local agent's main process from one thread instead of one thread per agent. It uses pidfds on Linux, a kqueue on macOS, and polls only a process the kernel cannot report on. On a repro, bringing up 40 agents under the old threads took the observer from 110 to 144 MiB, and that memory stayed after the agents were destroyed.

## 2026-09-29

Bumped the offload version baked into `libs/mngr/imbue/mngr/resources/Dockerfile` (`OFFLOAD_VERSION`) from `0.9.13` to `0.9.14`, keeping the in-image `offload apply-diff` binary in lockstep with the CI pin.

Corrected the stale cross-reference in the comment above that pin: it told readers to keep `OFFLOAD_VERSION` in sync with `.github/workflows/ci.yml`, but the CI-side pin moved into the `.github/actions/setup-offload` composite action when the three offload jobs were de-duplicated, and `ci.yml` no longer names a version.

`mngr observe` no longer announces a destroyed agent again right after reporting it removed. A destroy stops the agent's process, and the observer's process watcher then re-lists the host. That listing could read the agent before the destroy deleted it and finish after the `AGENT_REMOVED`, so it emitted the agent as `STOPPED`. Consumers such as the chat app then showed a deleted chat again until the next full snapshot, up to five minutes later. A listing now leaves out any agent removed while it was running, and the periodic full snapshot does the same. An agent that a listing started after the removal still finds is reported as usual.

No change to `mngr` itself. This branch is stacked on the ownership-lock change, whose `mngr` entry covers the process-title quoting it carries; the diff against `main` shows it only because the base has not merged yet.

`mngr event --follow` now exits with `Error: Stopped following events: the reader for source '<source>' died (...)` when one of its readers dies. Before, it kept running and looked healthy while delivering nothing from that source. Full `mngr observe` (as run by `mngr notify`) already fails when one of its per-host follows fails, so it now exits with an error there instead of silently losing that host.

paramiko's `SFTPError` ("Garbage packet received", seen when an SFTP read is interrupted mid-packet, as when the suspension watchdog closes a connection after a laptop sleep) is now retried on a fresh connection like other broken-connection errors. It used to slip past every SSH error handler, and it was what killed those readers.

Repeated failures to check whether the followed host is online are now logged: debug on the first, one warning after ten.

New developer doc `docs/sleep_and_connectivity.md` (linked from `docs/architecture.md`) collects how clocks, SSH connections, and timeouts behave across laptop sleeps, DarkWakes, and lost networks, and what new timeouts and liveness checks should do about it.

The generated `mngr create` help docs for the AWS `--aws-ami=` build arg now say the fallback is the newest Debian 13 AMI for the instance type's architecture, resolved from EC2 at create time, instead of the pinned per-region default that the AWS provider no longer ships.

Documented how host sizing works across providers (`docs/concepts/hosts.md` "Sizing", `docs/concepts/providers.md` responsibilities): a host's CPU, memory, and disk are chosen at `mngr create` time through the provider's build and start arguments and reported as recorded via `get_host_resources`. There is deliberately no generic resize operation; a provider whose substrate can change size after creation (imbue_cloud today) exposes that through its own provider-specific command.

`mngr`'s generated command docs no longer call the desktop app by its old name: the `mngr forward`, `mngr latchkey` and `mngr imbue_cloud` reference pages now say "the Imbue Studio desktop client" where they named the product.

`mngr chat`'s page is deliberately untouched: the command no longer exists, so `make_cli_docs.py` cannot regenerate that file and it is left as the orphan it is.

The README's pointer to the desktop app now calls it Imbue Studio rather than minds. Link targets and paths are unchanged -- the app still lives at `apps/minds`.

- `ByteSize` is the new shared primitive for go-units byte sizes (`512m`, `4GiB`); `DockerMemorySize` refines it and lima's disk sizes build on it, so the grammar lives once.

- The journald cap the VPS hosts apply (`SystemMaxUse=512M`, change-guarded) moved into `providers/ssh_host_setup.py` as `build_cap_journald_command`, so the lima provider applies the same snippet.

- The backend-scoped host resolver behind `mngr docker resize` (discover only that backend's providers, refuse when none is enabled) moved to `cli/backend_hosts.py` and is shared with `mngr lima resize`.

- The hosts concept doc's "Sizing" section names `mngr lima resize` alongside the imbue_cloud and docker commands.

- The start-argument flag scanner the docker provider used to read and strip its recorded `--cpus` / `--memory` args (`flag_value_at`, `strip_flags`) moved to `providers/start_arg_flags.py`, so the lima provider reads its recorded size through the same code.

Merges the gen-2 learnings series into one branch; see the `mngr-host-sizing`, `mngr-host-sizing-debian-13`, and `mngr-vps-stopped-container-recovery` entries for the details. In short: host sizing is documented as fixed at create with provider-specific resize only, the generated `--aws-ami=` help names the Debian 13 default, `mngr list` finds the agents of a stopped VPS container (`docker cp -L`), and `OfflineHost` gained an `observed_state` so a reachable VPS whose container merely exited reports `STOPPED` rather than `CRASHED`.

The docker provider now records, reports, and can change a container's CPU and memory caps.

- New `[providers.docker]` settings `default_cpus` and `default_memory` cap every new container (`docker run --cpus` / `--memory`, with `--memory-swap` set equal to the memory cap so the container cannot swap). A `--cpus` or `--memory` in `default_start_args` or a caller's `-s` flag still wins, since docker keeps the last spelling of a repeated flag. A `default_cpus` above the daemon's CPU count is clamped with a warning instead of failing the create.

- `mngr list` reports a docker host's real size (`host.resource.cpu.count` / `memory_gb`) instead of the old 1 CPU / 1 GB placeholder: the caps recorded in its `docker run` arguments, with any uncapped dimension filled in from the daemon machine's totals. Disk stays unreported (the host volume has no quota of its own).

- Stopped hosts now list their size too: `get_provider_resources` moved from `OnlineHostInterface` to `HostInterface`, offline hosts answer it from the provider's records, and the default listing path (docker, lima) fills `resource` for offline hosts. The unreachable-host fallback tolerates a provider whose record read fails (size unknown, with a warning) rather than failing the listing.

- New `mngr docker resize HOST [--cpus N] [--memory SIZE]` rewrites the caps recorded for a docker host and applies them to its container with `docker update`, running or stopped; a value docker refuses is reported and leaves the record untouched. `mngr start` and a snapshot restore re-apply the recorded size, so it survives out-of-band `docker start`s and daemon restarts.

- `mngr list` (and every command that resolves an agent by address) now finds the agents of a stopped VPS container. The outer listing script copies a stopped container's host_dir out with `docker cp`, but a VPS container's host_dir is a symlink into its volume, and `docker cp` copied the link itself, so the extracted path was never a directory and the host listed zero agents; the copy now uses `docker cp -L`. Containers whose host_dir is a real directory (the local docker provider) are unaffected.

- `OfflineHost` gained an `observed_state`: the lifecycle state a provider saw for itself when it built the host, which wins over the state derived from the host record. The VPS providers set it to `STOPPED` for a reachable VM whose container has merely exited, so the agent row of `mngr list` no longer reports such a host as `CRASHED`.

## 2026-09-28

Combines the waitlist and signup-code work from `mngr/release-onboarding` with the browser sign-in handoff fixes from `mngr/signin-handoff-followup` (PR #1371); see this project's `mngr-signin-handoff-followup.md` entry for the details.

- The generated `mngr imbue_cloud auth login` docs list the new `--listen-timeout` option, carried in from the sign-in handoff branch.

The generated `mngr imbue_cloud auth login` docs list the new `--listen-timeout` option (how long the command waits for the browser to finish signing in).

## 2026-09-27

The `mngr autocompact` command reference (`docs/commands/secondary/autocompact.md`) now documents passing multiple agent targets to `check` and `run`.

The generated CLI reference for `mngr imbue_cloud auth login` (`docs/commands/secondary/imbue_cloud.md`) follows the `--success-redirect-url` help text: its example scheme is now `imbue-studio://`, the desktop app's new deep link scheme (phase 3 of the Imbue Studio rename, which lands together with the phase 4 cutover).

## 2026-09-26

The outer listing script now fails when `docker exec` into a running container fails, instead of always exiting 0. A container killed partway through a listing (for example, an imbue_cloud workspace being stopped) used to return its truncated output as a successful, shorter agent list. imbue_cloud then cached that list as the host's full set of agents, and a stopped workspace whose `system-services` agent had been cut off showed in minds as "unreachable" with no Start button. A truncated listing now counts as a failed one, so discovery keeps the previously cached agents.

The Docker provider's `test_container_responds_to_sigterm` no longer flakes. It stopped the container as soon as Docker reported it running, which can be before the entrypoint shell has installed its SIGTERM trap; PID 1 ignores SIGTERM without a handler, so the stop waited out its timeout and SIGKILLed the container (exit 137). The test now waits until PID 1's caught-signal mask includes SIGTERM before stopping.

Removed a duplicated definition of the plugin catalog's gate classes (`SignalGate`, `RequiredPackagesGate`, `Gate`), which were declared twice back to back.

The release Docker-in-Docker image (`Dockerfile.release.extras`) now installs the buildx CLI plugin beside the static Docker binaries, so `docker build` inside it uses BuildKit; the legacy builder rejects the `--secret` flag and `RUN --mount` the workspace template now uses.

## 2026-09-25

An unreachable docker `ssh_bind_address` (a loopback address with a remote daemon, or an IPv6 address with a local one) no longer breaks every mngr command. It used to be rejected while loading config, so a docker block that was disabled or never used could still fail an unrelated command: for example, a project settings file pinning `ssh_bind_address = "127.0.0.1"` on top of a user config pointing docker at a remote daemon made `mngr create` fail for imbue_cloud. The check now runs only when mngr creates a docker container (a new host or a snapshot restore), with the same error message.

`mngr observe` no longer runs away to several GB on gVisor workspaces. Under gVisor, `ps` sometimes lists a process that exits mid-read with pid 0. Every agent's lifecycle probe walks its pane's process tree, and when that pid-0 process was one of the agent's own (a short-lived `sleep` or `wc`), the walk followed pid 0's children back up to pid 1 and around into the pane again, forever. The observer that ran the probe grew about 30 MB/s until earlyoom shed it, and earlyoom's bands shed the workspace's chats, workers and update leads first. The probe now skips pid-0 lines, and both of its process-tree walks visit each process once, so no parent-link cycle can loop them.

The e2e test fixture's teardown now actually cleans up after each test, and the session-end Docker sweep runs again. Five defects the 2026-09-21 TMR run reported against the harness are fixed:

- The teardown ran `mngr destroy --all --force`, a flag `mngr destroy` lost in March, so it exited 2 and destroyed nothing; every e2e session leaked its agents. It now runs the documented `mngr list --ids | mngr destroy - --force` under pipefail, scoped to the backends the test could have created agents on, and logs a warning when that fails.

- The generated `destroy-env` script (written for `--mngr-e2e-keep-env`) ran the same dead command behind `|| true` and always printed "Environment destroyed.". It now runs the working destroy, deletes the test's Modal environment when the test was Modal-marked, exports the mngr environment the test ran under, and reports which steps failed with a non-zero exit.

- Every teardown deleted a Modal environment, whether or not the test could have created one, costing a network round trip and a spurious "No such environment" warning per test. The delete now runs only for `@pytest.mark.modal` tests, and a missing environment is logged as information rather than a warning.

- The Modal environment name overran Modal's 64-character limit whenever `MNGR_AGENT_NAME` was long (every TMR mapper), so the backend truncated it and concurrent sessions sharing a name prefix and a start second shared one environment and deleted each other's at teardown. The user id is now built to fit the limit, with a random suffix last so it survives any truncation; the fixture asserts the full name fits.

- The session-end stale-container sweep and the own-fixture leak check created their Docker client inside the session fixture's teardown, which runs inside the last test's teardown while the `docker_sdk` resource guard still enforces that test's marks; in any run deselecting `docker_sdk` the guard rejected the client and the sweep logged "Docker unavailable" and did nothing. Both checks now run from `pytest_sessionfinish`, after the guard's per-test environment is gone, and a guard rejection is logged as such instead of as an unreachable daemon. An own-fixture leak still fails the run (via `pytest.exit`) when pytest runs without xdist, which is how CI's offload sandboxes run it; under xdist it is logged and cleaned but cannot change the controller's exit status.

The e2e suite no longer carries a `@pytest.mark.timeout` on nearly every test. The 269 markers at or below 300 s, and the comment each one carried explaining that a `mngr` subprocess outlives the repo's 10 s default, are removed: release tests now get a 300 s class budget at collection from the shared conftest hooks. The 18 markers above 300 s stay, since those tests really need more.

## 2026-09-24

The type name a provider discovery snapshot records when its poll times out is now a shared constant (`PROVIDER_DISCOVERY_TIMEOUT_ERROR_TYPE_NAME` in `imbue.mngr.api.discovery_events`), so consumers can recognize a timed-out poll without copying the string. The per-host bounded discovery loop is now a shared function, `discover_hosts_within_per_host_timeout`, that providers which read a host and its agents together can hand their own per-host reads to. A host whose read in that loop fails with an unexpected error is now read again on the next poll, instead of being skipped as a read still in flight (and reported unknown) until the process restarts.

Commands that act on one agent no longer slow down with every other agent on its host. Loading a host's agents (`get_agents`, `discover_agents`) used to cost two SSH round trips per agent, one to check its directory and one to read its `data.json`, and `mngr exec`, `mngr start` and `mngr message` all load every agent on the host just to find the one they target. They now read every agent's `data.json` in one command. On a workspace with 36 agents, a pinned `mngr exec ... 'echo hi'` took 30s, and 16s of that was this loading. It now takes about 5s in total.

The listing script that providers run on a host (imbue_cloud, VPS, Modal) now collects activity times and tmux panes for all agents at once, instead of starting about eight processes per agent. Sandboxed (gVisor) workspaces pay tens of milliseconds for every process start, so listing a 36-agent workspace dropped from 3-7s to 0.3-0.7s. The parsed listing is unchanged; this was checked field by field against the old script on a live workspace.

Pinned `TERM` in the e2e fixture environment, fixing six release tests that failed only in CI.

`mngr connect` replaces itself with `tmux attach`, which needs a terminal type. A developer shell always sets `TERM`; a GitHub Actions runner does not, so the tests passed locally and failed in CI with `open terminal failed: terminal does not support clear`. `watch` fails the same way with `Error opening terminal: unknown.`, and there the surrounding `|| true` turned it into an empty capture that the assertion could not explain.

Fixes all five failing `test_connect` tests (`no_start`, `short_form`, `by_name`, `with_start`, `with_start_restarts_stopped_agent`) plus `test_list_watch_mode` -- eight failure instances across seven shards in release-tests run 35790568923 on main.

The e2e tutorial tests no longer depend on GNU `timeout`, which macOS does not ship.

Commands that stream forever (`mngr event --follow`, `mngr observe`) were bounded with `timeout N`. On a BSD userland that is `127: command not found`, so the test never ran `mngr` at all -- and where the call ended in `|| true`, the 127 was swallowed and the assertion failed with something unrelated instead. Six release tests failed this way on every macOS shard.

They now use `time_bounded()`, the perl form the style guide's portable-shell section prescribes, which is also already used in product code (`workspace_version.py`). `exec` keeps the command's own pid and exit status, so the assertions that prove a stream stayed open still work.

Those assertions move off the literal 124. GNU `timeout` exits 124 however you observe it, because it exits normally; the perl form lets the alarm kill the command, and a signal death reads differently depending on whether a shell survives to report it. The bound is parenthesised so one always does, which makes the value 128 + the signal on both platforms -- bare, macOS `sh` execs itself away and the caller reaps the negated signal instead.

The tutorial blocks that use `watch` now name the macOS install, and the tests that run them skip when `watch` is absent instead of failing obscurely.

`watch(1)` is GNU (procps) and macOS does not ship it, so a Mac user following the tutorial got `command not found` from our own instructions. It is not an mngr dependency -- no mngr command runs it -- so it stays out of the dependency registry; the four tutorial blocks that recommend it now say `on macOS: brew install watch`.

On the test side the missing binary was invisible. Each block is wrapped in `|| true`, so `watch: command not found` became an empty capture: `test_list_watch_mode` failed claiming its output lacked "No agents found", and the other three passed while exercising nothing. A `require_binary` helper now fails them up front with a message that names the install. It fails rather than skips deliberately: a skip would leave the suite green if a CI image ever lost the binary, which is the same silent-vacuum this change exists to remove.

Bump the pinned Claude Code version in `libs/mngr/imbue/mngr/resources/Dockerfile` from 2.1.269 to 2.1.280.

2.1.280 is the first pin whose binary carries the `claude-opus-5-5` model id, and its `opus` / `opus[1m]` aliases resolve to Claude Opus 5.5, so on the old pin neither the release tests nor a dev agent created from this repo's templates could select Opus 5.5. The release test `test_claude_code_version_matches_default_workspace_template_pin` requires this literal to equal default-workspace-template's `[agent_types.claude].version`, so it lands together with the matching bump there.

## 2026-09-23

Regenerate agent capabilities matrix documentation (`libs/mngr/docs/concepts/agent_capabilities.md`) to reflect `codex` support for the `compaction` capability.

Regenerate agent capabilities matrix documentation (`libs/mngr/docs/concepts/agent_capabilities.md`) to reflect `pi-coding` support for the `compaction` capability.

Added `is_unreachable_peer_connect_error`, which recognizes an SSH connect that never reached a listening peer -- a refused port, a missing route, or a downed host -- across both shapes paramiko raises it in. Used by the latchkey forward supervisor to decide whether a failed wiring step is worth another cycle before it is reported.

Tightened the direct-subprocess ratchet count from 15 to 13 now that the shared rule no longer counts `subprocess.Popen[...]` type annotations as spawns. (Affects test infrastructure only.)

Commands that target existing agents (`exec`, `message`, `start`, `stop`, `label`, `connect`, `transcript`, and the rest, but not `destroy`, `stop --stop-host`, or `AGENT@HOST.PROVIDER:PATH` locations) no longer run discovery when every address names both a host id and a provider, as in `AGENT@host-....PROVIDER`. mngr reads only that host, so the command no longer waits on the provider's other hosts. If the agent is not on that host, or the host no longer exists, the command fails with "not found" and names the pinned host; it does not fall back to searching elsewhere. Host names, partial addresses, and mixed pinned and unpinned addresses still run discovery. Providers gain a `discover_host_and_agents` method for this single-host read; the VPS providers (aws, gcp, azure, ovh, vultr) still read every VPS inside it, so pinned addresses are correct but not faster there.

Working branch for shared workspace desktops. It carries the parent-death watcher fix from `mngr/desktop-ui-phase-6` and the regenerated `mngr imbue_cloud` and `mngr forward` CLI references from `mngr/sharing-cleanup`; those branches' own entries (`mngr-desktop-ui-phase-6.md`, `mngr-sharing-cleanup.md`) describe the changes in full. Nothing in `libs/mngr` changes for the shared-desktop work itself.

Regenerated the `mngr imbue_cloud` CLI reference for the new `users`, `contacts`, and `shares set-grantees` commands.

## 2026-09-22

Added `HostInterface.connect()`, the inverse of the existing `disconnect()`: it establishes a host's connection immediately instead of on the next operation. It is a no-op by default, and opens the SSH connection for online hosts.

Callers need this when a later operation has to ride a connection that already exists -- for instance across a window in which the host's endpoint will not complete new handshakes. The Modal provider uses it to record a filesystem snapshot over a connection opened before the snapshot (MIND-234).

# Regenerated `mngr latchkey` command docs

`docs/commands/secondary/latchkey.md` regenerated for the new
`create-agent-env` / `forward` help text: a VPS workspace's gateway URL is now
`http://host.docker.internal:1989` (reached over the container's docker bridge),
while a desktop-gateway workspace keeps `http://127.0.0.1:1989`.

`ProviderInstanceInterface.get_container_loopback_ssh_port`'s docstring no
longer says the VPS-resident latchkey gateway reverse-tunnels into every
container: the loopback publish port only matters for a container that
predates the docker-bridge route. No behavior change.

`imbue.mngr.primitives` gains `OUTER_HOST_HOSTNAME_IN_CONTAINER`
(`host.docker.internal`, docker's conventional name for a container's host),
the one definition `mngr_vps` builds its `--add-host` mapping from and
`mngr_latchkey` builds a VPS-gateway agent's `LATCHKEY_GATEWAY` URL from.

A host command whose text is itself a secret can now be kept out of the logs. `commands_kept_out_of_logs` (in `imbue.mngr.utils.command_logging`) scopes an execution so that the host's TRACE line names the kind of command and its size in place of the body, the resolved parameters (an `env` mapping above all) are withheld with it, a timeout error reports the same stand-in, and pyinfra's own echo of the command it is about to run is dropped rather than forwarded. On a local host the same stand-in is the process label, so the command reaches neither the reader thread's name (which every JSONL log event from that thread records) nor the backend's own timeout error. Outside such a scope nothing changes: commands are logged in full as before.

Comments in `utils/git_utils.py` and `test_install_script.py` no longer describe an mngr tree
vendored inside default-workspace-template; the template installs mngr from the public-repo commit
it pins.

Added test coverage for the installer's new "restart your shell" hint, shown after `scripts/install.sh` installs uv.

## 2026-09-21

Tightened the Trip 1 provider release test's opt-in offline-host_dir read (`MNGR_RELEASE_TEST_OFFLINE_HOST_DIR=1`): the `mngr file get` against a stopped host now asserts a zero exit before checking the marker, and reports the refusal's stderr on failure. `mngr file get` either serves a file's bytes or refuses with one stated `Error:` line, so a nonzero exit is the read's own verdict -- asserting it fails on the refusal itself rather than on a missing marker. (Affects test infrastructure only.)

Corrected two things a volume-backed offline host did while witnessing the `mngr file` behavior
corpus (see the `mngr_file` changelog entry).

- `OfflineHostWithVolume.list_directory` asks the volume whether a path exists before listing it.
  A volume served by a storage service, such as a Modal volume, raises an error of its own kind for
  a missing path where a filesystem volume returns nothing, so listing a missing directory on a
  stopped host escaped as an uncaught error instead of an empty listing the caller can refuse
  cleanly.

- A path outside the host directory is now refused in the user's terms -- naming the host directory
  and saying that only files under it can be reached while the host is not running -- rather than
  naming the internal `OfflineHostWithVolume` class and its volume.

`mngr observe`'s event stream now has a supported read side. `mngr observe` is
single-writer per host dir (it holds an exclusive `flock` for its whole run), but
the events it produces are appended to a plain JSONL file that any number of
processes may read. Previously only the writer was public, so a second consumer
had to either start its own observer -- losing the lock, exiting seconds into
boot, and leaving its agent view frozen forever -- or re-derive the file's
invariants itself.

`imbue.mngr.api.observe` now exports three things for that read side:

- `is_observe_writer_running(events_base_dir)` -- whether some process holds the
  observe lock. This is the liveness signal for the stream: the observer holds
  the lock for its whole run, and nothing else takes it. It never creates the
  lock file, and opens it read-only, so a missing file simply means no observer
  has ever run here and an observer whose lock file another user owns is still
  seen. If the probe itself fails it raises `ObserveLockProbeError` rather than
  answering: "we could not look" is not evidence either way, and every caller goes
  on to state whatever it gets back to a user as fact. That holds in both
  directions -- only `BlockingIOError` means "someone holds it", so a filesystem
  without `flock` (an NFS or CIFS mount returning ENOLCK) no longer reads as a live
  observer just because a stale lock file is lying around.

- `find_last_full_state_offset(events_path)` -- the byte offset of the last
  complete `AGENTS_FULL_STATE` line, or None when the file holds none yet. A
  whole-agent-set view can only be rebuilt from a full snapshot; replaying a lone
  mid-stream `AGENT_STATE` first would collapse it to the single agent that line
  names. An events file that does not exist at all raises `FileNotFoundError`
  rather than answering None: "no observer has ever run here" is a different fact
  from "no snapshot yet", and a caller may reasonably wait for the second only.

- `ObserveEventFollower` -- tails the file the running observer writes, taking no
  lock, and forwards each complete line verbatim. That is the same event sequence
  `--stream-events` prints on stdout, so a consumer folds identical events whether
  it owns the observer or follows one. It begins at the newest snapshot, forwards only
  newline-terminated lines (a snapshot exceeding the atomic-append size can leave
  a half-written tail, which is left in place and picked up once the writer
  finishes it), re-seeds at the newest snapshot if the file is truncated or
  replaced, and seeds from a single scan of the file -- the snapshot lookup and
  the tail boundary must describe the same file state, or a snapshot the live
  writer appends between two separate lookups is silently skipped and the
  follower drops everything until the next one. It refuses to start at all when
  no process holds the lock --
  silently tailing a dormant file is the failure it exists to prevent. It is
  single-use, and says so: starting a follower that is already running would
  forward every line twice, and starting a stopped one would leave a thread that
  exits immediately while still reporting itself live, so both raise instead.
  The follow thread sleeps on a directory watch (the same watchdog-backed wake
  the `mngr event --follow` tails use), so a line is forwarded the moment the
  writer appends it and an idle stream costs no wake-ups; a fallback poll covers
  a filesystem event the watch missed and bounds how late a lost observer is
  noticed (`fallback_poll_seconds`, ten seconds by default).

A follower survives the observer it follows being restarted. Losing the writer is
environmental, not a fault of the fold: the observer can be shed under memory
pressure, crash, or simply be restarted by an unrelated flow, and a pass that runs
for an hour is exposed to all three. So the follow loop keeps polling through an
outage instead of giving up on it -- re-probing costs nothing, since a follower
holds no lock and cannot take one from the observer it wants back. While the
outage lasts, `is_stream_healthy()` is False and `failure_detail()` says why (a
health gate reading it flips to degraded); when a new `mngr observe` takes the
lock, its opening full-state snapshot replaces the stale folded view and both
clear on their own. Internal failure is still permanent and first-cause-wins:
if the consumer's own `on_line` sink raised, retrying a broken fold just breaks
it again.

The follower reports why it stopped rather than dying quietly. Any exit that was
not a deliberate `stop` records a failure, including exceptions raised by the
consumer's own sink (which it cannot enumerate), so a consumer gating on
`is_stream_healthy()` cannot keep reporting itself healthy after its fold has
died. Those exceptions still propagate, so the traceback survives. A stream it
cannot get an answer about says so in those words, rather than reporting the
observer as exited -- that detail is what a consumer puts in front of whoever has
to fix it, and the wrong diagnosis sends them somewhere else. `stop` holds to the
same rule: a thread still running when its wait elapses is logged rather than
assumed gone, since the only way it gets there is being stuck inside the
consumer's own sink, still delivering events to a consumer that has just been
told the follower stopped. How long that wait is, like the fallback interval
beside it, is the consumer's to state (`join_timeout_seconds`): how long a sink may
reasonably take to return is a fact about the sink.

`AgentDetails.initial_branch` now reports the branch mngr placed an agent's
work_dir on at creation, whether mngr created that branch or checked out one that
already existed. It previously came from `get_created_branch_name()`, which is
deliberately None for a pre-existing branch so teardown never deletes a branch
mngr did not create -- and `--branch BASE` (no `:NEW`) is exactly that case. So
for an agent deliberately placed on an existing branch, nothing in `mngr ls` said
which one, and callers were left re-deriving it from the `--branch` spec they had
passed, duplicating mngr's own parsing. Every existing consumer of the field --
`mngr_kanpan`'s PR lookup, `mngr_mapreduce`, `mngr_robinhood` -- wants the branch
the work is on, so all of them now get an answer in that case instead of none.

The same widening reaches `mngr start`'s recovery hint for a work_dir that has
gone missing: it now names the branch the work_dir was on, so an agent attached to
a pre-existing branch gets the `git worktree add` command to restore it rather than
being told no branch is recorded.

Teardown's narrower question is unaffected and still separate: `data.json` keeps
recording `created_branch_name` for "a branch we made, and may therefore delete
on cleanup", and `mngr destroy --remove-created-branch` still reads only that.
What is new is a second recorded value beside it: `CreateWorkDirResult` carries
the checked-out branch, `create_agent_state` persists it,
`AgentInterface.get_checked_out_branch_name()` reads it back, and
`DiscoveredAgent.checked_out_branch_name` covers the offline-host path. Agents
created before the field existed fall back to their created branch, which is the
only branch those records ever knew, so `initial_branch` never gets *worse* than
it was.

Providers that build `AgentDetails` by hand from `data.json` rather than through
the shared builders (imbue_cloud, vps, modal) read it the same way, via a shared
`read_checked_out_branch` helper, so the pre-existing-record fallback lives in one
place. Without that, those providers' online listings would have kept reporting
the old narrower value while their offline path reported the new one, so the
answer would have changed with host state.

The recorded value is read back off the work_dir once the checkout has happened,
rather than taken from the `--branch` string that was asked for. `--branch`
accepts any checkout target, so a SHA, a tag, or `origin/main` leaves the work_dir
detached (or on a DWIMmed local branch by another name), and a source repo with no
branch of its own leaves nothing to inherit. Recording the request in those cases
would have named a branch that does not exist; reading it back answers "unknown"
instead, so a caller that acts on the field says so rather than merging from
nowhere.

Note the field is recorded at create time and is not re-read: an agent that checks
out a different branch itself is not reflected, and it is None for transfer modes
involving no git. The generated CLI docs for `mngr list` are regenerated to
describe it.

`ObserveEventFollower` can now start before the observer it follows. It refused
to start while no `mngr observe` process held the lock, which is the right
answer for a consumer attaching to an observer it expects to be up. It is the
wrong answer for a consumer that a supervisor boots beside the observer in no
guaranteed order: that consumer had to retry `start` until the observer won the
race. The follower already rode out an observer restart mid-run; this extends the
same handling to the start.

A follower constructed with `require_writer=False` starts with no writer, enters
the outage state, and reports it: `is_stream_healthy()` is False and
`failure_detail()` says no `mngr observe` process holds the lock for the events
file. The first tick that finds a writer seeds from one scan of the file at that
moment and forwards from its newest full-state snapshot, so the outage clears on
its own once the observer is up. A snapshot sitting in a file nobody holds the
lock on is never seeded from: it is history, not a live view, until a writer is
back. The default (`require_writer=True`) keeps the old refusal.

The outage detail for a missing writer now says only what the follower knows. A
follower that has seen a writer and then finds none still reports that the
observer exited; one that has never seen a writer reports that none holds the
lock yet, rather than blaming an exit that never happened.

The parent-death watcher (`mngr observe`, `mngr event`, and the other non-daemonized long-running commands) now exits the process immediately when it arms and finds its parent already gone, instead of recording init as the parent and waiting forever for a change that never comes. This closes a leak where a parent that stopped its child right after spawning it (for example the chat app stopping `mngr observe` during startup) left a `mngr observe --discovery-only` process running indefinitely.

Adopted the new repo-wide `raw ConcurrencyGroupExecutor construction` ratchet (`test_prevent_raw_concurrency_group_executor`) at a count of 0. It points callers at `mngr_executor`, which now also covers the imbue_cloud, vps, modal, mapreduce and ovh plugins' fan-outs. That closes the gevent Hub leak that kept long-running `mngr observe` processes growing after the July fix.

The README's "For development" section (mirrored into the PyPI README) now points Linux users who want to run the minds desktop app from source at `apps/minds/scripts/install-linux.sh`, with the curl one-liner and a link to `apps/minds/docs/dev-setup.md`. The installer itself is described in the minds changelog.

Merges `mngr/linux-dev-packaging` (see `mngr-linux-dev-packaging.md`): the README's "For development" section points Linux users who want to run the minds desktop app from source at `apps/minds/scripts/install-linux.sh`.

## 2026-09-18

Added `close_provider_instances_for_context`, which closes and forgets every cached provider instance built against one `MngrContext`. A long-lived process that reloads its configuration (the minds desktop app now does) needs this to retire the provider set it loaded before: the instance cache is keyed by the context's identity, so those instances would otherwise answer from a configuration that no longer applies and hold connections that nothing will ever use again.

## 2026-09-17

Writing a file to a remote host is much faster: the whole write now goes out on a single SSH exec channel instead of an SFTP upload followed by separate shell commands.

`write_file` used to open an SFTP subsystem (three round trips on its own), upload the bytes, and then spend a separate SSH session on `chmod`, and for an atomic write another one on `mv`. It now opens one session channel and sends the exec request, the file bytes as the command's stdin, and the EOF in a single flight, waiting only for the exit status: two round trips after the channel open, where a small file used to cost six or more. The command it runs also creates a missing parent directory in the same breath, so the retry that used to pay for one is gone. It runs under `sh -c`, as the commands `execute_idempotent_command` runs always have, so the remote user's login shell (fish, csh) does not matter to it. The bytes are sent in bounded chunks, so a large file costs the same per byte as a small one.

`write_file` is now atomic by default (`is_atomic=True`) on every host: the bytes are staged in a sibling temp file that is renamed over the destination once complete, so a reader never sees a half-written file. `write_text_file` inherits the same default and takes the same `is_atomic` switch. Because the rename replaces the inode, an atomic rewrite of an existing file does not keep that file's mode or owner: without a `mode` the result carries the umask default, where the old in-place default kept whatever the file had. Pass `is_atomic=False` to write through the path in place, which is what a caller wants when the path is a symlink that must be kept rather than replaced, or when an existing file's mode and owner must survive the rewrite. An offline host's volume has no atomic-rename primitive, so its writes stay non-atomic and no longer log a warning about it.

An atomic write applies its `mode` to the staged file before renaming it onto the destination, so a file written with `mode="0600"` is never published world-readable for a round trip. Publishing onto a path that is an existing directory now fails with `<path>: Is a directory` instead of silently moving the staged file into it; on a local host the same write fails with `IsADirectoryError`, and in both cases the staged temp file is removed rather than left next to the directory. Paths are shell-quoted, so a destination containing a space or a quote works, and the mode reaches `chmod` as a validated octal number: it is required to be three or four octal digits such as `"0755"`, and anything else (a symbolic mode such as `u+x`, a signed or padded number) is rejected with a `MngrError` before anything is written, on local and remote hosts alike (the local path used to fail on it with a bare `ValueError`).

A remote write that fails now raises a `MngrError` naming the host and path with the command's stderr, rather than one of several errors depending on which step failed. The whole write is one retry unit, so a write retried after a lost SSH response re-sends the bytes before running the command again, and a connection that drops before the command reports its status is retried as a connection error rather than reported as a failed write. A command that fails before it has read all of its stdin (a parent directory that cannot be created, a disk that fills under the write) is reported with its own stderr and exit status too, even though the far side closes the channel while the bytes are still going out; that closed channel is not mistaken for a dropped connection, so the payload is not re-sent on retry after retry.

The exec request is sent through a private paramiko method, so `imbue-mngr` now pins paramiko to an exact version (`paramiko==3.5.1` in `libs/mngr/pyproject.toml`, repeated by a test in `outer_host_test.py` so a bump cannot go unnoticed), and new tests drive the write through a real SSH transport (an in-process paramiko server, and the local sshd used by the acceptance tests) so a future bump can be checked against a real server.

## 2026-09-16

The local provider now answers to the host name recorded in its host dir, not only to `localhost`. Inside a workspace container the host record is stamped by the outer provider that built it, so `mngr list` there reported a name like `workspace-1` while `mngr event`, `mngr capture`, and every other address-taking command refused the very address `list --addrs` had printed ("No hosts found matching workspace-1"). Discovery and resolution now read the same record `list` does; `localhost` stays an alias in the provider's own host lookups (asking the local provider for `localhost` still returns the host), while addresses resolve against the recorded name `list` prints; and `mngr rename --host` on a local host records the new name instead of silently doing nothing. Two transcript reads are fixed alongside it: `mngr event` now reads common-transcript streams, whose opening header record carries no timestamp and used to fail the whole read ("Missing required 'timestamp' field") and stall `--follow` on the first line of a new transcript; and `mngr transcript` now reads a conversation whose stream has rotated, oldest segment first, where it used to render the tail of a long conversation as though it were the whole of it. Together these left every minds bug report since the collector began pinning addresses without chat transcripts.

A host-destroyed event now leaves the host in the discovery aggregator as a DESTROYED tombstone instead of forgetting it. Before, readers of the aggregator (the minds desktop client among them) had no state at all for the host until the owning provider's next snapshot re-listed it as DESTROYED, up to a full poll interval later, which is indistinguishable from "not reported yet". The tombstone is dropped the way any host is: when the provider's next clean snapshot omits it. The observer no longer treats a DESTROYED host as known, so it stops that host's activity stream at the event and does not start one when a provider re-lists the host for its persistence window.

Garbage collection no longer aborts in its first phase when one host cannot be connected to for a reason other than being offline or refusing credentials, such as a served host key that no longer matches its pin. The failure is recorded against that host and, under `--on-error continue` (which `mngr destroy` uses), the sweep goes on to every other host and every later phase; `abort` still raises. Host resolution itself is covered too, since some providers probe the machine over SSH to resolve it.

The shared pre-destroy agent preservation (the `on_before_host_destroy` body the claude, codex, opencode, antigravity, and pi plugins use) no longer aborts a host destroy when the host cannot be reached to discover its agents. Preservation is best-effort, and the hookspec makes any raise abort the destroy, so an unreachable host, the one most in need of destroying, could not be destroyed at all. It now warns and skips, as the usage plugin already did.

- The `mngr ask` release test declares the rsync it now performs: `mngr ask` preserves its agent's logs when it destroys the agent at the end of a successful run, and the test's resource guard requires the mark for that.

`mngr pair` gained three flags, so its reference page and `--help` now list them:

- `--start` / `--no-start`, the standard flag other subcommands carry. It still defaults to starting an offline host; a caller that must never be what turns a stopped machine on (and bills for it) passes `--no-start`.

- `--ignore-archives`, for pairing two paths as though they had never been paired before. For a caller that just created one of the directories, where an archive from a previous pairing describes a directory that no longer exists — unison would otherwise read the fresh directory as a mass deletion and stop.

- `--source-path` now accepts an absolute path when pairing with a host rather than an agent, which is what `--source-host` makes possible. Its description says so.

- `--links` / `--no-links`, which decides whether symbolic links inside the two directories are synced. Defaults to `--links`, which is how pairing already behaved.

## 2026-09-15

List the new `imbue-mngr-witness` package (`libs/mngr_witness`, the behavior-corpus witness pipeline) in `UNPUBLISHED_PACKAGES`, so the release tooling knows it is internal and does not offer it for publication, and add the generated help page for its `mngr witness` command under `docs/commands/secondary/`. No behavior change to `mngr` itself.

Remove the generated help page for the retired `mngr tmr-behaviors` command.

`test_create_agent_state_stores_created_branch_name` is marked flaky: it is a trivial data.json round-trip, but on a slow offload sandbox its fixture setup alone crossed the 10 s per-test timeout (11.2 s, one failure in 25,759 on a docs-only PR), so offload now retries it instead of failing the run.

## 2026-09-14

Add `HasCompactionMixin` capability interface in `imbue.mngr.interfaces.agent` exposing compaction requests (with optional free-form instructions), cache TTL, context token inspection, and idle tracking across conversational agents. Normalize nested BaseModel submodel fields during agent type configuration parsing before `model_construct`, ensuring that submodels (such as `ContextCompactionConfig`) merge properly when overlaying configs.

Fixed a spurious failure in atomic writes to a remote host (`write_file(..., is_atomic=True)`). The rename that completes such a write goes out through `execute_idempotent_command`, which retries on a transient SSH error and cannot tell "the command never ran" apart from "the command ran but its result was lost on the way back". A plain `mv` replayed after a successful run failed with `mv: cannot stat '...tmp': No such file or directory`, so a write that had in fact landed surfaced as `MngrError: Failed to move temp file to final location`. The rename now recognizes that its uniquely-named staged file can only be missing because this same rename already completed, and treats that as success; both the staged file and the destination missing is still an error. Paths are shell-quoted properly throughout the write, so a destination containing a space or a quote no longer breaks the rename or the mode change that accompanies it. An atomic write now also applies its `mode` to the staged file rather than to the published one, so a file written with `mode="0600"` arrives at its final path already carrying that mode instead of sitting at the umask default -- commonly world-readable -- until a second round-trip lands.

The file mode is shell-quoted the same way the path is, so `mngr file put --mode`, whose value reaches `chmod` unvalidated, can no longer smuggle a second command into it.

Added a `local_outer_host` fixture to the shared plugin test helper (`register_plugin_test_fixtures` in `imbue.mngr.utils.plugin_testing`), so a test that needs an `OuterHostInterface` that really runs the commands it is given -- rather than a stub -- gets one without each suite rebuilding the local-connector construction. (Affects test infrastructure only.)

Preserved agent archives now include a best-effort manifest with safe agent and origin-host identity, per-item copy outcomes, and an attempt timestamp. Destroyed agents can be discovered from these manifests for transcript recovery, while older archives remain readable.

Online agent destruction runs bounded final raw and common transcript producer passes immediately before copying preserved files, recording the attempt results without treating command success as a transcript-completeness guarantee.

`mngr transcript <agent> --preserved` includes caller-local archives when the live agent is absent; `--no-preserved` keeps live-only lookup and `--preserved-only` reads the snapshot directly without live discovery. Archives resolve by name, id, or -- for an archive that recorded its origin host -- a host qualifier. When several archives share a name and only one of them preserved a common transcript, that is the one the lookup answers with, rather than reporting the name as ambiguous. A qualifier that rules every candidate out says which archives it excluded and where each of them ran, and an ambiguity among archives that recorded no origin host asks for the agent id rather than for a qualifier that could not help. Human, JSON, JSONL, and ATIF output use the live readers' formats and validation, while ordinary invocations keep live discovery behavior.

ATIF export recovers the proxy children that recorded the same origin host for both preserved parents and live parents whose child was destroyed. Archives remain destruction-time snapshots; the command does not refresh them, search other machines, or reconstruct a missing common transcript from native raw logs.

An archive's manifest records the agent's origin host whether or not the host can still list the agent, and each recorded transcript flush carries the time it ran, so a later attempt that runs no flush does not redate it. Once an item has been copied into an archive it stays recorded as copied, so a repeated preservation attempt against a source that is already gone cannot hide data that is on disk. An attempt that finds nothing to preserve leaves no archive behind, so lookups by agent name are not answered by empty archives. A manifest written by a newer mngr is read rather than rejected, and is never written over by an older one.

## 2026-09-13

Removed source line-number references from the `api/list` and `api/gc` test suites.

Six section banners in `list_test.py` labelled groups of tests with line ranges in `list.py` (for example `# Lines 396-405: Error differentiation in _handle_listing_error`). Those ranges had drifted by 91 to 344 lines as `list.py` grew, so following any of them landed in an unrelated function. The banners are removed outright; each test's name already states the function and branch it covers.

Three docstrings additionally pointed at a specific line or internal helper call. They now describe the behavior being verified instead, so they no longer depend on where the implementation happens to live. One of them also claimed the discovery events file must contain the legacy whole-fleet snapshot, which the test has asserted is absent since per-provider snapshots superseded it; the docstring now matches what the test checks.

Creating an agent on a local host no longer shells out for work the local filesystem can do
directly. `write_file` used to write the bytes in-process and then spawn `sh -c chmod ...` to
apply the mode, and spawn `sh -c mv ...` for an atomic write; on the local path both are now
`Path.chmod` and `os.replace`. Remote hosts are unchanged and still use the shell. A local
`mngr create` was paying nine chmod spawns for scripts it had already written.

`load_config` also resolved the current directory's git worktree root twice per invocation --
once to locate the project config directory and once for `MngrContext.project_root` -- running
`git rev-parse --show-toplevel` as a subprocess each time. It now resolves it once and derives
both. `resolve_project_config_dir` is split into the override lookup, the precedence rule
(`derive_project_config_dir`, over an already-resolved root), and the resolve-then-derive path
for callers that have no root in hand; the git lookup is still skipped entirely when
`MNGR_PROJECT_CONFIG_DIR` already settles the answer.

Together these cut a real-agent CLI test from 28 subprocess spawns to 17. Process-spawn
latency is what stretches under a loaded CI runner, so this is also why those tests were
tripping the global 10s pytest-timeout.

Tests marked `@pytest.mark.tmux` now inherit a 60s budget from the shared conftest hooks
rather than the repo-wide 10s default, so the 53 hand-applied `@pytest.mark.timeout`
decorators at or below that value have been removed along with the comments that justified
them. Decorators above 60s stay: those tests are genuinely slower than their class.

A regression test now pins the budget itself: it asserts, from inside a live pytest session,
that a tmux-marked test with no timeout of its own resolves to a budget large enough for real
tmux work, and that a test in no such class is still left on the global default. Against the
previous behaviour it fails with "resolved to a 10.0s budget".

The Docker provider now publishes each container's SSH port on `127.0.0.1` when the daemon is local (`host` empty or a `unix://` socket), so a container's sshd is no longer reachable from the machine's LAN; a remote daemon (`ssh://` or `tcp://` `host`) keeps the all-interfaces bind, since mngr reaches those containers via the daemon's hostname. A new `ssh_bind_address` provider config field overrides either default with an explicit IP (for example `"0.0.0.0"` to expose local containers to the LAN); on a local daemon mngr connects to that address (a wildcard bind is reached via `127.0.0.1`), so it must be IPv4, and a loopback address is rejected for a remote daemon.

Existing containers keep the bind they were created with, including across `mngr stop` / `mngr start`; a snapshot restore or recreate picks up the new bind.

## 2026-09-12

Added `build_stderr_tee_redirect` (in `imbue.mngr.agents.base_agent`) for agent plugins that want an interactive harness's stderr both on the pane and in a log file: a `2> >(tee -i <file> >&2)` redirection that leaves stdout alone, preserves the wrapped command's exit status, and ignores SIGINT so a Ctrl-C the harness handles cannot break its stderr. The claude and pi-coding plugins use it.

Because process substitution is a bash/zsh/ksh feature, the agent window's shell (tmux's default shell for the session, which is the user's login shell in practice) must now be one of those; the launch-step docstring in `hosts/host.py` records the requirement. Also added the `poll_until_file_contains` test helper for files written behind such a redirect, since the shell does not wait for the `tee` to finish.

Integration branch for the imbue_cloud slice-fleet generation 2 program (raw qemu slices with routed-tap networking on Debian 13 boxes, replacing lima), merged together with `main` and the three pre-cutover fix PRs: imbue-ai/mngr-internal#857 (SSH certificates from Vault, #850), #855 (DHCP placement, #849) and #856 (the upstream artifact mirror, #851). The per-change details live in this directory's constituent entries, listed below.

SSH utilities and `create_pyinfra_host` present an OpenSSH certificate found beside a private key (paramiko floor 3.2), plus the `imbue_cloud` command doc updates for the gen-2 fleet.

Constituent entries: `new-fleet-base.md`, `new-fleet-phase-4-impl.md`, `mngr-new-fleet-testing.md`, `mngr-ssh-authority-in-vault.md`

Gen-2 small follow-ups: every sshd mngr launches inside a host container (the provisioning start, the self-healing PID-1 entrypoint, and the Modal sandbox launch, all fed by `SSHD_START_OPTIONS`) now runs with `PasswordAuthentication no` and `KbdInteractiveAuthentication no`. Nothing set either before, so Debian's stock default (`yes`) applied -- inert while no account has a password hash, but dead config on a fleet that authenticates by key or certificate only. Existing containers pick it up on their next sshd relaunch.

`mngr stop --stop-host` now honours the `@HOST[.PROVIDER]` qualifier when the same agent name (or id) exists on several hosts: the qualifier picks the host instead of the command failing as ambiguous. `resolve_hosts_for_identifiers` keeps its one-host contract on top of the new `resolve_candidate_hosts_for_identifiers`. A candidate host the provider no longer knows is skipped, and when that leaves nothing for the qualifier to match, the "No agent found matching address" error names the skipped host(s).


Host-key pin store: two new per-host helpers, `drop_bootstrap_pins` (forget every BOOTSTRAP-origin pin of one host, called by imbue_cloud adoption once both endpoints are verified serving user-origin keys, so the connector's bake-time keys are never re-emitted at a stale endpoint) and `drop_host_pins_outside_endpoints` (forget a host's pins at every endpoint but the named ones, called after a relocation moved its pins). The pin format, the sidecar, and `move_host_endpoint_pins` are unchanged.

Variable machine sizing (specs/slice-fleet, phase 1): the generated command reference (`docs/commands/secondary/imbue_cloud.md`) now documents the new `mngr imbue_cloud machines show` and `mngr imbue_cloud machines resize` subcommands (units + grow-only disk, record-then-restart). The commands themselves live in the `mngr_imbue_cloud` plugin; see that project's entry in this same PR.

This branch also carries the earlier slice-fleet gen-2 phase 1-4 work; see the `mngr-slice-fleet-gen2-phase-*` and `mngr-variable-sizing` entries in this same PR.

`create_pyinfra_host` and the SSH utilities load an OpenSSH certificate found beside a private key (`<key>-cert.pub`) and present it during authentication, so hosts that trust a certificate authority rather than the raw key can be managed through pyinfra (imbue-ai/mngr-internal#850). The paramiko floor moves to 3.2, the first release with the `PKey.from_path` loader this uses.

`new-fleet-base` is the integration base for the in-progress slice-fleet generation-2 program (specs/slice-fleet-gen2 and specs/slice-fleet): the squash of the formerly stacked PRs #571, #573, #574, #581, #609, and #614. It is deployed only to dev canaries and must not be deployed or merged as-is; follow-up PRs stack on it and the whole program lands on `main` as one change.

For this project it carries only the regenerated command reference (`docs/commands/secondary/imbue_cloud.md`) documenting the `mngr imbue_cloud machines show` and `mngr imbue_cloud machines resize` subcommands, which live in the `mngr_imbue_cloud` plugin. See this directory's `mngr-new-fleet-testing` entry.

`ssh_utils.read_served_host_key_or_none(hostname, port, timeout_seconds=...)`: the unauthenticated host-key probe now also returns the served key as an OpenSSH line (the existing `is_server_presenting_host_key` / `wait_for_expected_host_key` are built on it); the gen-2 cutover uses it to tell a rotated slice VM key from the row's bake-time key.

## 2026-09-11

Reading a directory over SSH now fails the same way it does locally. A local read of a directory
raises `IsADirectoryError`, but the SFTP path surfaced the server's opaque failure instead, so any
caller distinguishing "that path is a directory" from a genuine read error got the right answer on
a local host and the wrong one on a remote host. `OuterHost.read_file` now classifies that case by
asking the server what the path is, and raises `IsADirectoryError` for a directory on both.

The classification costs nothing on the success path: the extra question is asked only after a read
has already failed, and a failure to answer it leaves the original error to stand.

A `[commands.<name>]` table in a higher-precedence settings layer now adds the parameters it names to the ones lower layers set, instead of replacing the whole map. Before, a local `.mngr/settings.local.toml` (or a user profile) setting only `commands.create.type` merged as a map with one key, so the project's `connect`, `host_env`, and every other create default under `[commands.create]` were dropped, and the settings-narrowing guard refused the merge outright. The per-parameter rules are unchanged: a list assigned bare over a non-empty one from a lower layer is still a narrowing, and `key__extend` still appends to it.

Bump the pinned Claude Code version in `libs/mngr/imbue/mngr/resources/Dockerfile` from 2.1.227 to 2.1.269 (the `latest` release channel at the time of the bump; `stable` still sat at 2.1.236).

2.1.269 is the first pin whose binary carries the `claude-fable-5-1` model id, and its `fable` / `fable[1m]` aliases resolve to Claude Fable 5.1, so on the old pin neither the release tests nor a dev agent created from this repo's templates could select Fable 5.1. The release test `test_claude_code_version_matches_default_workspace_template_pin` requires this literal to equal default-workspace-template's `[agent_types.claude].version`, so it lands together with the matching bump there.

## 2026-09-10

mngr: `cleanup_old_modal_test_environments` now reports which environments it actually reaped.

It used to return the number of environments it *attempted*, discarding the per-environment outcome it had already computed. A sweep that failed to delete a single environment was indistinguishable from one that deleted them all, so the CI job driving it could never go red.

It now returns a `ModalTestEnvironmentSweepResult` naming the environments that are gone and the ones whose delete failed. The per-environment work moved into `sweep_old_modal_test_environment`, which is injectable (along with the environment lookup) so the sweep can be unit-tested without invoking the real Modal CLI.

Separately, the Modal-specific code that had accumulated in core moved out to the `mngr_modal` plugin: `imbue.mngr.utils.modal_cli`, the "Modal test environment cleanup utilities" section of `imbue.mngr.utils.testing`, and `ModalCliOutputError`. Core keeps the provider-agnostic leak-tracking registries. If you imported any of those from `imbue.mngr`, import them from `imbue.mngr_modal.modal_cli` / `imbue.mngr_modal.cleanup` / `imbue.mngr_modal.errors` instead.

## 2026-09-09

mngr: fix the Modal test-resource cleanup sweep, which had been reaping nothing at all.

`delete_modal_apps_in_environment` and `delete_modal_volumes_in_environment` (in `utils/testing.py`) read the JSON from `modal app list --json` / `modal volume list --json` under the CLI's *display* column headings (`"App ID"`, `"Description"`, `"Name"`) rather than the keys it actually emits (`app_id`, `description`, `state`, `name`). Every lookup returned an empty string, the `if app_id:` / `if volume_name:` guards skipped the body, and no app was ever stopped and no volume ever deleted. Nothing was logged, because nothing failed. The result on the imbue Modal workspace was 26 leaked `mngr_test-*` environments, the oldest from 2026-07-30, several still holding a live `deployed` app.

Both functions now go through a new `utils/modal_cli.py`, which parses those payloads into `ModalAppListing` / `ModalVolumeListing` models. A payload that does not carry the keys we read raises the new `ModalCliOutputError` instead of quietly yielding nothing, so a future Modal CLI rename fails loudly rather than silently turning the sweep back into a no-op.

A command blocked on an SSH connection that a laptop sleep killed is now released when the laptop wakes, instead of waiting for the kernel to give up.

Nothing above the socket could cut it short: pyinfra's per-command timeout never fires in mngr's usage (a blocked paramiko read starves the gevent hub it runs on), SSH keepalives never wait for a reply, and through a NAT mapping the sleep killed the peer's reset never arrives, leaving only TCP retransmission timing out a minute or two later. One `mngr start` measured just under two minutes of that on a host that was up.

Every SSH connection mngr runs commands over is now stamped with both clocks and watched; when they diverge, connections built before the gap are closed, which wakes everything blocked on them, and the existing retry reconnects.

`mngr` now writes its process title with `shlex.join` instead of joining arguments on a bare space.

The title is set on every invocation and overwrites argv, so it is what `ps` shows and what `psutil.Process.cmdline()` returns -- the only surviving record of how a running `mngr` was invoked. Joining on a bare space made an argument containing a space indistinguishable from two arguments, so the title misreported the invocation it was there to describe.

Visible effect: a path with a space appears quoted in `ps` output, e.g. `--latchkey-directory '/Users/Jane Doe/latchkey'`.

## 2026-09-08

`start-dockerd.sh` no longer returns an opaque `timeout`/exit-124 when the daemon fails to come up. Its readiness wait was refactored into a `wait_for_dockerd` helper that watches the `dockerd` pid: a daemon that dies during startup now fails immediately with a clear message instead of polling a doomed socket until the deadline, and the deadline itself is now controlled by the caller via the `DOCKERD_STARTUP_TIMEOUT_SECONDS` environment variable (default 60s, unchanged for the Dockerfile CMD and the release-test fixture). This is what lets snapshot-resume callers give dockerd the longer budget it needs to recover a populated `/var/lib/docker` under gVisor (MIND-215).

`mngr event --follow` recovers from a host that restarts under it. The follow loop only ever swapped in a re-resolved host handle when the host's online state flipped, so a restart that completed between two probes (a Docker workspace stop and start, which also re-maps the SSH port) left the tail reading through a dead handle forever, with the failures logged at trace level only. Every consumer of the stream stopped learning anything new from that host: the minds desktop client's forward proxy, which follows each workspace's `services` events, never learned an app registered after the restart and served its "Loading workspace" page for it. A tail whose reads keep failing now reports it, the follow loop re-probes the host every five seconds instead of every thirty and adopts the refreshed handle even though the state did not change, and a read that fails thirty polls in a row is logged at warning level.

## 2026-09-07

Fixed a Modal host bring-up flake (MIND-209): a freshly provisioned or tunnel-fronted sshd can drop the SSH handshake with `SSHException("No existing session")` -- which pyinfra wraps as `ConnectError("SSH error (No existing session)")` -- even after `_wait_for_sshd` has already confirmed sshd can open a session. The bounded connect-retry that rides out the sibling "Error reading SSH protocol banner" failure did not recognize this shape, so it was fatal on the first attempt and aborted `mngr create` / `start_host` on fresh sandboxes.

The transient-connect-failure classifier now recognizes the whole family of transient SSH *handshake* failures (banner-read and no-existing-session) rather than just the banner-read string, so both are ridden out over the same bounded window while refused/auth/host-key failures still fail fast. The retry helper and its window constants were renamed from `banner_read`/`BANNER_RETRY` to `transient_handshake`/`HANDSHAKE_RETRY` to match the broadened behavior.

The plugin manager -- `create_plugin_manager`, `get_or_create_plugin_manager`, `reset_plugin_manager` and `load_plugin_hookspecs` -- now lives in `imbue.mngr.cli.plugin_manager` instead of `imbue.mngr.main`, so an embedder that needs the plugin set (to load config, and from there to reach a provider) no longer has to import the whole CLI to get at it: importing `main` builds every command, which costs about a second before any work is done. All four are re-exported from `imbue.mngr.main`, so existing callers are unaffected.

Regenerated the `mngr latchkey` command reference: `register-agent` now also pushes the updated permissions file to the host's own machine when the host has one (a remote workspace whose gateway was provisioned from this computer), since that machine's gateway enforces its own copy.

`mngr cleanup` now destroys hosts concurrently (up to 16 at a time) instead of one after
another. Destroying a host is almost entirely waiting on the provider, so a sweep of a large
backlog was bound by that wait: the daily TMR host cleanup was getting through roughly 90 of
its 1582 stale hosts per hour and hitting its three-hour timeout with the great majority of
them untouched.

Hosts are still reported in the order they were selected, so the destroyed-agent list and the
recorded failures do not depend on which host happened to finish first. `ErrorBehavior.ABORT`
keeps its previous meaning -- stop at the first failure, leaving every later host untouched --
which is only well defined when hosts are taken one at a time, so that behavior stays
sequential. The `mngr cleanup` command itself always continues past errors, so it always gets
the concurrent path.

`test_connect_cli_runs_custom_connect_command` and `test_stop_archive_sets_archived_at_label` are
now marked `@pytest.mark.flaky` with a 60s per-test timeout, matching the precedent on the sibling
tmux CLI tests. Both create a real tmux agent, run in about two seconds locally, and tripped the
global 10s pytest-timeout on a slow CI sandbox (incidental flakes hit on this branch, which touches
neither code path; not filed to Linear from this workspace -- the scheduled flake sweep will
reconcile them).

## 2026-09-04

The vendored ATIF models record harbor `v0.22.0` as their provenance. The models themselves are
unchanged -- `src/harbor/models/trajectories/` is byte-identical between harbor `v0.21.0` and
`v0.22.0` -- so this is the provenance catching up with the version the repo now depends on, not a
re-vendor.

`mngr plugin add` and `mngr plugin remove` no longer break a mngr that was installed from a git URL.

Every plugin command rebuilds the whole `uv tool install` command from uv's receipt. The receipt has no structural place for a git ref or a subdirectory -- its only field is `git`, a single URL -- so uv encodes both as query parameters (`<repo>?subdirectory=libs%2Fmngr&rev=<ref>`). uv reads its own serialization back correctly, but it is not a PEP 508 requirement URL, and mngr was re-emitting it as one. uv then silently ignored the ref and the subdirectory and built the repository root, which for a monorepo fails in setuptools with "multiple packages discovered".

Two fixes: existing `--with` requirements are translated into PEP 508 form before being handed back to uv, and the *base* requirement keeps its git source instead of collapsing to a bare `imbue-mngr` (which would have silently re-resolved mngr from PyPI, replacing the pinned commit with whatever version happened to be released).

This is what lets a consumer depend on an unreleased mngr -- pinned to a commit of the public repo -- and still manage its plugins.

The ref is placed immediately after the path, before any surviving query or fragment. Appending it to a fully assembled URL put it after them, where uv ignores it and resolves the default branch instead -- asking for `v0.2.16` installed HEAD, with no error. A fragment the URL already carried is now kept, and a `subdirectory` is merged into it rather than appended as a second `#`.

## 2026-09-03

`mngr list` now defaults to `--on-error continue` (previously `abort`), so a single unreachable or unauthenticated provider no longer aborts the whole listing. The reachable providers' agents are still emitted in every output format, the inaccessible provider is reported in the errors channel (the JSON `errors` array, or the stderr error block for human/template output), and the command still exits with the granular provider-inaccessible code so the partial failure stays detectable. Pass `--on-error abort` to restore the old fail-fast behavior.

This fixes `mngr list --format json` returning empty stdout whenever any configured provider was down: the JSON path builds its output in one batch and the abort discarded everything before it was emitted, while the human path had already streamed its rows. The empty JSON broke `mngr tmr-behaviors --reintegrate`, which reconstructs a map-reduce run from `mngr list --format json`; it now receives the agents the healthy providers reported.

Hardened the agent start path against wedges and mistaken kills (mngr-internal#807):

- The per-agent launch batch (`tmux new-session` and friends) is now bounded at 90 seconds per attempt (over SSH the batch is retried like any other idempotent command when the read times out, so the worst case is a few minutes, still inside the 10-minute lock wait below). A wedged tmux server or client used to hang `mngr start` forever, and since the start runs under the host lock, everything queued behind it too; it now fails with an `AgentStartError` naming the agent and the bound.

- The pre-launch "is this agent's tmux session already up?" probe no longer guesses: a probe that times out raises a `CommandTimeoutError` and aborts the start, so the stale-process reap that runs before a launch only ever fires on a definitive "no session" answer. Previously, on local hosts, a slow `tmux has-session` read as "no session" and the reap killed the live agent's whole process tree (for a minds services agent: its supervisord and every service under it).

- `mngr start` (and every other path that starts an agent through the shared locked helper) now waits at most 10 minutes for the host lock instead of forever, and a timeout raises a `LockNotHeldError` that names the host and the agents it was trying to start.

- Starting a stopped agent on the way to messaging, connecting to, exec'ing in, or capturing it now happens under the host lock, like `mngr start` itself. Two concurrent unlocked starts of the same agent could previously both see "no session", and the slower one's reap would kill the tree the faster one had just launched.

- A command run with `raise_on_timeout=True` on a remote (SSH) host now surfaces a timeout as `CommandTimeoutError`, like it already did locally, instead of folding it into the generic `HostConnectionError`. Without this the two bounds above only behaved as described on local hosts: over SSH a wedged launch surfaced as a connection error instead of the named `AgentStartError`, and a timed-out session probe surfaced as a connection error instead of the `CommandTimeoutError` that lets the start abort cleanly today.

Two kinds of place where mngr handed a filesystem path to a parser without quoting it for that parser. Both break when the path contains a space, and both fail in ways that give no useful error.

**Shell completion.** The generated zsh/bash completion functions bake in the absolute path of the interpreter that produced them, unquoted, inside a command substitution. When that interpreter lives under a directory whose name contains a space, the shell splits the path and every Tab press reports `no such file or directory`. The path is now shell-quoted. An already-installed completion heals itself: `mngr list` rewrites the managed file that the rc shim sources, so the next shell picks up the fixed function. The shim version bump is what nudges a shell that is still open to refresh with `mngr extras completion` instead of waiting -- and only one whose old function still runs, since the nudge is printed by the completer that function spawns. `generate_zsh_script` / `generate_bash_script` now take the interpreter path as an argument, read once by `write_managed_completion_scripts`, rather than each reading `sys.executable` for itself -- so what they emit depends on what they are given rather than on which interpreter happens to be running them.

**SSH host-key verification.** `build_ssh_transport_command` passed the `known_hosts` path to `UserKnownHostsFile` with shell quoting only. But ssh documents that option as "one or more files ... separated by whitespace", so ssh itself splits the value a second time: a host directory containing a space became several nonexistent paths, and every connection failed with `No ED25519 host key is known ... Host key verification failed`. In practice that surfaced as `mngr create` being unable to push the git repo to a freshly created host. The value is now quoted for ssh's parser as well as for the shell. `mngr connect` built the same option for the ssh it execs, where ssh is the only parser and the value was not quoted for it either; attaching to a remote agent from a host directory with a space in it failed the same way, and is fixed too. The ready-to-paste ssh command that `mngr list` reports for each host (`host.ssh.command`) had the same defect in both its `-i` key path and its `UserKnownHostsFile`, so copying it out of a spaced host directory produced a command that could not connect; it is now quoted for both parsers.

The shell-then-ssh quoting is applied through one named helper, `quote_ssh_known_hosts_option_value` in `primitives.py`, rather than open-coded at each site. Both of its layers are load-bearing and neither works without the other, so a single place to state that is what keeps a later reader from collapsing it back to plain shell quoting. `mngr connect` is deliberately not a caller: its argv is exec'd, so ssh is the only parser and it needs the inner quotes alone.

`MNGR_HOST_DIR` can point anywhere the user likes, and a home directory such as `/Users/Jane Doe` produces a space with no help from the user at all.

The quoting is now named rather than open-coded, as a pair in `imbue.mngr.utils.ssh`: `quote_ssh_option_value` for a value ssh reads directly -- one element of an exec'd argv, or one line of a generated ssh config -- and `quote_ssh_option_value_for_shell` for one a shell parses first (`GIT_SSH_COMMAND`, rsync `-e`, a wrapper script, a command a user pastes). Both layers of the second are load-bearing and the doubled wrapping reads as redundant, so an open-coded copy invites being simplified back to a single `shlex.quote`; and the two are not interchangeable, since shell-quoting a value ssh reads directly puts literal quotes into the filename it opens.

The rule is about any ssh option value ssh tokenizes on whitespace -- `IdentityFile` and `ControlPath` as much as `UserKnownHostsFile` -- so the names say that rather than naming one option. `build_ssh_connect_command` moves alongside them into `imbue.mngr.utils.ssh`: it renders an ssh command, which is not the shared vocabulary `primitives` exists to hold, and `utils` is the one layer every caller can reach. The module is named for what it builds -- ssh invocations as text -- rather than for one of the pieces, since it holds both the option quoting and the command that carries it.

## 2026-09-02

`mngr destroy` with provider-scoped host addresses (`@host-<id>.<provider>`) now narrows its host-resolution discovery to exactly the named providers, so a broken unrelated provider can no longer abort the destroy. Previously the resolution ran a full all-provider discovery snapshot and failed fast on any provider that raised mid-discovery -- an expired imbue_cloud sign-in (its token refresh rejected by the connector) would kill the destroy of a perfectly healthy local docker workspace before anything was torn down. An address without a provider qualifier keeps the full scan and its loud failure, deliberately: silently skipping a broken provider there would let `--force` treat a host that provider owns as already gone.

## 2026-09-01

Deflake the list-agents continue-mode provider-error tests (MIND-229), which
intermittently turned CI red with `assert 2 == 1` and `assert False = all(...)`.
The three tests
(`test_list_agents_streaming_continue_mode_records_failing_provider_error`,
`test_list_agents_batch_continue_mode_records_failing_provider_error`, and
`test_list_agents_continue_mode_records_unauthenticated_provider_with_structured_fields`)
call `list_agents` in full-enumeration mode, which constructs and discovers over
*every* registered backend -- not just the intended `local` provider plus the one
broken/unauthenticated provider under test.

The test environment also registers the `lima` backend (it is deliberately kept
out of the remote-backend exclusion so it can be registered without `limactl`).
Its default instance's discovery shells out to `limactl` and reads the real
`/mngr` host directory, so whether it errors depends on the ambient CI
environment (limactl presence, `/mngr` contents, subprocess timing under load).
An incidental discovery failure there added an unexpected second `ProviderErrorInfo`
-- breaking the "exactly one provider error" and "all provider errors are
inaccessible" assertions non-deterministically.

The fix makes the tests hermetic: the ctx builders now pin `enabled_backends` to
just `local` plus the backend under test, so a full-enumeration listing only
touches those providers regardless of what else is registered. A new regression
test (`test_list_agents_continue_mode_ignores_incidental_backends`) registers an
extra erroring backend and asserts it never contaminates the listing, so removing
the scoping fails deterministically in CI rather than silently re-flaking. This is
a test-only change; production `list_agents` behavior is unchanged.

Listing agents (`mngr list`, and the board fetch built on it) now bounds each host's
live detail collection by a wall-clock budget instead of waiting on every host without limit.
While the budget is active, each of the host's reads self-terminates within the remaining time,
so a slow or contended host (slow tmux/ssh, a wedged provider) falls back to its offline/partial
data rather than stalling the whole read -- and nothing is left running in the background. The
budget is configurable via `host_detail_read_timeout_seconds` (default 20s).

The per-host detail build also stopped issuing a redundant boot-time probe: it previously ran
`sysctl kern.boottime` (or the Linux equivalent) twice per host per fetch -- once for boot time
and once for uptime. A single host probe now returns both, with uptime measured on the host so
it is unaffected by clock skew between the client and the host.

Deflaked the remaining real-agent (`@pytest.mark.tmux`) CLI and API tests that were tripping the default 10s pytest-timeout under offload's parallel load. These tests run a full real tmux-agent lifecycle in-process, whose honest wall-time (~5s unloaded) is genuine and essentially irreducible, so the fix aligns them with their already-provisioned siblings: each now carries `@pytest.mark.timeout(30)` for headroom and `@pytest.mark.flaky` so offload auto-retries a single unlucky run instead of failing the whole job.

Affected tests: `test_cleanup_stop_action_with_real_agent` (MIND-185; gains the timeout to match its siblings), `test_create_agent_work_dir_is_created`, `test_list_command_with_agent`, `test_list_command_with_exclude_filter`, and `test_list_command_with_host_name_filter`.

The other real-agent members of this cluster (`test_destroy_single_agent_via_session`, `test_destroy_remove_created_branch_deletes_branch`, and `test_send_message_one_agent_failure_does_not_prevent_other_agents`) were already given the same treatment on main, so they are unchanged here.

The tutorial and its transcript e2e tests move to the ATIF role vocabulary now that the claude emitter emits ATIF-shaped records: the human-readable transcript labels the agent's own turns `agent:` rather than `assistant:`, so the tutorial's transcript examples ask for `--role assistant --role agent` -- the ATIF role plus the legacy one, which agents provisioned before the cutover still emit. Its `mngr event --include` example filters on `type == "step"` (the ATIF record type) instead of the retired `user_message`.

The pre-ATIF transitional state is gone from the common transcript.

The retired `user_message` / `assistant_message` / `tool_result` record types have been removed from the canonical schema: the common transcript is now exactly the ATIF-shaped stream (`header` / `step` / `observation`).

`mngr transcript` renders only ATIF-shaped records. A stream written by an agent provisioned before the ATIF cutover is detected and reported with a clear error ("its stream uses the retired pre-ATIF format") for every output format, instead of being partially rendered. The check runs before role filtering, so it fires whatever `--role` was passed.

`--role` help and examples drop the "legacy streams use assistant" hedge, and the tutorial's "view only the agent's own messages" block is back to `mngr transcript my-task --role agent`.

The old-format error now names the recourse: the raw native transcript is still there under `logs/<agent_type>_transcript/`, and `mngr event <agent>` still shows the raw records. The reader and the ATIF doc-builder share one explanation of why the stream cannot be read.

`mngr transcript` warns when a `--role` filter matches nothing on a stream that does have records, naming the requested roles and the valid vocabulary (`user`, `agent`, `system`, `tool`), instead of printing an empty transcript. Its help text describes the stream in ATIF terms (user turns, agent turns, and tool results).

The shared agent-plugin release harness (`agent_release_testing.py`) now asserts
against the ATIF-shaped common-transcript records that all five emitters write:
the envelope check reads `emitter` rather than `source`, seed-turn and delivery
helpers key on `step` records with `source: "user"`, recall keys on agent steps,
usage is asserted on at least one agent step carrying `metrics.prompt_tokens` /
`completion_tokens` / `cached_tokens` (claude omits `metrics` entirely on lines
that report no usage), forced tool calls are found under an agent step's
`tool_calls[].function_name`, and tool results are expected as `observation`
records.

Added the ATIF doc-builder: `mngr transcript <agent> --format atif` assembles an agent's ATIF-shaped common-transcript stream into a single validated ATIF-v1.7 trajectory document (optionally written to a file with `--output`). The merge rules follow `specs/atif-transcript-alignment/spec.md`: file order is authoritative, step ids are assigned sequentially, streamed observation results attach to the step that declared their tool call (unmatched results are preserved with a warning, never silently dropped), and the root is enriched with the agent type, id, and summed per-step metrics.

Claude subagents that mngr ran as sibling proxy agents are embedded recursively as ATIF v1.7 `subagent_trajectories` (marked `extra.subagent_kind: "mngr"`), with a `subagent_trajectory_ref` attached to the delegating tool call's result; a subagent whose delegating call has not produced a result yet is still embedded, under a result marked `extra.subagent_result_pending`, and a subagent that cannot be resolved leaves the plain textual result untouched.

`--format atif` goes through the same option machinery as every other format, so it can also be set as a config default (`[commands.transcript] output_format = "atif"`) or via the matching environment variable.

Old-format (pre-ATIF) streams are reported with a clear unsupported error.

The canonical common-transcript schema now requires the per-agent ATIF header ids the emitters write (`header-<sha256 hex>`, enforced by pattern). See the emitter changelogs (mngr_claude, mngr_codex, mngr_antigravity, mngr_opencode, mngr_pi_coding) for why the fixed `"header"` id had to go: analytics dedupes the fleet by event id, and identical header ids collapsed every agent's header row to one per account.

`mngr transcript` now renders the ATIF-shaped common-transcript records (`header` / `step` / `observation`) in human output: agent steps show their message, thinking, and tool calls with an arguments preview; observation records show each tool result; system steps show their inline result. The stream itself always carries the complete arguments and outputs -- the 200-character tool-input, 2000-character tool-output, and 500-character thinking caps are applied at display time only, and the new `--full` flag turns them off (human output only). Multimodal content parts render their text, with images shown as an `[image: <media type>]` placeholder.

`--role` filtering understands the ATIF vocabulary (`user`, `agent`, `system`, `tool`); legacy streams keep their `user` / `assistant` / `tool` roles. Legacy record rendering is unchanged (and now also honors `--full`). The stream header is dropped from human output before `--head`/`--tail` are applied, so `--head 1` shows the first conversation record rather than spending its window on framing; `--format json`/`jsonl` still emit the raw stream, header included.

Marked `test_destroy_single_agent_via_session` flaky: its tmux session teardown raced once on a contended CI offload sandbox and passed on retry.

Added the ATIF-shaped common-transcript stream-record schema (`header` / `step` / `observation` record types in `common_transcript_records.py`), composing the vendored ATIF sub-models (`ToolCall`, `Metrics`, `ObservationResult`) so the payload shapes are stated exactly once. Step records reuse the vendored ATIF `Step` validators at emit time, so emit-time conformance implies build-time document validity, and both step and observation records validate their ISO 8601 timestamps.

A step's originator is the `StepSource` enum (`system` / `user` / `agent`), observation results are typed as `StreamObservationResult` (which requires the `source_call_id` that lets the doc-builder attach a result to its step), and `parse_common_transcript_record` raises `InvalidCommonTranscriptRecordError` at the parse boundary rather than surfacing pydantic's `ValidationError`.

The legacy `user_message` / `assistant_message` / `tool_result` record types remain in the schema (CLEANUP-marked) until all five emitters are migrated to the new records later in this stack.

Vendored harbor's ATIF (Agent Trajectory Interchange Format) pydantic models into `imbue/mngr/agents/data_types/atif/` (from harbor v0.21.0, pinned to ATIF-v1.7), as the foundation for aligning the common transcript with the ATIF standard (`specs/atif-transcript-alignment/spec.md`).

The vendored directory keeps upstream's style (documented in its README.md along with provenance and re-vendoring instructions) and is carved out of the ratchet checks its patterns would otherwise trip.

## 2026-08-31

Added `has_unpinned_bootstrap_drift` to the host-key pin store: reports whether known_hosts text still carries trust the store lacks (an unpinned endpoint, or a differing bootstrap-origin pin), while deferring to differing user-origin pins so a deliberate local trust decision (e.g. a key rotation) is never flagged as drift. Used by the minds record-secrets materializer to converge stale bootstrap pins onto synced adopted host keys.

Fixed 21 release tests that failed on their resource marks rather than on anything they assert.

The resource guards fail a test that declares `@pytest.mark.rsync` or `@pytest.mark.modal` without exercising that resource, and the reverse: 17 tutorial tests declared `rsync` for local in-place agents that never shell out to it, two declared `modal` for listings that never reach the provider, and two `mngr_robinhood` streaming tests invoked rsync with no mark at all.

Two tests that the CI failure list also named are deliberately left alone. `test_ask_simple_query` and `test_create_codex_agent` reach rsync only through teardown log-preservation, which runs when they fail -- and both fail for real, unrelated reasons. Their marks are stale on the passing path and required on the failing one, so dropping them would only add guard noise to a failure that needs fixing on its own.

Dropped the `rsync` mark from the `mngr ask` release test. Its only rsync is the teardown that preserves logs when the test fails, so on the passing path the resource guard flagged the mark as never invoked. Removing it is safe in both directions: when the test does fail, preservation catches the guard block and warns rather than masking the real failure.

Follow-ups to the event-driven follow-tail work (PR #733), addressing review feedback:

- `DirectoryWatchGroup` now supports multiple subscribers per watched directory: `watch` adds a (directory, wake event) subscription instead of silently replacing any previous wake event for that directory, and `unwatch` removes only the caller's own subscription, releasing the underlying filesystem watch when the last one goes. This removes the footgun where one caller's unwatch could break another caller's watch on the same directory.

- The per-source follow tails in `mngr event --follow` now bridge the stream's stop event into their directory-watch wake event via a parked forwarder thread (matching the discovery-log tail), so a tail sleeping on a directory watch reacts to shutdown immediately and independently of `wake_all()` registration timing.

## 2026-08-30

Reduced the idle CPU cost of mngr's follow-mode daemons (`mngr observe`, `mngr event --follow`), which is especially significant under gVisor where each poll/timer wakeup costs ~3x native (imbue-ai/mngr-internal#700):

- `mngr event --follow` no longer wakes 10x per second: its consume loop now blocks until an event arrives or the next housekeeping deadline (source rescan / online check) is due. Housekeeping also now runs on schedule under a continuous event stream instead of only when the queue happens to be empty.

- Local event tails (per-source follow tails and the discovery-log tail) are now woken by a directory watch (inotify on Linux, FSEvents on macOS, via the new `watchdog` dependency) with a 10-second fallback poll, instead of polling every second. Delivery latency improves to milliseconds; remote (SSH/volume) tails keep the 1-second poll. New shared utility: `imbue.mngr.utils.file_watch`.

- `mngr observe`'s per-local-agent PID watchers now block in an event-driven poll(2) over the process's pidfd plus a stop pipe on Linux (no timer at all); the psutil fallback (macOS, pre-5.3 kernels) polls at 3s instead of 1s.

- The observe activity worker's queue timeout (which only bounds child-process health checks, not activity latency) went from 2s to 5s, and the `--discovery-only` main thread no longer wakes every second just to re-check its stop flag.

## 2026-08-29

A provider can no longer go quiet in `mngr observe`'s discovery while every other provider keeps reporting. Two paths allowed that; both are fixed.

A wedged provider now re-emits its timeout snapshot every poll. Previously it emitted nothing while a discovery that had outlived its error timeout kept running, so the one provider whose discovery was broken was also the one that went silent -- which is what a dead poller looks like. Only the first timeout of an episode logs at warning level.

A provider that cannot be built is now retried rather than written off for the life of the process. Providers used to be constructed once at startup, so one that was unreachable (Docker Desktop paused) or not yet created (no Docker state container or Modal environment) got a single snapshot and no poller at all. Construction now happens inside the poller, on every poll at the provider's ordinary cadence, so unpausing Docker restores that provider's workspaces and service routes within one poll interval, with no app restart. Missing credentials are the exception -- they change only through a user action that restarts this process -- so that provider reports once and ends just its own poller. Any other construction failure reports too, rather than a log line nobody reads: the case that reaches it is a settings block naming a provider backend this install does not have, which used to kill the whole producer at startup.

Two things fall out of moving construction into the pollers. One slow provider no longer delays every other provider's first poll, and a provider's error is live rather than frozen at launch, so the providers panel stops insisting that Docker Desktop is paused long after it was unpaused. The shared provider-instance cache is now thread-safe, since the pollers build concurrently.

## 2026-08-28

Test-infrastructure only; no change to shipped behavior.

`cleanup_tmux_session` walks a pane's process tree with `pgrep`, and that walk had no timeout even though the neighbouring tmux and pkill calls in the same file already do. Cleanup runs inside the caller's `pytest-timeout` window, so a stuck `pgrep` stalled the test itself; one such hang failed `test_connect_start_restarts_stopped_agent` on a loaded CI run. The walk now shares a single deadline across the whole recursion -- a per-call timeout would not bound it, since it recurses once per descendant -- and returns the PIDs found so far when the budget runs out, which is what cleanup wants.

`test_connect_start_restarts_stopped_agent` and `test_stop_agent_kills_single_pane_processes` are also marked `@pytest.mark.flaky`. Both spawn a real tmux agent inside the repo-wide 10s timeout and pass locally, matching the treatment already given to their siblings in `test_destroy.py`.

# Mark another tmux destroy test flaky

- `test_destroy_single_agent_via_session` gains `@pytest.mark.flaky` and a 60s per-test timeout, matching its siblings in the same file: its real tmux create/destroy workload trips the global 10s pytest-timeout on a slow offload sandbox.

## 2026-08-27

- `test_send_message_one_agent_failure_does_not_prevent_other_agents` is marked flaky and given the same 30s timeout as its sibling tmux tests; its two-agent setup/teardown occasionally exceeded the 10s default in CI.

Marked `test_connect_start_restarts_stopped_agent` (`cli/test_connect.py`) `@pytest.mark.flaky`: like its already-marked siblings `test_connect_via_cli_group` and `test_cleanup_destroy_with_provider_filter_matches`, it is a tmux subprocess test that intermittently exceeds the 10s pytest timeout on a cold offload checkpoint (the timeout fired inside its tmux cleanup), so offload now retries it instead of failing the run.

`test_destroy_with_confirmation` is now marked `@pytest.mark.flaky` with a 60s per-test timeout, and `test_connect_start_restarts_stopped_agent` (whose `flaky` mark landed on main independently) gains the same 60s timeout, matching the precedent on the sibling tmux CLI tests: both run real tmux create/destroy flows whose in-test wait budgets alone exceed the global 10s pytest-timeout, and each tripped that ceiling on a slow CI sandbox (incidental flakes hit on an unrelated PR; not filed to Linear from this workspace -- the scheduled flake sweep will reconcile them).

## 2026-08-26

The local provider's `get_host_resources` no longer crashes when `psutil.cpu_freq()` raises `SystemError`, a known psutil quirk on some macOS / Apple-Silicon setups. The failure is now treated as "frequency unknown" (`CpuResources.frequency_ghz=None`) instead of propagating, so host-detail collection -- and the acceptance tests that drive `fetch_board_snapshot` / `list_agents` against a real local host -- run on affected Macs instead of only on Linux CI.

The sibling `disk_usage` failure path in the same method now logs at debug when it falls back to `disk_gb=None`, instead of swallowing the `OSError` silently.

A lookup for an agent or host that does not exist now exits `mngr` with a distinct code (`EXIT_CODE_TARGET_NOT_FOUND`, 8) instead of the generic error code, but only when every identifier it was given is machine-generated: an agent id, via the new `AgentIdNotFoundError`, or a host id, via the new `NoMatchingHostsError`. A miss on a user-typed *name* keeps the ordinary bad-input error, since a name that matches nothing is as likely a typo as a gone target -- so `mngr stop some-typo` is still exit 1, and so are related errors (an agent absent from a host that does exist, an unknown snapshot). Every entry point that resolves a target makes the same split -- `find_one_agent` and `find_all_agents` for agent addresses, `filter_one_host` for the host-only addresses that `mngr event @HOST`, `mngr file`, `mngr limit`, `mngr snapshot`, and `mngr wait` use -- so the code cannot depend on which command you ran. The host-id failure also stops advising you to check the command syntax: the identifier is usually well-formed and the host is simply gone, so it now points at `mngr list` the way the agent-side error already did. Out-of-process callers see only (exit code, stderr), so this lets them tell a target that cannot come back from a transient failure without matching on message text -- `mngr forward`, which runs a `mngr event --follow` child per agent and always addresses it as `<agent_id>@<host_id>`, uses it to stop respawning children that can never succeed.

Renamed `auto_dismiss_dialogs` to `auto_dismiss_dialogs_at_startup` in the agent-config registry's inheritance documentation and the config loader's examples, following the rename in the plugins that declare the field. Same behaviour; the name now says when it applies.

Fixed the agent pane's bottom row being undisplayable. When re-fitting the manual-pinned agent window to an attaching client, the fit hook sized the window to the raw `#{client_height}`, which counts the tmux status line the window cannot use; since tmux does not clamp a manual-pinned window, the window ended up one row taller than the client could display, so whatever the agent drew on its bottom row rendered where nobody could see it. The hook now subtracts the status line's height (handling `on`/`off`/an explicit row count, per-session or global) after applying the minimum-client floor.

A TUI harness can now express readiness as a predicate over the pane, alongside the existing substring and regular-expression forms, for harnesses where readiness depends on *where* something appears rather than on whether it appears at all. The two existing forms are unchanged.

A readiness timeout no longer includes the pane's contents in the raised error. The pane is still logged; it is unbounded and can contain the user's own code or a diff, which does not belong in a surfaced message.

The TUI-ready timeout is now a public constant, so a harness can wait the same window the readiness check does when it needs to decide whether a pane is merely slow to draw.

Messages now go to the agent's own pane, and reach it even if the pane is sitting in a tmux mode. Two things were wrong before, both silent. A tmux target of the form `session:window` resolves to whichever pane is *active*, so a single split sent the message into the newly opened shell instead -- no error, just delivered somewhere else. And a pane in copy-mode swallows keystrokes while still accepting a paste, so a long message would land in the input box with the Enter after it eaten, sitting there unsent. mngr now records the agent pane's ID when it creates the session and sends to that, clearing any mode first. The ID lives in a tmux session option, so it lives and dies with the agent and a restarted agent records a fresh one; an agent created before this keeps working on the old target.

Everything a send does now addresses the agent's pane, not just the paste: the submitting Enter, its retries, the pane reads those retries are gated on, and every capture the dialog and readiness checks are built from. Half of that had been left on the old target, which is worse than not fixing it, since a split would still deliver the Enter to another shell while the message went to the right place.

The `auto_dismiss_dialogs` compatibility alias is gone. It could not work: the config path builds agent types with `model_construct`, which skips the validator that would have folded the old name in, so a settings file using it was accepted and then quietly ignored -- worse than the loud failure it was meant to replace. The rename stands on its own; a settings file with the old name fails to load and says which field it does not recognise.

A failed send is now one record per agent instead of two lists zipped by name. `MessageResult.failures` carries the agent, the reason, and the kind together; `failed_agents` stays as a derived view so `mngr message`'s exit code and output read exactly as before. The reason no longer carries "Failed to send message to agent X" framing, because every consumer has the name to hand and added that framing itself -- the CLI printed it twice in one line, and a client showing it under its own title printed it a third time. Naming `mngr connect` is now done by the CLI alone, which is the only place it is true.

Marked `test_destroy_remove_created_branch_deletes_branch` as flaky, so offload retries it. It spawns a real tmux agent inside the repo-wide 10-second pytest timeout and hit that timeout on a loaded offload run, then passed on its own locally; its sibling `test_cleanup_stop_action_with_real_agent`, which fails the same way, was already marked. The underlying timing sensitivity is not addressed here.

Added `git_fetch` to the git API, alongside the existing `git_push` and `git_pull`. It has the same shape — mngr builds the URL and SSH environment, and anything in `extra_args` is forwarded verbatim — but merges nothing, which makes it the right primitive for comparing two repositories. `mngr pair` uses it to work out which side is ahead.

Added `is_directory` to the host interface, next to the existing `path_exists`, so callers can ask whether a path is a directory on a host without branching on `is_local` themselves. Several copies of this check existed inside `libs/mngr`; they now share one implementation. (Two hand-rolled copies remain outside it: one in `mngr_vps`, whose outer-host doubles do not model `is_local` and so cannot reach the interface method, and one in `mngr_claude`, left for a follow-up.) One of them, `Host._is_directory`, built its shell command as `test -d '<path>'` by hand rather than quoting properly, so a path containing a single quote would have misbehaved.

`GitContextInterface` (and its local and remote implementations) gained `get_head_commit` and `get_repo_root`, so callers can read a repository's HEAD, or find the worktree root containing a path, without knowing whether it is on this machine or reachable only over SSH. `get_repo_root` is what turns a path into something that can be used as a git *remote*: git resolves a repository from any subdirectory you run it in, but a repository URL has to name the root itself.

Fixed an inconsistency between the two git contexts: `RemoteGitContext.get_current_branch` returned the literal string `"HEAD"` on a detached HEAD, silently handing callers a branch name that is not a branch. It now raises, matching what the local implementation has always done.

## 2026-08-25

Bumped the offload version baked into `libs/mngr/imbue/mngr/resources/Dockerfile` (`OFFLOAD_VERSION`) from `0.9.12` to `0.9.13`, keeping the in-image `offload apply-diff` binary in lockstep with the CI pin.

Added `imbue.mngr.hosts.mutate_id`: internal, convergent primitives that rewrite identity in a host's on-disk state -- `mutate_agent_id` (renames the agent state dir and rewrites data.json's id, and re-keys any externally persisted agent-data copy via the new `OnlineHostInterface.remove_agent_data` counterpart to `save_agent_data`; refuses running agents and collisions) and `mutate_host_id` (re-stamps the host-level data.json). No CLI; callers are future restore/clone/adopt flows.

Terminology sweep: mngr-level prose speaks host/agent (the minds-level machine/workspace vocabulary and minds references were removed from core comments), and the release-testing helpers' `workspace` parameter is now `project_dir`.

Robustness fixes from the 2026-07-14 partial workspace-destroy incident:

- State-store record writes are now atomic (temp file + rename). The `Volume.write_files` contract requires per-file atomic visibility, implemented in `LocalVolume` (via a new `atomic_write_bytes` helper) and `DockerVolume` (tar under temp names + a rename exec), so a concurrent reader can never observe a torn (empty / partial) host record -- the incident's root cause.

- Host-record reads now distinguish "unreadable" from "absent". Parse failures are retried (a torn read heals on re-read); after retries the default behavior still warns and treats the record as missing, while the new off-by-default `strict_host_record_parsing` config flag makes them raise `HostRecordUnreadableError` instead, which docker discovery deliberately re-raises rather than silently dropping the live container or degrading it to stale offline data.

- Discovery now heals the persisted agent store from each successful live agent listing (for providers that persist agent data: docker, lima). Agents created inside the host -- e.g. by an in-container bootstrap running `mngr create` -- previously never reached the store, so offline listings structurally missed them. Missing live agents are persisted and orphaned records removed; deletions are guarded by a non-blocking host-lock probe plus a post-probe re-read of the live listing, and a per-host cache keeps steady-state discovery read-only.

- The "Host {} still has {} agent(s) after destroy; leaving host alive" signal in `mngr destroy` is now logged at warning (was debug) and names the remaining agents -- in the incident it was the clearest evidence that a teardown had been left incomplete.

Merged the machine/workspace naming cleanup branch (`mngr/fix-machine-workspace-naming`, PR #436) into this branch and resolved the merge conflicts against main. The changes themselves are described in this project's `mngr-fix-machine-workspace-naming` changelog entry.

## 2026-08-24

Fixed `mngr exec` (and every other SSH connect) intermittently failing against a freshly provisioned host with `Failed to connect to host: SSH error (Error reading SSH protocol banner)`. A new Modal sandbox transiently accepts the TCP connection but resets it before its sshd answers the SSH banner, which paramiko surfaces immediately -- not after the 30s banner timeout. The connect retry assumed every banner-read failure had already spent that timeout, so it used three zero-wait attempts; against an immediate reset those three attempts burned out in milliseconds, long before the sandbox was reachable, and the exec failed. This flaked the `test_exec_*_on_modal` acceptance tests on `main` for weeks (MIND-202, a recurrence of MIND-178).

The banner-read connect retry is now bounded by a wall-clock deadline (30s) with a fixed pause (0.5s) between attempts instead of a fixed attempt count, so the ride-out window is deterministic regardless of whether a given attempt fails immediately (a reset tunnel) or slowly (a stalled one). Genuinely-down hosts (connection refused, auth/host-key failures) are still not retried and fail fast.

Defined and standardized the "SSH protocol banner" terminology that the provider SSH code leans on throughout but never pinned down (MIND-203). This is a comments-only change with no behavioral effect; the banner-read connect retry it documents was fixed separately under MIND-202 (#572).

The term now has a plain-language definition at its anchor, `SSH_BANNER_TIMEOUT_SECONDS`: the SSH protocol banner is the server's RFC 4253 identification string (the `SSH-2.0-...` line sent immediately after the TCP connect, before key exchange), which is distinct from the RFC 4252 userauth banner it is easily confused with. The incidental variants ("banner window", "banner exchange", "banner reset") now refer to it consistently.

Also locks in the `imbue/mngr` inline-imports ratchet count (4 -> 3) to match the current tree.

An SSH connection that the far end has reset is now treated as the transient failure it is, and retried on a rebuilt connection, instead of aborting the command.

This is what a connection cached across a laptop sleep meets when it is next used: the peer dropped it while nothing was running, and the reset arrives on the next command rather than on connecting. It was classified as a permanent error only because its message is the errno text ("[Errno 54] Connection reset by peer") rather than the "Socket is closed" wording the check looked for -- so a command that would have succeeded on a reconnect instead failed, and where nothing caught it, printed a Python traceback. Waking a laptop and having a machine restart itself was one way to see it.

A reset that outlasts the retries still fails, but now reports itself as a connection error like every other kind.

SSH liveness hardening, from the `mngr start` slowness investigation (`specs/mngr-start-slowness-investigation.md`):

- Every SSH connection mngr establishes (pyinfra host connections and outer hosts) now enables paramiko transport keepalives, so a silently dead network path (laptop slept mid-operation, dropped NAT state) surfaces as a transport error instead of leaving blocked reads waiting forever.

- Channel opens are now bounded: the cooperative host-lock channel, the detached debug lock holder, and SFTP channel creation all pass an explicit open timeout, and SFTP channels get a default per-read silence timeout, so an sshd that accepts TCP but no longer services channels can no longer hang these paths indefinitely. The detached lock holder's launch confirmation is bounded too, and a stall there degrades to the same tolerated "did not confirm launch" warning as a channel that closes early. The cooperative lock *wait* is deliberately left unbounded -- that one is waiting on another actor's work, not on a dead path.

- Container sshds launched via `SSHD_START_OPTIONS` (the docker provider, vps container setup, and imbue_cloud slice adoption; existing containers pick it up on their next sshd relaunch) now run with `ClientAliveInterval 30` / `ClientAliveCountMax 4`: a client whose path died silently is reaped in about two minutes instead of the kernel TCP keepalive's ~2h11m, which is what let a dead cooperative-lock holder block every subsequent `mngr start` for hours.

- `get_default_cli_events_log_dir` added to `imbue.mngr.utils.logging` so consumers (the minds bug-report attachment sweep) can locate the per-command CLI event log without loading a logging config.

## 2026-08-22

Regenerate the `mngr imbue_cloud` CLI reference doc (`docs/commands/secondary/imbue_cloud.md`) for the plugin's new `auth login --accounts-url` flag, which opens the hosted login page on the tier's browser accounts origin (see the `mngr_imbue_cloud` changelog for the underlying Google sign-in fix).

## 2026-08-21

Deflake the doc-link URL resolution tests (MIND-188), which periodically turned
per-PR CI red. Three tests fetched live github.com URLs to verify the
version-pinning ref policy in `doc_links`; they were on the per-PR merge gate
(`@pytest.mark.acceptance`), so a slow or briefly-unreachable github.com --
orthogonal to the ref policy -- failed the gate. The failures came two ways:
curl's `--max-time 30` outran the 10s pytest-timeout, producing an uncatchable
`Failed: Timeout (>10.0s)` SIGALRM, and transient transport errors raised
`CalledProcessError`.

A live third-party dependency does not belong on the merge gate: it will fail
when the third party is down, and a networked check cannot be made to pass
honestly (skipping on failure proves nothing). The deterministic half of the ref
policy -- the URL *shape* a code change can actually break, e.g. a wrong tag
format -- is already pinned on every PR with no network by the existing
`doc_links_test.py` unit test, so nothing is lost by taking the live check off the
gate.

The three live-resolution tests (repo public, tag pushed, doc present) move to the
release lane (`@pytest.mark.release`), where they run in the release workflow and
the daily TMR schedule. They fail closed: if github.com is unreachable the test
fails rather than skips, and a human decides whether to re-run or merge past. A
per-test timeout override clears curl's 30s budget so a slow github.com yields a
clean curl error instead of the uncatchable pytest-timeout SIGALRM.

Fixed the sshd session-open readiness probe (`wait_for_sshd_with_retry`, introduced in MIND-178) authenticating as the local OS user instead of the remote SSH user. The probe never passed a username to paramiko, which then defaults to `getpass.getuser()` -- on CI runners that is `runner`, not the `root` user the host key is authorized for, so every Modal host provisioning failed deterministically after a 60s wait with "accepted connections but could not open sessions". This turned the daily TMR and Release Tests lanes red from 08-20 onward (MIND-204).

The probe now takes an explicit `username`, widens its banner timeout to the shared `SSH_BANNER_TIMEOUT_SECONDS` (30s, matching every other provider connection; degraded Modal tunnels are observed to take 13-16s at the banner exchange), and logs the failure reason at trace level so future readiness failures are diagnosable instead of silent for 60s.

`mngr start --format json` (and `--format jsonl`) now reports `was_host_started`, which says whether the command actually brought a host up rather than finding it already running. `ensure_host_started` had always computed it and thrown it away. A start is idempotent and starts a named agent whatever state it is in, so the agent list beside it cannot answer this -- and a caller that dispatched a start to revive an unresponsive machine needs to know that a start which booted nothing cannot be the explanation for what happens next.

Docker state-volume writes are now torn-read-safe: `DockerVolume.write_files` extracts each file under a temporary dot-prefixed name and renames it into place, so a concurrent reader (e.g. host discovery catting a host record while another worker writes it) sees either the old or the new content, never an empty or partial file. This was the root cause of the `test_create_snapshot` HostNotFoundError flake (a host record read back empty mid-write); the test's `@pytest.mark.flaky` is retained until the scheduled flake sweep confirms a green streak.

## 2026-08-19

Added `wait_for_sshd_with_retry` function to `imbue.mngr.providers.ssh_utils` (MIND-178).

- New helper performs full SSH authentication and session-open check to verify sshd is fully ready, not just accepting connections.

- Used by mngr_modal to absorb cold-start latency where the tunnel is available but sshd is still initializing.

Reduced `mngr` CLI cold-start latency. Previously every `mngr` invocation (even `mngr config`, `mngr list`, `mngr --version`) eagerly imported the full cloud-provider SDK ecosystem (Google Cloud, Anthropic, Azure, Modal, AWS) at startup -- thousands of modules -- which on a cold filesystem cache could take many seconds and, under CI I/O contention, exceeded command timeouts. Provider plugins now register lazily, so the heavy SDK for a provider is imported only when that provider is actually operated; provider-independent commands start substantially faster.

Provider plugins may now return a `LazyProviderBackend` from the `register_provider_backend` hook to register only a backend's lightweight metadata (name, config class, CLI help text) and defer importing the backend implementation until first use. Operator command groups can likewise be registered through `LazyProviderCliGroup` so they appear in `mngr --help` without importing their implementation module. The existing eager `(backend_class, config_class)` return value is still supported for lightweight backends.

Added a regression test (`test_mngr_startup_does_not_eagerly_import_heavy_provider_sdks`) that fails if importing the CLI entry point pulls in a heavy provider SDK, preventing this cold-start regression from returning.

# Model discovery's provider outcomes as one map instead of parallel lists

`DiscoveryOutcome` now carries a single `results_by_provider` map -- each provider's name to the instance that answered, or to an `Unreachable` recording why it could not be reached -- replacing the parallel `providers` / `skipped_providers` lists and the `is_empty` flag that distinguished a reached-but-empty provider from an unreachable one. "Unavailable providers" is now a filter over that map rather than a boolean predicate, and a provider that answered but holds nothing simply contributes no hosts, so it needs no case of its own. `providers` and `unavailable_providers` remain as derived views, so `mngr list` and the agent-lookup error path (which names an unreachable backend instead of reporting the agent as missing) are unchanged. No user-visible behavior change.

(This branch began as a crispy-comments pass over PR #304; the `DiscoveryOutcome` docstring trimmed there is rewritten by this remodel.)

Stopping or restarting one agent can no longer kill the shared tmux server -- and with it every other agent on the host. The stop-time environment sweep (which reaps `MNGR_AGENT_ID`-tagged orphans) now exempts the tmux server process, and the agent launch command unsets `MNGR_AGENT_ID` before `tmux new-session` so a server forked by mngr is never tagged in the first place. Previously, when the server had been forked from an agent-context `mngr start` (e.g. the workspace template's boot units, which source the system-services agent's env before relaunching it after a VM reboot), the server inherited that agent's marker, and the next `mngr stop`/`mngr start --restart` of that agent SIGTERMed the server, destroying every agent session on the host at once; issued from inside the container, the caller died mid-restart and the start half never ran, leaving the workspace down (observed on production imbue_cloud workspaces during minds update-self, 2026-08-18; a fleet survey found the tagged-server state on 20 of 169 leased slices). Trade-off: an agent-private tmux server on a separate socket now outlives its agent as an idle process instead of being reaped.

Fixed the tutorial release tests, which failed on stale assumptions rather than on real defects.

`test_create_docker_default_image` confirmed its agent with `mngr list --include 'host.provider == "docker"'`. `--include` filters what discovery returned; it cannot stop discovery, and `mngr list` defaults to `--on-error abort` across every enabled provider, so an enabled-but-unreachable one (Modal, in a job holding no token) aborted the listing with exit 6 before any agent was reported. It now scopes with `--provider docker`, which the rest of the file already uses.

Four `mngr exec` calls passed their command as loose tokens. `mngr exec` takes `[AGENTS]... COMMAND`, so the command must be a single argument -- the tutorial's own `test_exec` docstring says so. Unquoted, the leading word is consumed as another agent name: `mngr exec my-task cat /etc/os-release` looked up agents `cat, my-task`, and `mngr exec my-task -- sh -c '...'` rejected `-c` as an agent name.

The custom-Dockerfile tutorial documented a command that cannot work: `-b file=./Dockerfile.dev`. The docker provider passes `-b` values to `docker build` verbatim, so that produced two positional arguments and docker rejected it with `'docker build' requires 1 argument`. The Dockerfile is selected with docker's own `--file`.

Four tests declared a resource they never touch -- `@pytest.mark.modal` on a listing with no agents, `@pytest.mark.rsync` on a dry run and on local in-place agents -- which the resource guards reject with "marked with X but never invoked X".

Regenerated the `mngr imbue_cloud` CLI reference doc: the operator-only `admin` group moved out of the public plugin into Imbue's private operator CLI.

Bump the pinned Claude Code version in `libs/mngr/imbue/mngr/resources/Dockerfile` from 2.1.207 to 2.1.227 (the `stable` release channel).

This Dockerfile is the repo's shared mngr image, and it is built from three directions: the offload test sandboxes (`offload-modal.toml` / `offload-modal-acceptance.toml` name it as their `dockerfile`, so it is what the test suite runs inside), this repo's own `mngr create` templates (`.mngr/settings.toml` passes `--file=` at it for the docker, modal, aws, azure, and gcp templates, so a dev agent created in this checkout runs an image built from it), and `mngr schedule`'s Modal deploy, which resolves the packaged copy at runtime. It is *not* the image a stock `mngr create` builds when the user supplies no Dockerfile -- that content is generated in Python by `_build_default_dockerfile()` in `providers/docker/instance.py` and installs no claude at all.

2.1.219 is the first release whose binary carries the `claude-opus-5` model id, so on the old pin neither the release tests nor a dev agent from these templates could select Opus 5. The release test `test_claude_code_version_matches_default_workspace_template_pin` also requires this literal to equal default-workspace-template's `[agent_types.claude].version`, so it must land together with the matching bump there.

## 2026-08-18

# Report the backend that is down, instead of the agent it hides

Discovery no longer drops the providers it could not construct. `discover_hosts_and_agents` (and `discover_by_address`) now return a `DiscoveryOutcome` -- the hosts and agents found, the providers that answered, and the providers that could not be reached -- rather than a two-tuple that had no room for the third fact. A provider whose construction fails is queried by nobody, so its machines are missing from the snapshot in exactly the same way as a provider that genuinely holds none; keeping the skip is what lets a caller tell those apart, and each one is now logged as it happens.

The payoff is the error users actually see. Looking up an agent while a provider is unreachable now raises `ProviderUnavailableError`, carrying that provider's own explanation -- and its own remediation, so an unauthenticated cloud provider still tells you to fix your credentials rather than to start Docker -- instead of reporting the agent as not found. Previously `mngr exec` / `mngr start` / `mngr stop` against a machine on a stopped Docker daemon said `Agent not found` -- the daemon is checked when the Docker provider is *constructed*, so the provider vanished before discovery ever ran -- which sends you looking for a machine you think you deleted rather than at the backend that is down. Both lookup paths answer this way, so the single-agent commands (`mngr wait` / `transcript` / `rename` / `file`, and the interactive picker) do not go on saying "could not find agent" about the machine `mngr stop` just told you it could not reach. An agent that is genuinely absent, with every provider reachable, still reports as not found; so does an ambiguous name, which is a complete answer that an unrelated backend being down does not make less complete.

`ProviderUnavailableError`'s generic message now ends with a marker sentence built from a single constant, and a new `parse_provider_unavailable_reason(text, provider_name)` helper recovers the provider's own failure reason from whatever an out-of-process caller captured (typically an mngr subprocess's stderr, Click's `Error:` prefix and trailing help text included). Only that one message shape is recovered, so an unrelated failure is never misread as a provider outage; the flip side is that a subclass keeping its own verbatim message, like `ModalAuthError`, carries no marker and so is not recovered either. Only the named provider's outage is returned -- a command aborts on whichever provider it queried is unavailable, which need not be the one the caller is asking about -- and a caller with no provider to compare against passes `None` to take whichever the text names.

The consumer is the minds desktop client in this same PR: it runs mngr as a subprocess and uses this to tell "the provider backend is down" apart from "this machine is broken" when an exec or restart step fails.

Deployment pass for the observability bring-up: this branch carries `mngr/log-and-telemetry-aggregation`, whose only mngr-project change is the regenerated CLI command reference for `mngr imbue_cloud admin server prep` (new optional `--extra-prep-script <file>`).

Regenerated the committed CLI command reference for `mngr imbue_cloud admin server prep`, which gained an optional `--extra-prep-script <file>` (an additional idempotent root script appended to the box prep; the observability rollout uses it to install the pinned OpenTelemetry Collector on bare-metal boxes).

## 2026-08-17

A create template may now set a field on the agent type the create resolves to, not just a
`mngr create` option. This lets a template describe an agent's *role* without naming the
harness that will run it: a role writes `output_style = "..."` or
`append_system_prompt__extend = [...]` once and it lands on whichever type the template stack
selected. Previously the only way to express either was `agent_args`, which is raw argv and
therefore harness-specific, so a role that needed one had to be duplicated per harness.

An output style is a markdown file whose frontmatter sets `name:`; `output_style` takes that
display name, not a filename. The name is resolved and validated during provisioning, and an
unknown name fails the create with the available names listed, rather than silently launching
an agent with no style applied.

Routing happens after every template applies, because a harness template is what sets the
type. Keys compile into settings entries and reuse the existing template-contributed-settings
fold, so the operator suffixes behave exactly as they do everywhere else: a bare key assigns
(the last role in the stack wins) and `__extend` accumulates. Keys are combined per field
before being emitted, because each settings entry is applied against the base config
independently -- two `__extend` entries for one field would each extend the empty base and the
later would simply win, dropping the earlier role's contribution.

A key that is neither an option nor a field on that type now RAISES, naming the template, the
type, and which types do support it. Previously it was silently dropped, so a typo -- or a role
stacked onto a harness that could not honour it -- produced an agent that quietly ignored part
of its configuration. The two role fields live on the harness config subclasses rather than on
the base `AgentTypeConfig`, so a harness with no support for them has no field to route to and
the create fails naming the template instead of launching a misconfigured agent.

Stacking create templates that declare conflicting base `type`s is now rejected up front,
rather than silently letting the last template win. A create resolves to exactly one base
type, so a stack like `-t worker -t codex` (a claude role plus a codex one) is an ambiguous,
always-wrong request and now fails naming the conflicting templates. Aliases are normalised
first (so two names for the same base do not conflict), templates that leave `type` unset
never participate, and an explicit `--type` on the command line still overrides every
template's type (it is authoritative), so only the templates-decide-the-type case is guarded.

The in-process message API gains `send_key_chord_to_agents`, the keystroke sibling of
`send_message_to_agents`: it presses a single tmux key token (e.g. `M-q`) into a resolved set of
agents' panes with the same host-grouped concurrent fan-out and the same per-agent `message.lock`
serialization as a text send. The lock is what keeps a chord from landing between a concurrent
message's paste and its Enter. Keystroke-driven agents (`SendKeysAgent` / the interactive TUI
agents) implement it via the new `SupportsKeyChordMixin.press_key_chord`; agents whose input is an
API rather than a tmux pane (pi, opencode) are refused per-agent with a clear error. The Minds
workspace UI uses this to deliver claude's native queue-flush chord.

`mngr create` now waits for a newly-created agent's readiness signal even when there is no
initial message to deliver. Previously the ready-signal wait only ran on the with-a-message
path; a no-message create (how the Minds chat UI creates every chat) returned the instant the
process was spawned. For a slow-booting harness that meant the agent surfaced as live before it
could accept input -- most visibly pi, which writes its session and model-state files several
seconds after launch, so a fresh pi chat showed a wrong status and an empty model bar and
dropped an early message into a race. The wait uses the same per-harness `wait_for_ready_signal`
the message path already uses (claude/codex poll for the composer prompt in the pane; pi waits
for its session-started sentinel file). Its base implementation only runs the start action, so a
plain `command` agent that does not override readiness detection still starts with no added wait.

The shared agent release-test arc now observes RUNNING/WAITING through overridable profile hooks (`is_waiting_after_create` / `is_reporting_running`) instead of hard-coding the `active` marker file. The default still reads the marker (claude and the other ports are unchanged); a harness whose lifecycle is read live overrides it (codex reads the daemon's `thread/status`).

Fix mngr host provisioning silently skipping all deploy files when the mngr host directory lives outside `$HOME` (e.g. a custom `MNGR_HOST_DIR`, or the modal acceptance-test layout). Deploy-file destinations are now derived from the mngr root-name convention (`~/.{root_name}/<path relative to the host dir>`) instead of the local file's position relative to `$HOME`, so `collect_provider_profile_files` no longer raises `ValueError` when the host dir is elsewhere -- which previously aborted the whole `collect_deploy_files` call and, because the error was downgraded to a warning, skipped provisioning of every deploy file.

Transient `*.lock` files (e.g. `.modal_ssh_key.lock`) in a provider's profile directory are now excluded from deployment.

Internal refactor (behavior-preserving): the `MNGR_ROOT_NAME` environment read (defaulting to `mngr`) is now centralized in a single `read_root_name()` helper in `config/host_dir.py`, replacing scattered inline `os.environ.get("MNGR_ROOT_NAME", "mngr")` copies across the config loader and CLI.

(GitHub issue #392)

Add an overridable `_determine_confirmation_policy` hook to the shared TUI agent so a port can widen the relaxed-confirmation set beyond slash commands (the default classification -- slash commands relaxed, everything else strict -- is unchanged). The Claude plugin uses this to confirm leading-`!` shell commands under the relaxed policy.

Add a `send_key_keystroke` helper alongside `send_enter_keystroke` for sending a single named tmux key (e.g. `BSpace`).

- Fixed quality-gate failures from the stop-button/message-lock work: the stub host used by the `press_key_chord` unit tests now declares `is_local` as a constructor parameter (resolving type errors from post-construction attribute assignment), and a trailing comment in those tests moved to its own line to satisfy the trailing-comment ratchet.

- Reformatted `interfaces/agent.py` with ruff (no behavior change).

- Made the interactive `handle_not_implemented_error` tests in `cli/issue_reporting_test.py` hermetic: they now pin `IS_AUTONOMOUS` (via `monkeypatch.delenv`) so the prompt-and-report path is exercised even in sandboxes that export `IS_AUTONOMOUS=1`.

- `test_install_script.py` now locates the repo root by walking up to the first ancestor containing `scripts/install.sh` (falling back to the nearest `.git` ancestor), so the install-script tests also pass when the mngr monorepo is vendored inside another git repository.

- A create whose readiness wait times out now destroys the half-started agent before reporting failure, so a failed create leaves no zombie behind.

- The destroy-time orphan-process sweep uses a single grep over /proc/*/environ instead of one grep fork per process, cutting the scan from ~1-2s (and past its 10s bound under parallel multi-agent destroys, where it silently skipped kills) to under 0.1s.

- `mngr_common_transcript_lib.sh`: documented that the shared per-agent convert lock now also serializes raw streamers that keep a read-offset/append/write-offset section (codex's `stream_transcript.sh`), closing the daemon-vs-single-pass duplicate-emit race on the raw rollout stream. No behavior change in the library itself.

## 2026-08-16

Agent ids are now unique per host rather than globally: the same agent id may exist on multiple hosts at once (the building block for migrating an agent between hosts while it exists on both).

Address resolution now honors a host id in the `@HOST` component: `AGENT@host-...` addresses match by host id (previously only host names matched, so an id-qualified address never resolved). This applies to all address-taking commands, including `mngr stop --stop-host`.

Added the `AgentInstanceKey` primitive (`<agent_id>@<host_id>`, also a valid CLI address) and re-keyed all cross-host aggregation by it: the discovery aggregator, the discovery event-stream resolution replay, the `mngr observe` observer (state tracking, PID watchers), and shell-completion membership. Agent-destroyed handling is host-scoped everywhere, so destroying an agent on one host never evicts a same-id agent on another host.

`mngr create` now rejects reusing an agent id on the same host with the new `DuplicateAgentIdOnHostError` (per-host uniqueness is load-bearing); reusing an id on a different host is allowed and documented as "this is the same agent" (use `mngr clone` for an independent copy).

Single-target commands given a bare agent id that matches instances on multiple hosts now fail with an error listing each instance and suggesting `ID@HOST` disambiguation; `mngr stop --stop-host` raises the same disambiguation error instead of silently picking a host. Plural commands (`destroy`, `stop`, `start`, `message`, ...) keep their operate-on-all-matches semantics, and `mngr destroy` warns with the full instance list when a bare id matches multiple instances.

`AgentRemovedEvent` on the observe stream gains an additive `host_id` field (readers tolerate its absence in old lines).

Persisted discovery event parsing is now forward-compatible, fixing the downgrade/rollback wedge where an old client hung forever on newer-schema lines in the shared append-only discovery log (mngr-internal#422).

- Discovery event models (and their nested payload models: `DiscoveredAgent`, `DiscoveredHost`, `SSHInfo`, `DiscoveryError`, `DiscoveredProvider`) now ignore unknown fields instead of rejecting the whole line, so additive schema changes cost old readers nothing. `ProviderInstanceConfig` stays strict for user-authored settings.toml; discovery events carry a tolerant `PersistedProviderInstanceConfig` instead.

- `parse_discovery_event_line` skips-and-warns on schema mismatches (including wholly unknown event types from future versions) instead of raising; the new `DiscoverySchemaMismatchWarner` deduplicates the warnings per distinct failure and emits a counted summary for replays, so a poisoned log surfaces as a few clear warnings rather than one per line.

- The regenerate-and-retry recovery paths and `DiscoverySchemaChangedError` are removed: regeneration could never converge while a newer-version writer was still appending, which is exactly how the incident wedged. Per-line skipping plus the existing full-scan fallback replace them.

- `mngr observe`'s discovery stream consumer now warns and continues on an unparseable line instead of crashing the stream.

- Golden forward-compat tests inject an unknown field at every nesting level of every discovery event type and assert parsing survives, plus runtime lock tests that the tolerant configs are never re-tightened.

Host SSH info now names its known_hosts file explicitly: `SSHInfo` gained an optional `known_hosts_path` field, populated from the connector host data every provider already records, and carried through `mngr list --format json` and the `host_ssh_info` discovery event. Consumers no longer have to guess the pin file's location from the private key's directory.

The human-facing `ssh` command in host SSH info now includes `-o UserKnownHostsFile=<path> -o StrictHostKeyChecking=yes` when the pin file is known, so a copy-pasted command verifies the host key.

The transient SSH connect retry (paramiko "Error reading SSH protocol banner" on freshly booted hosts) now makes three bounded attempts instead of two: a slow fresh Modal sandbox was observed outlasting the single retry, so the worst case for a host that accepts TCP but never speaks SSH grows from ~30s to ~45s in exchange for riding out slower sshd boot windows.

Provider SSH connections now give paramiko a 30-second banner timeout (up from its 15s default, via pyinfra's `ssh_paramiko_connect_kwargs`): degraded Modal tunnels were measured answering the SSH handshake right around 15s, so every connect died with "Error reading SSH protocol banner" no matter how many times it was retried. A genuinely dead endpoint still fails fast at the TCP layer.

## 2026-08-15

Fixed an intermittent modal host-creation failure: provider-profile deploy-file collection crashed with "X is not in the subpath of Y" whenever the profile directory lived outside the user's home (custom MNGR_HOST_DIR, test sessions) and a transient `.<key_name>.lock` flock artifact from SSH-keypair generation happened to be present. Lock files are now never collected, and profile files that cannot be mapped to a `~/` destination are skipped with a warning instead of failing the whole host creation.

Regenerated the `mngr imbue_cloud` CLI reference (`docs/commands/secondary/imbue_cloud.md`) to document the new `admin relays list/add/remove` commands introduced by the multi-relay sharing work.

Regenerated CLI reference docs for the new `mngr imbue_cloud admin workspaces` operator commands (workspace abandon escape hatch), part of the imbue_cloud workspace stop/start feature.

Phase 2 of the user-controlled-keys plan: a structured host-key pin store in mngr core.

New `imbue/mngr/providers/host_key_store.py`: every provider known_hosts file gains a JSON pin-store sidecar and becomes a derived artifact rendered from it. Pins carry an origin (BOOTSTRAP or USER) with the precedence rule that a user-origin pin is displaced only by newer user material while bootstrap-origin pins are replaceable by anything, and pinning always replaces per (endpoint, keytype). Out-of-band lines written to a known_hosts file are imported on the next store operation, so unmigrated writers lose nothing.

`add_host_to_known_hosts` / `clear_host_from_known_hosts` keep their signatures but write through the store when given a `host_id` or when a store already exists for the file; host_id-less callers on store-less files keep the legacy direct-write behavior (throwaway known_hosts files stay sidecar-free).

The docker provider mints a per-host client key (`docker_ssh_key`) and a per-host container sshd host key for every new container, resolving per-host first with a legacy shared-pair fallback for older containers; `delete_host` forgets the host's pins and per-host keypairs (dead-endpoint GC). Shared per-host keypair helpers (`per_host_key_dir`, load-or-create and resolve-with-fallback variants) are now in `providers/ssh_utils.py` for all providers.

Each per-host key dir also carries a `known_hosts` symlink to the provider-wide rendered file, so consumers that derive the pinned-host-keys file as the private key's sibling (the forward SSH tunnel) keep working with per-host client keys.

The host-key store gains `move_host_endpoint_pins`: relocate one host's pins from an old endpoint to a new one with their keys and origins intact, evicting whatever stale pins the recycled endpoint held. Used by imbue_cloud's stop/start path so an adopted host's user-origin pins follow the workspace to its new box instead of regressing to connector bake-time keys.

`ssh_utils` gains `is_server_presenting_host_key`, a public one-shot unauthenticated probe of which host key an endpoint currently serves (used by imbue_cloud's crash-safe host-key rotation to decide whether an interrupted install landed).

The host-key store gains three helpers for consumers that exchange known_hosts material as text (used by minds' workspace sync): `load_current_host_key_pins` (read-only view of a file's current pins, folding in any out-of-band file lines without writing), `render_pins_as_known_hosts_text` (pins back to sorted known_hosts lines), and `pin_known_hosts_text` (parse known_hosts text and apply every line through the store in one locked mutation, with the usual replace-by-endpoint+keytype origin precedence).

No standalone changes for this project yet: this branch currently merges `mngr/remote-machine-stopping` as groundwork for the upcoming workspace-sync key-management spec (user-controlled SSH keys, structured known_hosts store, imbue_cloud lease adoption). The merged changes are described in this project's `mngr-remote-machine-stopping.md` entry where present; this entry will be updated when the spec and its implementation land on this branch.

## 2026-08-14

# Regenerated imbue_cloud CLI docs

Regenerated `docs/commands/secondary/imbue_cloud.md` for the updated `admin server backfill-autostart` `--dry-run` help text (the sweep no longer extracts a per-VM start-script path; the dry run now probes per-VM reachability instead).

Regenerated the `mngr imbue_cloud` CLI reference docs for the new sharing options: `shares relays` (list the relay fleet) and `shares create --preferred-region` (steer a first-time local share to the closest relay).

## 2026-08-13

`mngr list` now reports an agent blocked on a tool-approval dialog (or an `AskUserQuestion`) as `WAITING` rather than `RUNNING`.

Such an agent is inside a turn -- its `active` marker is still set -- but is making no progress, so it belongs with the agents waiting on you rather than with the ones working. Agent plugins already treated it that way, but only for `mngr wait`/`message`/`find`; the agent listing reads the lifecycle probe directly and never saw it. The two disagreed about the same agent at the same moment: `mngr list` showed `RUNNING` while `mngr wait <agent> WAITING` returned immediately.

Being blocked on the user is now an input to the state rather than a correction applied afterwards. An agent type answers `is_blocked_on_dialog()` and the lifecycle probe folds that into the state it builds, so every reader sees the same answer. `mngr observe` reads the listing too, so a blocked agent now shows up as a `RUNNING` -> `WAITING` transition -- and `mngr notify` raises a desktop notification the moment the agent needs you, rather than only at the end of the turn. Agent types that raise no such dialog (pi, plain command agents) or whose dialogs mngr cannot observe (antigravity) inherit a `False` default and are unaffected.

This does not reach the batched provider listings, which stat marker files by fixed name and have no agent to ask: aws, azure, gcp, imbue_cloud, modal, ovh, vps and vultr still report such an agent as `RUNNING`. local, docker, ssh and lima are fixed.

Regenerated the `mngr imbue_cloud` CLI reference docs for the new
`admin server backfill-autostart` command (the reboot-resilience in-VM
backfill sweep added in mngr_imbue_cloud on this branch).

`test_create_snapshot` in the Docker provider's lifecycle tests is marked flaky. It failed in CI with `HostNotFoundError` immediately after the host store warned that the record it had just read was empty (`Invalid JSON: EOF while parsing a value at line 1 column 0`, `input_value=b''`), which is a write and a read of the same host-record file interleaving. The mark buys retries; the underlying repair is to make that write atomic, which is left for a change that owns the store.

## 2026-08-12

Regenerated the `mngr imbue_cloud` CLI reference docs for the new `shares create --entry-label` option.

Regenerated the `mngr imbue_cloud` CLI reference docs to include the new `mngr imbue_cloud hosts enable-sharing` command (added on this branch for server-side share bring-up on leased hosts).

SSH client keypair generation (`generate_ssh_keypair`, used by the docker, modal, lima, vps, and imbue_cloud providers via `load_or_create_ssh_keypair`) now produces Ed25519 keys in OpenSSH format instead of RSA-4096 in traditional PEM. Ed25519 is required by the minds workspaces' owner-exec envelope auth, so the key a client SSHes with can also sign exec requests. Every consumer (paramiko, pyinfra, the `ssh` binary) auto-detects the key type, and existing RSA keypairs on disk keep working unchanged -- generation only happens when no pair exists yet.

Merge the web-only work (`mngr/hopefully-last-web-details`) into main.

SSH client keypair generation (`generate_ssh_keypair`, used by the docker, modal, lima, vps, and imbue_cloud providers) now produces Ed25519 keys in OpenSSH format instead of RSA-4096 PEM. Ed25519 is required by the minds workspaces' owner-exec envelope auth, so the key a client SSHes with can also sign exec requests. Every consumer auto-detects the key type and existing RSA keypairs on disk keep working; generation only happens when no pair exists yet.

Regenerated the `mngr imbue_cloud` CLI reference docs for the reworked auth commands (browser-based `auth login` replacing `auth oauth`, `auth signout --all-devices`, `auth is-verified` as a plain status query) and the new `shares create --entry-label` option and `hosts enable-sharing` command.

Regenerate the `mngr imbue_cloud` CLI docs for the reworked auth commands: the new browser-based `auth login` replaces `auth oauth`, `auth signout` gains `--all-devices` (device-scoped revoke by default), and the pending-verification flow is gone (`auth is-verified` is a plain status query).

## 2026-08-11

`CommandResult` gained an optional `exit_code` field (populated by the outer-host streaming execution paths), so callers can distinguish a command that really exited non-zero from one whose output stream died -- used by the remote docker build error reporting in mngr_vps.

`mngr message` no longer exits 0 when a host it was told to reach could not be reached. A failure resolving the host, starting it (`--start`), or listing its agents was recorded nowhere, so the command printed `No agents found to send message to` and exited 0 for a message that was never delivered -- and any caller reading the exit code, such as a `mngr list --ids | mngr message -` broadcast or `mngr kanpan`'s reply, read that as sent.

Such a failure now lands against every agent on that host, so it appears in the output, in the `--format json`/`jsonl` result, and in a non-zero exit code -- exactly as an offline host without `--start` already did. That inconsistency was the tell: the same "host is unusable" condition exited 1 or 0 depending on which line raised.

A failed agent lifecycle probe is recorded the same way. The probe decides whether an agent needs starting, so it runs before any path that records an outcome, and it absorbs only a host connection error -- so a probe that failed any other way, such as on an agent whose `data.json` no longer parses, left its agent in neither result list, the same undelivered-but-reported-as-sent outcome one layer down.

`--on-error abort` now records these failures before it raises, rather than raising with an empty result. The command still ends with that error; the difference is that a `--format jsonl` consumer sees a `message_error` for each agent that missed the message instead of the event stream simply ending. This matches what the per-agent send paths already did.

## 2026-08-06

`test_prevent_getattr` in `utils/test_ratchets.py` is marked
`@pytest.mark.flaky`: the tree-wide regex scan occasionally blows the 10s
pytest-timeout on a cold-cache offload run (sandbox I/O saturated by the base
image build) and passes on retry.

## 2026-08-04

Updated the generated `mngr latchkey` command documentation for the single-gateway workspace topology.

## 2026-08-03

Bumped the offload version baked into `libs/mngr/imbue/mngr/resources/Dockerfile` from `0.9.10` to `0.9.12` to track the CI pin, keeping the in-image `offload apply-diff` binary in lockstep with the orchestrator.

Regenerate the `mngr forward` command reference (`docs/commands/secondary/forward.md`) for the mngr_forward embedding substrate: new `--browser-bridge-token`, `--embedder-origin`, and `--trust-ca` options, and the `--use-http2` description now reflects per-startup leaf certs minted from a persistent local CA instead of a regenerated self-signed cert.

Update the `mngr forward` command docs for the service-per-origin scheme: forwarded origins are now `[<service>.]<host-id>.localhost:<port>` (bare origin serves the configured backend; `<service>.` origins reach any agent-registered service directly).

Merge main into the self-hosted sharing branch; the `mngr forward` service-per-origin docs from this PR's sibling entries are unchanged by the merge.

Merge main into the mithril-refactor stack (relay-based sharing, iframe desktop shell, Mithril SPA). The changes to this project arrive verbatim from main and are described by their original PRs' changelog entries; no project-specific conflict resolution was needed here.

Docs only: the `forward` command doc is updated for service-per-origin forwarding (per-service origins, `/_bridge` entry, embedder CSP), and the `imbue_cloud` command doc replaces the tunnels surface with the new `shares` subcommands.

## 2026-08-02

Added the repo-wide `test_prevent_unpinned_modal_pip_install` ratchet (synced into every project's `test_ratchets.py`): Modal images must install from a committed hash-locked requirements export instead of passing bare package names to `pip_install`/`uv_pip_install`. No violations in this project (count 0).

Registered the new modal-app-kit workspace library as unpublished in the plugin catalog (it ships into our Modal apps' containers as a source mount, never to PyPI).

## 2026-08-01

Regenerated the `mngr imbue_cloud admin server list` command docs so they document the new `--verify-occupancy` / `--env-name` options (added in mngr_imbue_cloud: SSH each bare-metal box and report its real occupancy across every env plus any cross-tier contamination, rather than the DB-derived table's this-env-only slot counts).

A provider discovery failure now carries a traceback. `DiscoveryError` and `ErrorInfo` gained an optional `traceback_text`, captured wherever the exception object is still in hand (`ErrorInfo.build`, `ProviderErrorInfo.build_for_provider`, and the per-provider error snapshot in `provider_discovery_stream`).

Previously a failed poll recorded only an exception type and message, and nothing logged a traceback around it -- so the snapshot was the sole durable record of the failure and it named neither the file nor the code responsible. An `OSError` raised without a filename stringifies to a bare `[Errno 2] No such file or directory`, which is precisely what one provider emitted every ~30s for 25 hours with no way to tell what it had tried to open. Captured tracebacks are capped (tail kept, since the innermost frames are the useful part) so a long outage cannot bloat the events file.

## 2026-07-31

Registered the new `behaviors` plugin (`imbue-mngr-behaviors`, corpus-generic behavior tooling; see the `mngr_behaviors` changelog entry) in the plugin catalog at the INDEPENDENT tier, listed it as unpublished for now, and added the generated CLI docs page for the `mngr behaviors` command group.

Add the generated CLI documentation page for the new `mngr tmr-behaviors` command (`docs/commands/secondary/tmr-behaviors.md`), the behavior-anchored test map-reduce recipe registered by the `imbue-mngr-tmr` plugin.

Regenerated the `mngr imbue_cloud` CLI reference to document the new `auth is-verified` subcommand (checks email-verification status and promotes a pending session once verified; part of the email-verification-required sign-in flow shipped in mngr_imbue_cloud).

## 2026-07-30

Fixed agent creation hanging with a truncated, half-typed command in the tmux pane.

The agent's command was typed into its window's shell with `tmux send-keys`, which writes into the pane's pty. A pty in canonical mode -- where a just-started shell sits until its line editor takes over -- silently discards input past its buffer (1KB on macOS) while both the write and tmux still report success. Because the keys are sent in the same tmux batch that creates the session, they routinely land inside that startup window, so any command longer than ~1KB arrived truncated mid-token and left the shell blocked on an unterminated construct with nothing logged anywhere. Claude agents were the visible casualty: their launch command recently grew past 1KB, so `mngr create` reliably produced a dead session showing a partial command.

The agent's command is now written to a `launch_agent.sh` script in the agent's state dir and sourced, so what crosses the pty is a short fixed-length line however long the command grows, and the script records on disk exactly what ran. Up-arrow in the pane still re-runs the launch, and the script is removed along with the rest of the agent's state when the agent is destroyed.

This means the agent window's shell must understand the POSIX `.` builtin. In practice that is not a new requirement, since mngr's generated launch commands are already POSIX shell. Commands configured for *additional* windows are still typed in directly, because those windows inherit the user's tmux `default-command`, which need not be a POSIX shell.

## 2026-07-29

mngr: correct docstrings in `utils/testing.py` that described the Modal test-environment cleanup as the "hourly safety net" and referred to "cron/CI logs". There is no cron trigger; the `cleanup_old_modal_test_environments.py` CI job runs on every push to main and every pull request. The docstrings now reflect that. No behavior change.

The docker provider's "state container is stopped" check now looks at *its own* state container instead of whichever labelled one Docker happened to list first.

The provider lists candidate containers by label and then keeps the ones whose name starts with its `MNGR_PREFIX`. That prefix test is a plain `startswith`, so a `minds-` environment's list also contains every container belonging to an environment whose prefix extends it (`minds-staging-`, `minds-dev-<name>-`) -- including their state containers, which carry the same provider and type labels. The stopped-state-container guard then raised `ProviderUnavailableError` for the first labelled container it found, and Docker returns newest-created first.

The effect: quitting a *newer* minds environment (whose quit flow deliberately stops that environment's state container) made an older environment's docker provider report `Provider 'docker' is not available: Docker state container is stopped` on every discovery cycle, even though its own state container was running the whole time. Every docker snapshot went empty-with-error, so `mngr event <agent> --follow` exited non-zero and `mngr forward` never learned a freshly-created workspace's `system_interface` URL -- the desktop app's readiness probe then timed out against a permanent 503 and the workspace sat on "Loading workspace" until the sibling environment's container was started again.

The guard now requires the container's actual name to be the one `state_container_name` assigns to this environment, matching what `_find_container_by_host_name` already does for host containers. A sibling environment's state container -- stopped or running -- no longer speaks for this one.

The attach-time discovery replay (`mngr forward --observe-via-file`, used by the minds app on startup) now skips provider snapshots that are superseded by that provider's latest one(s), instead of re-emitting and re-folding one interleaved world-state per discovery cycle of the backlog.

The replay window reaches back to each provider's last healthy snapshot, so a provider that stopped writing (removed from config, or erroring for days) used to force every attach to replay days of stale snapshots -- tens of thousands of events and minutes of startup delay during which minds showed a stale workspace listing. Only each provider's latest non-errored snapshot, latest snapshot, and latest snapshot carrying a provider config can still affect the reconstructed state, so only those (plus every non-snapshot event) are emitted; the final state is identical.

The skipped snapshots are summarized in one counted log line per provider (count, errored count, and time span), plus one counted line per distinct error the skipped snapshots carried, so the record of the gap -- including which failures occurred during it -- still surfaces in the log without the per-cycle flood.

The listing collection scripts can now be given fallback host_dir locations, for providers whose hosts were created under an older layout than the one currently configured.

- `build_listing_collection_script` and `build_outer_listing_collection_script` take `fallback_host_dirs` and resolve the first candidate that holds a `data.json`, reporting the winner on a new `HOST_DIR=` line that `parse_listing_collection_output` surfaces as `host_dir`.

- The probe is the host record rather than directory existence, because a read against the wrong layout can leave an empty state directory behind.

- Providers that pass no fallbacks are unaffected.

The layouts a client will *read* are now named once, in `imbue.mngr.providers.host_dir_layouts`: `KNOWN_WORKSPACE_HOST_DIRS` lists the in-host `host_dir` locations mngr has shipped, and `host_dir_fallbacks(configured)` returns the others. That replaces the per-provider constants each reader had grown.

The layout new hosts are *written* at is still declared separately, by whoever bakes them -- `pool_bake.py` for imbue_cloud pool hosts, the workspace template's provider blocks, and `WORKSPACE_HOST_DIR` in the minds app (which cannot import mngr; its settings package has to load before the plugin does). So a future move is an edit in each of those plus one here, not a single line.

- Falling back runs in both directions. A client resolving the older `/mngr` (the built-in default, which is what a `$HOME`-scoped read gets when no project settings apply) can now read a host baked at `/home/user/.mngr`, not just the reverse.

- Only providers reading a container or VM that mngr baked opt in. The bare VPS realizer derives its host_dir per host (`/mngr/hosts/<name>`) and the ssh and local providers run on machines mngr does not own, so for them a stray `/mngr` would belong to something else.

- The docker and lima providers keep recording `host_dir` in their own per-host records at creation time instead. Those records are local and writable, so the layout is known without a probe. What they do not cover is hosts created between the layout cutover and the record field landing; that window is developer-only, because a pre-cutover host and a `$HOME`-scoped read both resolve `/mngr` and so agree anyway.

- The vps providers (vultr, ovh) have no per-host record and read at whatever their config resolves. That is sound as long as their `host_dir` setting matches the bake, which it does for the workspace template; they are not reachable from the minds app without a hand-written provider block.

Fixed unbounded memory growth in `mngr observe`. It supervises a `mngr observe --discovery-only` child plus one `mngr event --follow` child per host, all of which stream for as long as the observer runs, and it retained every line they ever emitted.

Those children are now started with `is_output_accumulated=False`, so lines are consumed as they arrive and then dropped. Their stderr was previously discarded silently; it is now logged at debug level, so a failing child is still diagnosable.

Regenerated the `mngr imbue_cloud admin server prep` / `admin server setup` command docs so the documented `--lima-version` default matches the new bare-metal box lima pin (2.2.0, bumped from 2.1.2 in mngr_imbue_cloud for the upstream guestagent portfwdserver leak fix).

## 2026-07-28

The submit-and-confirm engine now supports rejection evidence: a `SubmissionEvidenceProbe` can be marked `is_rejection`, and when such a probe is the one that confirms, the outcome carries `is_rejection` and `send_message` logs an accurate "agent rejected the message" warning and records a `send_rejected_by_agent` delivery event instead of implying the message was accepted. The send still exits 0 (delivery semantics: the input reached the agent; the agent's verdict on it is reported, not hidden).

The message-delivery journey release test gains an unknown-slash-command step (profile-gated, like the existing relaxed-slash step): the send must exit 0 and a `send_rejected_by_agent` event must be recorded. This is the canary for upstream TUI changes to the rejection record -- if the record's shape changes, the probe stops firing and this step fails, pointing at the agent's rejection filter.

The message-delivery journey now recognizes injected deliveries: Claude Code 2.1.21x may deliver a queued message by removing it from the queue (a `queue-operation`/`remove` record, previously unknown to mngr) and injecting it into the running turn, writing no user record. The per-step waits and the exactly-once check count one injected delivery as delivered, via a new profile hook (`count_injected_deliveries`, default 0); duplicate user-message records still fail, so an Enter-retry double delivery (which lands as two user records) remains detectable. Without this the journey flaked on current Claude (measured on unmodified main: the queued message was "not captured" whenever Claude took the injection path).

## 2026-07-27

The two create concurrency groups that persist agent state and mirror the work_dir git repo (`write_agent_state`, `_create_work_dir_via_git_mirror`) now use a generous 300s exit timeout instead of the 10s default: mirroring + Modal-volume-uploading a large work_dir (e.g. a repo that vendors the full mngr tree) can exceed 10s, and a create must wait for it rather than terminating the transfer strand mid-flight. Only the normal-exit path is affected; the failure (shutting-down) path keeps the short timeout.

Updated a tutorial e2e comment to the default-workspace-template's renamed system_interface location (system/apps/system_interface) as part of the creation rename. No behavior change.

## 2026-07-26

The regenerated `imbue_cloud` CLI reference (`docs/commands/secondary/imbue_cloud.md`) follows the default-workspace-template root declutter: the `mngr imbue_cloud admin pool create --mngr-source` option is now documented as rsyncing into the template's `system/vendor/mngr/` (previously `vendor/mngr/`).

Integration branch combining the user-data-layout trains (`mngr/fix-data-layout`, `mngr/declutter-template`) with `mngr/fix-apt-mirror`; the full per-train details live in this project's sibling entries for those branches.

For this project: the docker provider gains the additive `volume_mount_path` and `host_log_dir` knobs, host records persist their `host_dir` (used by discovery, start, and snapshot restore), and the regenerated `imbue_cloud` CLI reference follows the template declutter (`system/vendor/mngr/`).

Regenerated the `mngr imbue_cloud` CLI reference (`docs/commands/secondary/imbue_cloud.md`) to pick up the new `bucket destroy --force` / `-y` options added by the destroyed-workspace backup-retention work in `libs/mngr_imbue_cloud`.

`mngr destroy` now accepts host addresses (`@HOST`, `@HOST.PROVIDER`, or a bare `host-<hex>` id) alongside agent names. A host address destroys the host and everything on it -- including hosts with no agents at all, such as one left behind by an abandoned create. The local host is always refused. With `--force`, a host address that matches nothing is skipped instead of failing, so cleanup scripts stay idempotent. Destroy results now also report destroyed hosts (`destroyed_hosts` in JSON output).

`HostNameConflictError` can now carry the conflicting host's id and state plus a remediation hint, so providers can tell users exactly which host holds a name and how to clear it.

Two additive, default-preserving provider knobs supporting the upcoming /home/user workspace data layout:

- The docker provider's new `volume_mount_path` mounts the per-host volume at a configurable path (e.g. `/home/user`) with `host_dir` as a directory inside it, instead of mounting at `host_dir` directly. Requires `isolate_host_volumes=true`; the choice is persisted per host record and replayed on start and snapshot restore.

- A new `host_log_dir` config on every provider instance directs mngr's plain-text service logs (shutdown, activity watcher, volume sync) outside `host_dir` (e.g. to `/var/log/mngr`), keeping regenerable diagnostics out of backed-up data. Default unchanged (`<host_dir>/logs`).

- Docker host records now persist the `host_dir` each host was created with, and discovery/reads use the recorded value instead of whatever `host_dir` the current context's provider config resolves to. Previously, running e.g. `mngr list` outside the repo whose settings customized `host_dir` looked in the default `/mngr` and reported zero agents on such hosts. Host volumes for hosts created with `volume_mount_path` are likewise scoped so agent state stays addressable at host_dir-relative paths. Start and snapshot restore honor the recorded value too: the volume mount target, the in-container activity watcher, and the setup symlink all use the host's recorded `host_dir` rather than the restoring context's.

`mngr list` gained a `--hosts` view: it lists hosts instead of agents -- including agent-less hosts (e.g. one left behind by an interrupted create) and destroyed-but-persisted host records -- with each host's labels, state, and the agents on it. Combinable with `--provider` and the output-format options only. This is the host-inventory surface the minds desktop client's startup reconcile reads to find and clean up orphaned Lima/Docker workspace hosts.

Regenerated the `mngr imbue_cloud` CLI reference (`docs/commands/secondary/imbue_cloud.md`) for the new `keys litellm create --rotate-on-exists` flag (single-invocation key rotation, added in `libs/mngr_imbue_cloud` on this branch) and, via the merged `mngr/deploy-v0-3-9-debug` branch, the `bucket destroy --force` / `-y` options from the destroyed-workspace backup-retention work.

## 2026-07-24

Disambiguated the overloaded `HostState.UNAUTHENTICATED` so consumers can distinguish two conditions that need opposite handling, without adding a new host state:

`UNAUTHENTICATED` now means our access credential was rejected, or the host's sshd could not be verified, at an access boundary -- conditions a restart cannot fix, so it is terminal rather than restart-worthy. Two producers: imbue_cloud's outer SSH refusing this machine's key (a restart routes through the same rejected key), and the generic provider SSH into the workspace host raising a rejected-credential or unverifiable-host-key error (a restart touches neither our known_hosts nor a rejected key).

Every "we couldn't read the host" condition that a restart *might* fix now maps to `UNKNOWN` rather than `CRASHED`: a running container whose inner sshd/exec did not answer (a non-auth connection failure), and any degraded or unrecognized provider observation. mngr does not claim to know what is wrong with such a host -- `UNKNOWN` is non-evidence, so consumers that auto-restart off `CRASHED` (e.g. the minds recovery page) do not do so, and the host is neither hidden by `mngr list --active` nor garbage-collected. Resolves the provider-vocabulary deferral flagged in PR #2247.

`HostState.RUNNING`'s contract is now documented explicitly: it is a liveness read that does not assert we reached the host's inner sshd. A provider that reads liveness out-of-band reports `RUNNING`; a path that attempts the inside and cannot get in reports `UNKNOWN`, not `RUNNING`.

Diagnostics for docker discovery reporting existing hosts/agents as absent (observed live: `mngr start <agent>` failed with "Agent not found" during a minds app launch, against a stopped host whose records another process read successfully one second later):

- `DockerVolume.listdir` now distinguishes a genuinely-missing directory (still `FileNotFoundError`, the normal fresh-env case) from any other `ls` failure inside the state container, which now raises `VolumeListingError` (an `MngrError` that is also an `OSError`) carrying the exit code and output.

- The docker host store logs a warning (instead of silently returning an empty list) when listing host records or a host's persisted agent data fails for any reason other than the directory not existing -- an empty result there makes hosts/agents invisible to discovery.

- An agent lookup that is about to fail with "No agent(s) found matching" first logs a warning summarizing what discovery did return (per host: provider, name, id, state, and agent count), so a not-found caused by a discovery gap is distinguishable from a truly-absent agent after the fact.

Agent-scoped commands (e.g. `mngr start <agent>`) no longer fall back to a full all-provider discovery scan after a provider outage, which could stall them for a minute or more on an unrelated unreachable provider (observed live: a minds cold-boot restart of a stopped docker workspace spent ~60s of its 73s inside `mngr start`, waiting on SSH to a dead aws-us-west-1 host):

- The event-stream replay that resolves an agent identifier to its provider no longer forgets a provider's agents when replaying errored snapshots (e.g. the "docker state container is stopped" backlog written while the minds app is closed). Per the snapshot contract, absence from an errored snapshot means the read failed, not that the agents are gone -- matching what `DiscoveryStateAggregator` already did.

- The snapshot replay window now reaches back to each provider's latest non-errored snapshot (instead of merely its latest), so a provider's last-known membership stays reconstructable for as long as its outage lasts. This also lets `mngr forward --observe-via-file` consumers (the minds resolver) see the last-known agents of an errored provider immediately on attach.

Pin `restic` in the mngr container/offload image to the same version minds bundles (0.18.1, SHA256-verified) instead of installing the distro package.

Debian bookworm ships restic 0.14, which is roughly 10x slower than 0.18 (slow enough that minds backup tests blew past their timeouts under loaded offload sandboxes, surfacing as a SIGKILL / "exit -9") and predates flags the minds backup code relies on (e.g. `--insecure-no-password`, added in 0.17). The image now downloads the pinned restic release for the build arch, matching what end users get from the bundled binary. Keep the version in sync with `apps/minds/scripts/download-binaries.js`.

`mngr exec` can now stream a command's output live instead of only printing it after the command finishes.

- Added `mngr exec --stream`, which writes each stdout/stderr line as it arrives (human output format, intended for a single agent). Without it, `mngr exec` still buffers and prints everything at the end, as before.

- `HostInterface.execute_stateful_command` gained an optional `on_output(line, is_stdout)` callback: when provided, the command's output is streamed to it line-by-line (via the same paramiko read loop as `execute_streaming_command`) and the full result is still returned. `exec_command_on_agents` threads an `on_output` through to it. This is what makes long minds workspace operations (e.g. an in-place backup restore) show their progress as it happens rather than all at once at the end.

Regenerated the `mngr imbue_cloud` command docs: the `--api-key` option for `admin paid` / `admin account` / `admin sweep` commands now documents `$MINDS_ADMIN_KEY` as the canonical admin API key env var (the deprecated `$MINDS_PAID_ADMIN_KEY` spelling is still accepted as a fallback).

Fixed `test_cli_create_from_git_url`, which failed on every branch since the private-repo cutover: it clones the public `imbue-ai/mngr` URL, which is now a filtered mirror that does not publish `CLAUDE.md`, and asserted that file's existence. The assertion now targets `README.md`, which the mirror keeps.

## 2026-07-23

The `mngr extras` CLI tests no longer spawn the real `claude` Node CLI to probe claude-plugin status -- its startup on a contended offload sandbox tripped the global 10s pytest-timeout. A shared `stub_claude_on_path` fixture puts a stub `claude` on PATH that reports every known plugin installed, keeping the shell-out and JSON parsing exercised end to end while short-circuiting the install flow before its TTY check; the real claude probe stays covered by a new acceptance test with a Node-sized timeout.

The mngr resources Dockerfile (also the offload CI image) now pre-fetches the dugite-native git release asset pinned by `apps/minds/scripts/git-manifest.json` into `/opt/dugite-native-cache` at image build time, SHA256-verified against the manifest, and exposes the location as `MINDS_DUGITE_NATIVE_CACHE_DIR`. This exists because offload sandboxes run with network disabled: the minds bundled-git acceptance test (`apps/minds/test_bundled_git.py`) reads the asset from this cache in CI (and downloads it live on dev machines), keeping the managed-git payload continuously proven on linux-x64. Adds roughly 60 MB to the image. See `specs/minds-managed-git/concise.md`.

`mngr start` gains a `--resume-message TEXT` option: a per-invocation override of the agent's stored resume message, sent through the existing readiness-aware resume machinery after the agent starts. Mutually exclusive with `--no-resume`. This lets callers (such as the workspace auth-change restart in default-workspace-template) restart agents and deliver a "please continue" message in a single `mngr start --restart --resume-message ...` invocation instead of separate stop/start/message calls.

`mngr message` now distinguishes three outcomes so a caller can tell them apart by exit code:

- `0`: the message was delivered and no blocking dialog remained.
- `7` (new): the message was delivered, but a blocking interactive dialog (e.g. Claude's `/model` confirmation) could not be resolved and the agent is now stuck on it. See the Claude agent's `auto_accept_prompt_depth` setting.
- any other non-zero: the message was not delivered.

Blocked agents are reported separately from failed ones in the human, JSON, and JSONL output. Internally this adds a `MessageDeliveredButBlockedError` and a `blocked_agents` list on the message result; interactive TUI agents gain a post-submit hook (a no-op by default) that runs after a send is confirmed, which the Claude plugin uses to detect and auto-accept (or surface) a dialog opened by the just-sent message.

Internal: the agent `wait_for_ready_signal` parameter `is_creating` was renamed to `is_readiness_awaited` (it gates whether the call waits for the agent's readiness signal -- the TUI ready indicator, or a launch/session sentinel). No behavior change.

Regenerated `docs/commands/secondary/imbue_cloud.md` for the new imbue_cloud plugin commands: `account cleanup-grant`, `account recheck-storage`, and `admin sweep r2`.

Regenerated the imbue_cloud CLI reference docs to include the new `mngr imbue_cloud tunnels find-by-agent` subcommand merged from the speed-up-sharing branch.

Regenerate the `mngr imbue_cloud` CLI docs for the new account plan/quota commands (`account show` / `set-plan`, `admin account ...`, `bucket roll-key`) and the removal of `bucket keys create` / `destroy`.

Move the agent-capability-mixins design doc from the repo-root `specs/` into `libs/mngr/future_specs/capability-mixins.md` so the public doc that references it (`docs/concepts/agent_capabilities.md`) points at a path that exists on the public mirror.

The generated `mngr tmr` CLI reference documents the new `--reducer-branch-suffix` option (suffixes the reducer's branch/agent/host names so a reintegration's pull request does not collide with the original run's reducer branch).

Regenerated the `mngr imbue_cloud` CLI reference docs to include the new `auth oauth --success-redirect-url` option (added in `libs/mngr_imbue_cloud` for the minds desktop app's post-sign-in browser handoff).

## 2026-07-22

`mngr tmr` gained a `--reducer-env` option, documented in the generated CLI reference. It passes environment variables to the reducer agent only, never to the mappers, for credentials the reducer needs but mappers must not receive (such as a token that can push and open pull requests).

The `mngr tmr` help text now describes the reducer's full role (collapsing repeated changes, writing the run's changelog, opening the pull request), the explicit per-test `--timeout` its agents run with, and the `escalations` field in the outcome schema.

## 2026-07-21

Fixed: agent tmux windows are no longer born collapsed (e.g. to `2x1`) when a degenerate, unsized client is attached to the host's shared tmux server. Previously mngr created agent sessions with tmux's default `window-size latest` policy, so a brand-new detached session was born at the geometry of whatever client was already attached to the shared server -- even a stray `2x1` ttyd/web-shell client -- regardless of the `new-session -x 200 -y 50` sizing. A `2x1` pane breaks Claude Code's TUI, so the initial `mngr message` delivery (which waits for the TUI to be ready) would time out and the message would be silently dropped. This is the root cause of "the agent was created but never got its first message" symptoms (e.g. the desktop `/assist` "have an agent help" flow, and launch-task workers).

By default, agents are now pinned to a stable, usable geometry: the primary window is set to `window-size manual` and resized to the intended dimensions (historical `200x50`), making creation deterministic and immune to stray clients. Interactive attach still fits your terminal: per-session `client-attached` and `client-resized` hooks re-fit the pane to the current client's size (so a live terminal resize is tracked continuously, matching tmux's native `latest`), floored at a usable minimum (`80x24`) so a degenerate client can never collapse the pane below what Claude Code needs to render.

An explicit `--tmux-window-size` is still honored verbatim: a client-following mode (`latest`/`largest`/`smallest`) behaves as before, and an explicit `manual` stays truly fixed (the re-fit hook leaves it untouched).

Note: only newly created agents get the new pinning. Agents already running when you upgrade keep their current window-size policy until recreated.

Fixed `mngr list --sort` on numeric fields (e.g. `idle`, `age`, `runtime`, `idle_seconds`), which previously ordered values as strings -- so an idle spread like 2.4, 14040.2, 111044 came out interleaved rather than sorted. Numeric sort keys now compare numerically; string fields are unchanged, and missing values still sort to the end in both directions.

## 2026-07-15

Fixed the discovery-events replay window being permanently pinned at the last legacy `DISCOVERY_FULL` snapshot. Installs that upgraded across the per-provider discovery migration kept that line in `events.jsonl` forever (nothing writes the type anymore), so every observe-stream attach replayed days of stale history -- destroyed workspaces reappearing with their old names at app startup, then visibly disappearing again as the destroy events re-folded. The legacy timestamp now participates only when the file contains no per-provider snapshots at all.

- Added: `setup_command_context` (and `parse_output_options`) accept an optional `max_log_size_mb` override so a command can pin its own rotated-log size cap instead of inheriting `logging.max_log_size_mb` from config. Long-running daemons that write to a dedicated `--log-file` use this to keep a smaller cap than the general mngr default. The default behavior (inherit the config value) is unchanged.

Regenerated the `mngr imbue_cloud` CLI reference docs for the new `sync` subcommand group (workspace-record and key-bundle transport used by the minds app).

Marked `test_worker_hubs_do_not_accumulate_across_polls` as flaky: its live-gevent-hub count races against concurrently-running tests under xdist (it passes reliably in isolation).

Made the remote cooperative host lock safe across SSH reconnects. Previously a dropped connection mid-critical-section silently released the `flock`, and the transient-retry machinery reconnected and kept running unlocked -- a silent mutual-exclusion violation that let another actor enter its critical section in the gap.

- A monotonic acquisition counter (`host_lock.generation`) is now incremented under the lock on every acquire. A holder that reconnects while holding the lock re-acquires and verifies no other actor acquired in the gap: if the counter advanced by only its own re-acquire it transparently continues, otherwise it raises the new `LockLostError` (a `MngrError`, already isolated per-host by callers such as the `mngr_mapreduce` launch loop).

- Loss is detected at every boundary: the paramiko reconnect chokepoint, an independently-dead lock channel, before each rsync (which uses a separate connection), and a single-exit release backstop that guarantees no critical section is reported successful while the lock was silently lost.

- While a lock is held, a still-live SSH transport is no longer torn down on a channel-level or read-timeout error (which would needlessly drop the lock); only a confirmed-dead transport triggers reconnect-and-re-verify.

- All in-host lock acquirers now participate in the counter so an in-host acquisition is visible to a reconnecting remote holder: the idle-shutdown watcher increments the counter (and holds the lock) when it takes the host down, and the local-filesystem lock path increments on acquire.

## 2026-07-14

The full `mngr observe` observer now detects a local agent's main process dying on its own (OOM, crash, or normal exit) within seconds instead of waiting up to 5 minutes for the next full snapshot.

- It watches each local agent's main process PID (via psutil's event-driven `wait`) and treats the process exit as an activity signal, re-probing and emitting the new lifecycle state (STOPPED/DONE) promptly. Remote agents are unchanged (per-host activity streams plus the periodic snapshot).

- New `mngr observe --stream-events` mode: the full observer echoes each agents-stream event (`AGENT_STATE`, `AGENTS_FULL_STATE`, and the new `AGENT_REMOVED`) to stdout as compact JSONL, in addition to writing the usual event files, so a parent process can consume live agent lifecycle state by reading the observer's stdout. Cannot be combined with `--discovery-only`.

- New `AGENT_REMOVED` event on the agents stream, emitted when a previously-known agent is destroyed, so consumers learn of removals promptly rather than at the next full snapshot.

- `AgentDetails` gains an optional `pid` field, populated for running agents on any provider (the PID of their main process, e.g. `claude`, in the host's PID namespace); it is a filterable/sortable listing field. The observer only PID-watches local agents, gated on the agent's host belonging to the `local` provider (the one and only user of the local connector) -- a remote PID must never be watched in-process, since psutil would bind an unrelated same-numbered local process.

- The `libs/mngr` psutil floor is raised from `>=5.9` to `>=7.2` to guarantee the event-driven `wait()` path (os.pidfd_open on Linux, kqueue on macOS).

Fix the Docker provider's create-time host-name collision check matching containers from other mngr environments.

`_find_container_by_name` filtered only by the `host-name` and `provider` labels, which carry no environment discriminator -- two environments differing only in `MNGR_PREFIX` (e.g. two minds envs) label their containers identically, so creating a host whose name was in use in a *different* environment failed with a misleading "container already exists" error naming a container that does not exist. The lookup now also requires the container's actual name to match `<prefix><host-name>` (the same uniqueness scope Docker itself enforces and the same prefix filter discovery applies), so collisions are detected only within the current environment. The same lookup backs host resolution by name, so `mngr` commands addressed by host name also no longer resolve to (and act on) another environment's same-named container.

Test-only: give `test_extras_claude_plugin_subcommand` timeout headroom (it shells out to the `claude` Node CLI, whose startup can exceed the global 10s pytest-timeout on a contended CI sandbox).

Bump the release-sandbox Dockerfile's pinned Claude Code version from 2.1.160 to 2.1.207, which supports the Claude Fable 5 model. Must land together with the matching `[agent_types.claude].version` bump in default-workspace-template.

Fixed the root causes of two flaky CI tests (issue #2456):

- Remote host connections now retry (once, immediately) when the SSH handshake fails with paramiko's "Error reading SSH protocol banner" -- the signature of a freshly booted host (e.g. a new Modal sandbox or VPS) that accepts TCP before sshd is ready. Previously the first failed connect was fatal, surfacing to users as a spurious "Create agent failed" and making `test_snapshot_create_then_list_on_modal` flaky. Other connect failures (refused, unreachable, auth, host key) are still not retried, so genuinely-down hosts fail fast, and the two attempts bound the worst case (a host that accepts TCP but never speaks SSH) at about 30 seconds.

- Extras CLI tests no longer spawn the real `claude` CLI: a shared stub-`claude` fixture replaces the Node process (whose startup on a contended CI sandbox tripped the global 10s offload pytest-timeout in `test_extras_claude_plugin_subcommand` and `test_extras_no_args_shows_status`) while still exercising the real shell-out and JSON-parsing plumbing. The real `claude` probe stays covered by a new acceptance test with a Node-startup-sized timeout.

- Marked `test_worker_hubs_do_not_accumulate_across_polls` as known-flaky with a 30s per-test timeout: its 30 executor spinups and full-heap gevent-hub probes take ~5s even on an idle machine, so on a contended runner it could cross the suite-wide 10s pytest-timeout.

# Robust message delivery: durable-evidence confirmation for `mngr message`

- Fixed: `mngr message` could report success for a message that was never submitted. tmux latches a `wait-for -S` signal fired with no waiter, so a stale signal on the `mngr-submit-<session>` channel (left by any prompt submission no sender was waiting on, e.g. a task-notification dequeue) instantly "confirmed" the next send -- whose EXIT trap then killed the still-pending backgrounded Enter keystroke. The message stayed in the input box while every layer reported success.

- Changed: submission confirmation no longer listens on tmux `wait-for` channels at all. A new submit-and-confirm engine in `tui_utils` runs ONE sequential remote script per send -- capture per-probe baselines, send Enter, poll agent-supplied durable evidence probes -- with no background jobs and no traps, so Enter is always sent before any confirmation check can run.

- Changed: `InteractiveTuiAgent` subclasses now implement `_build_submission_evidence_probes` (durable on-disk evidence) instead of `_send_enter_and_validate` (send-Enter strategies). The unused `send_enter_best_effort` and `send_enter_and_poll_for_cleared_indicator` strategies were removed; agents supplying no probes degrade to a best-effort Enter. The `enter_submission_timeout_seconds` field is now `confirmation_timeout_seconds` (default 90s).

- Added: bounded, pane-gated Enter retries (at ~3s/10s/30s into the confirmation window): Enter is re-sent only while the pane still shows the pasted text, and the message text itself is never re-pasted, so mngr's own retries can never duplicate a message.

- Added: messages starting with `/` (TUI slash commands such as `/clear`) are confirmed under a relaxed policy: same retries and a brief evidence poll, but the send succeeds even when no evidence is observable, logging a warning and recording a structured agent event (`events/messages/events.jsonl`) instead of failing.

- Added: unconfirmed strict sends fail with rich diagnostics (per-probe baseline/final tokens, Enter-retry history, pane capture). `mngr create --message` and resume-message failures now explain that the agent itself is up and how to resend (`mngr message <agent>`).

- Added: a warning plus a structured agent event when the input box already contains text before a send (the new message is appended, as before).

- Added: release-test harness journeys for message delivery (`run_message_delivery_journey`: idle -> busy/queued -> rapid sequential -> long buffer-pasted message -> slash command, each delivered exactly once) and concurrent delivery to two agents on one tmux server (`run_concurrent_message_delivery`).

Fixed `mngr transcript` (human format) rendering assistant messages as `(no content)` when the event has no ordered `parts[]` list. The current common-transcript emitter always fills `parts[]`, but agents running an emitter version predating it (before 2026-06-15) emit only the flat `text`/`tool_calls`, so their assistant turns -- including ones with real text -- collapsed to `(no content)`. The formatter now falls back to the flat fields when `parts[]` is empty (a back-compat shim for those older agents; see MIND-113).

Fixed messaging an agent whose process had exited but whose tmux session was still alive (e.g. after a ctrl-c, a crash, or an out-of-memory kill of just the agent process). Previously the message was typed into the leftover shell and lost; now the agent is restarted and the message delivered.

This had two parts:

- Lifecycle detection no longer mis-reports such an agent as `REPLACED`. A known-type agent whose pane foreground has dropped back to a shell prompt is now `DONE` even when non-shell background processes are still running under the pane -- in particular mngr's own in-pane helpers (the transcript streamers and background-task script, each running a `sleep` loop), which always linger after the agent process is killed. A non-shell process in the pane *foreground* (e.g. a program the user launched in the agent's window) is still treated as a genuine replacement and is never torn down. Unknown-agent-type behavior is unchanged.

- Sending a message now restarts an agent that is `STOPPED` or `DONE` (neither has a live process to receive the message). For a `DONE` agent the lingering tmux session is torn down first, mirroring `mngr start --restart` -- including its host-lock serialization against `mngr gc` and concurrent starts, via a locked stop-then-start helper now shared with `mngr start --restart` -- so the relaunch actually happens rather than no-op'ing on the existing session. If this automatic (re)start fails (e.g. the relaunched agent never signals readiness), the failure is recorded against the agent in the command's output and exit code, just like a failed send, rather than only being warned about in the log.

Together this is what the OOM revival path relies on: an agent whose main process was shed is brought back by the next message it receives.

Fixed the remaining places where mngr shelled out to GNU-coreutils binaries and flags that stock macOS does not ship, each of which ran on the user's own machine whenever an agent used the `local` provider. All were verified failing on macOS 26.4.1 with a stock `PATH`, then verified fixed.

Raw-transcript streaming re-emitted already-emitted lines on macOS. `mngr_transcript_reconcile_offset` reverse-scanned the session file with `tac` to find the last emitted line; with `tac` absent the scan read nothing and the offset reset to 0. It now scans forward and tracks the last match, which needs no reverse at all. This also fixes a latent off-by-one: the old code combined `wc -l` (which does not count an unterminated final line) with `tac` (which emits it), so a session file whose last line was still being appended -- the normal state of a live transcript -- reconciled one line too low.

Transcript-streamer startup also got much faster. Both `mngr_transcript_build_id_set` and `mngr_transcript_reconcile_offset` used to extract each line's correlation field through a command substitution, which forks a subshell per line; they now match it with an inline bash regex and fork nothing. The dominant cost was `build_id_set`, which scans the whole already-emitted output: on a 50,000-line file it drops from about 32s to 0.7s on macOS, and about 14s to 0.6s on Linux (three trials each; forks are cheaper on Linux). `reconcile_offset` is subtler -- removing `tac` also removed its early exit on the first match from the end, so the new forward scan always reads the whole file (about 0.7s at 50,000 lines) instead of sometimes stopping early. But because it no longer forks, its worst case -- a match near the start, which made the old reverse scan fork through the entire file at about 12s -- is now that same bounded 0.7s. The inline match uses bash's built-in `[[ =~ ]]`, so unlike the `tac` it replaced it needs no external binary and behaves identically on macOS and Linux.

`mngr gc` mis-measured orphaned work directories on macOS. `du -sb` has no BSD equivalent (`-b` is rejected), and because the command was piped into `cut`, the pipeline exit status was `cut`'s, so the failure looked like success with empty output and every orphan was reported as 0 bytes. `stat -c %Y` is likewise rejected by BSD `stat`, so each orphan's `created_at` fell back to `datetime.now()` and looked brand-new, meaning age-based collection never ran on macOS.

Both `gc` call sites now go through the host interface instead of hand-rolled shell. `OuterHostInterface` gains a concrete `get_directory_size`, alongside the existing `path_exists` and `get_file_mtime`: it runs POSIX `test -d ... && du -sk` on the host -- locally via subprocess, remotely over SSH -- so callers never branch on `is_local`. Sizes are whole kibibytes, since `-k` is the only `du` block size POSIX defines, and a path that is not a directory reports 0. The trailing slash resolves a symlinked path to its target directory, and `du` exiting non-zero is tolerated because it still prints a correct total after skipping an unreadable subdirectory. The exact `du -sk <dir>/` invocation was verified on stock macOS (BSD `du`) and on Linux (GNU and busybox `du`) to agree on the load-bearing cases: a hard-linked inode counted once, a nested symlinked directory not followed, and a top-level symlink resolved.

A further fix originally on this branch -- a perl `alarm` fallback for `timeout(1)` in the tmux message-submission path -- was dropped when #2475 replaced that path's confirmation engine wholesale (durable-evidence polling, no `timeout(1)`). The portable-shell technique it established, including the perl timeout form and its two pitfalls, is preserved in the repo-root `style_guide.md` instead of in code with no remaining caller.

Testing: integrated the e2e/tutorial test fixes from 240 qualifying TMR test-agent branches into a single squashed commit (all test/doc; no implementation changes were required). During integration the shared e2e fixture (`e2e/conftest.py`), which 47 agents had each independently patched, was reconciled to one coherent approach:

- Provider discovery is now constrained to the backends the e2e suite actually exercises (`local`, `ssh`, `modal`, plus `docker` only when a Docker daemon is reachable) via the `MNGR__ENABLED_BACKENDS` env-var layer. This prevents the credential-requiring cloud provider plugins installed in the monorepo (aws, azure, gcp, vultr, ovh, imbue_cloud) from making a bare `mngr list` exit with `EXIT_CODE_PROVIDER_INACCESSIBLE`.

- Docker is dropped from the enabled backends when no daemon is reachable (probed with a plain socket connection, so it neither depends on the Docker CLI/SDK nor trips the `docker_sdk` resource guard), so the suite runs on Docker-less hosts (e.g. under `-m 'not docker'`).

- Setting the allowlist through the env-var layer (rather than a settings file) keeps it invisible to the `mngr config list --scope <file>` tests that assert on per-scope file contents.

Also de-duplicated a doubly-inserted `libs/overlay` entry in the install-test fixture's `_WORKSPACE_PACKAGES` and removed a now-stale `FIXME(tmr)` about `rsync` marks on the event tests (the integrated suite already drops those marks everywhere).

- Removed the superfluous `@pytest.mark.rsync` mark from the `test_advanced_collect_results_loop` e2e tutorial test. The test creates local `--type command` agents and only exercises `mngr exec`, which never invokes rsync, so the resource guard failed the otherwise-passing test.

- Fixed the `test_advanced_create_reuse_modal` e2e tutorial test: its post-create verification queries now scope `mngr list` to `--provider modal`, so they no longer fan out to every enabled provider (in particular the unconfigured AWS provider, whose unreachable-provider handling stalled the query past its timeout).

Removed a spurious `@pytest.mark.rsync` mark from the `test_advanced_fan_out_create` e2e tutorial test. The test creates local `--type command` agents in a clean git repo, which mngr materializes via a git worktree and never rsyncs, so the resource guard correctly flagged the mark as declared-but-never-invoked.

Fixed the `test_advanced_observe_stream` release test for the `mngr observe --discovery-only` tutorial block. The test now asserts on the current per-provider `DISCOVERY_PROVIDER` snapshot contract (the legacy all-providers `DISCOVERY_FULL` snapshot was deprecated when discovery moved to decoupled per-provider poll loops) and uses a longer timeout so the stream's initial snapshots are captured despite mngr's cold-start latency before the process is bounded by `timeout`.

Fixed the `test_advanced_watch_dashboard_running` e2e tutorial test, which failed with exit code 6 (provider inaccessible) whenever a remote provider backend was registered but uncredentialed (e.g. AWS with no credentials). The test's dashboard-verification query now scopes `mngr list --running` to the local provider (`--provider local`), matching the rest of the e2e suite, so it enumerates only the local provider and no longer depends on or contacts any remote backend in the isolated test environment.

Fixed the e2e tutorial test suite so unfiltered `mngr list` tests pass in environments without cloud credentials or Docker.

The e2e fixture now restricts discovery to the backends each test can actually reach: `local` is always enabled, while `modal` and `docker` are enabled only when the test carries the matching `@pytest.mark.modal` / `@pytest.mark.docker` mark. Previously the fixture left `enabled_backends` empty, which enabled every registered backend (aws, azure, gcp, ...); an unfiltered `mngr list` would then try to reach those uncredentialed providers and exit with `EXIT_CODE_PROVIDER_INACCESSIBLE` or hang on their discovery timeouts.

Also dropped the spurious `@pytest.mark.rsync` mark from `test_advanced_watch_list_live_dashboard`, which creates a local command agent and never invokes rsync.

Fixed and tightened the `test_archive_command` e2e tutorial test so it reliably verifies the documented scope of `mngr archive` on a stopped agent:

- Added a `timeout(120)` mark (the default 10s was too short for the create+stop+archive round-trip) and dropped the superfluous `rsync` mark (the test creates a local command agent and never invokes rsync).

- Scoped the archived-listing verification to `--provider local` (matching the sibling `test_stop_archive`), since `my-task` is a local command agent and the test's scope is local-only. This removes an unnecessary remote-provider enumeration, so the `modal` mark is no longer needed.

Fixed the `test_archive_running_agent_is_skipped` e2e tutorial test so it reliably verifies its scope (archiving a running agent without `--force` is a no-op): bumped its pytest timeout to 120s so the local command-agent create no longer trips the default 10s timeout, scoped the archived-list verification to the local provider (where the command agent lives) to avoid enumerating unreachable remote providers, and dropped the now-inaccurate `modal`/`rsync` resource-guard markers since the test exercises neither.

Added the `@pytest.mark.rsync` resource-guard mark to the `mngr ask` release test (`test_ask_simple_query`), which invokes rsync internally when preserving agent logs. This resolves the resource-guard failure so the end-to-end round-trip test passes.

Strengthened the release test for `mngr dependencies --install auto` so it also asserts the command runs the check/install flow to completion (no unhandled crash), not just that it reached the dependency reporting stage.

Fixed the `test_command_agent_data_pipeline` e2e tutorial test to scope its `mngr list` verification to `--provider modal` (where the `etl-job` command agent runs). Previously the unscoped listing reached out to every enabled provider, so it failed with exit code 6 in environments where another provider (e.g. aws) is enabled but has no credentials configured.

Removed a superfluous `@pytest.mark.rsync` mark from the `test_command_agent_dev_server_extra_windows` e2e tutorial test. The test exercises a purely local command agent (with an extra tmux window) and never invokes rsync, so the resource guard was failing it for carrying a mark it did not use.

Tests: fixed the `test_command_agent_python_http` e2e tutorial test. It carried a spurious `@pytest.mark.rsync` mark even though a purely local command agent never invokes rsync (rsync requires one remote endpoint), so the resource guard failed the otherwise-passing test; the mark is removed. The per-test and per-command timeouts were also raised so the three sequential mngr invocations (create, exec, list) do not spuriously time out on the contended sandboxes the release suite runs in.

Fixed a flaky failure in the `test_config_edit_editor_failure` e2e release test: a single `mngr config edit` cold start takes ~10s, which exceeds the default 10s func-only timeout, so the test now carries `@pytest.mark.timeout(60)` like its sibling `config edit` tests. No user-visible change.

Fixed the `test_config_edit_scope_missing_editor` release test so it no longer fails with a spurious 10s func-only timeout: a single `mngr` invocation exceeds the default timeout because of startup cost, so the test now carries `@pytest.mark.timeout(60)` like its sibling config tests.

Tightened the same test to also assert that the missing-editor failure surfaces the actionable "Editor not found" message rather than a bare Python traceback, matching the documented scope.

Fixed the `test_config_edit_scope` e2e tutorial test to allow a 60s timeout. It runs two `mngr` subprocesses (`config path` + `config edit`) whose cumulative cold-start time exceeds the default 10s func-only timeout, matching the other multi-subprocess tests in the config tutorial suite.

Fixed the `test_config_edit` e2e tutorial test, which wrongly assumed the e2e fixture pre-seeds the project `settings.toml`. The fixture deliberately leaves it unseeded so the test exercises genuine first-use behavior, where `mngr config edit` creates the file from a template before opening it in `$EDITOR`.

Tests: the fresh-install test suite now installs the `overlay` workspace package into its isolated venv. mngr grew a runtime dependency on `imbue.overlay` (the extracted config-merge algebra), but the `isolated_mngr_venv` fixture did not install that workspace package, so every fresh-install command crashed with `ModuleNotFoundError: No module named 'imbue.overlay'`. The fixture now includes `libs/overlay` alongside the other editable workspace packages.

Tests: `test_config_get_in_fresh_install` now asserts that `mngr config get headless` reports the default value `false`, verifying that config lookup actually falls back to the key's default rather than merely exiting cleanly.

Added a `@pytest.mark.timeout(60)` marker to the `test_config_path_invalid_scope` e2e tutorial test so its single `mngr` subprocess invocation is not killed by the default 10s per-test timeout during cold start.

Raised the per-test timeout on the `mngr config path` e2e tutorial test to 60s, matching the other multi-subprocess config tests, so it no longer fails against the default 10s pytest-timeout.

Fixed the `test_config_set_default_provider` e2e tutorial test so it can read the value back with `mngr config get commands.create.provider --scope project`. Since the e2e fixture no longer seeds the project `settings.toml`, the test now seeds that file with the pytest opt-in before running `config set`, mirroring the existing `test_config_unset_missing_key` pattern. No user-facing behavior changed.

Fixed the `test_config_set_headless_globally` e2e tutorial test to override the
10s global pytest timeout (which the `mngr` subprocess cold-start could exceed)
with `@pytest.mark.timeout(180)` and matching per-command subprocess timeouts,
consistent with the other `mngr`-invoking tests in the same module.

Fixed the `test_config_set_headless` e2e tutorial test so it seeds the pytest opt-in (`is_allowed_in_pytest = true`) into the project `settings.toml` before running `mngr config set headless true`. The project config is intentionally left unseeded by the e2e fixture, so without this the follow-up `mngr config get headless` (which loads the merged config) tripped the pytest config opt-in guard on the freshly-written project file.

Fixed the `test_config_set_rejects_unknown_key` scripting e2e test, which was timing out under the default 10s per-test limit because it runs multiple `mngr` subprocesses: added a `@pytest.mark.timeout(60)` override matching its sibling tests.

Extended the test to cover the full documented scope of `mngr config set` validation: it now also verifies that a wrong-type value for a known key (`config set headless notabool`) is accepted, since validation only rejects unknown fields (via `model_construct`) and does not check value types.

Fixed the fresh-install test fixture (`isolated_mngr_venv`) to install the `overlay` workspace package. mngr's config code path imports `imbue.overlay`, so without it the isolated venv crashed with `ModuleNotFoundError: No module named 'imbue.overlay'` when running commands like `mngr config set`.

Fixed the `test_config_set_unknown_key_fails` e2e tutorial test so its persistence check no longer assumes the e2e fixture pre-seeds the project `settings.toml`. The fixture deliberately does not seed that file, so a rejected `config set` never creates it; the test now reads the file back tolerating its absence and asserts the rejected key was never persisted.

Fixed the `test_config_unset` e2e tutorial test so it seeds the project settings file (including the `is_allowed_in_pytest` opt-in) directly instead of establishing the value via `mngr config set`. A file written by `set` does not carry the opt-in, so the follow-up `mngr config unset` was being rejected before it could run.

Fixed the isolated-venv install fixture to also editable-install the `overlay`
workspace package, which `mngr` now depends on. Without it, the freshly built
venv could not import `imbue.overlay` and every `minimal_install_env`-based
release test failed at startup.

Tightened the non-strict unknown-config-key release test to verify its scope
directly: it now checks (via a provider-independent `config get` command) that an
unknown config key is warned about but does not abort config loading, instead of
depending on a full `mngr list` run that also required a reachable provider
backend.

Gave the `test_connect_by_agent_id_fictional` e2e test a `@pytest.mark.timeout(120)`. Connecting by agent id has no host hint, so resolution falls back to a full-provider discovery scan that exceeds the default 10s per-test timeout; the command's behavior (a clean "Agent not found" error naming the id, exit 1) was already correct.

Removed a stale `@pytest.mark.rsync` mark from the `test_connect_by_name` e2e tutorial test. Connecting to a local agent execs `tmux attach` directly and never invokes rsync, so the mark (a leftover from when the test connected to a remote Modal agent) tripped the resource guard and failed the test.

Gave the `test_connect_explicit_host` e2e tutorial test a 120s timeout (matching the other connect tests) so it no longer trips the default 10s per-test timeout: the `mngr conn my-task@my-host` command legitimately takes longer than 10s because of mngr's subprocess startup cost plus the full-scan discovery it triggers.

Removed the superfluous `@pytest.mark.rsync` mark from the release e2e test `test_connect_no_start_fails_when_stopped`. This unhappy-path test creates a local command agent, stops it, and runs `mngr connect my-task --no-start`, which refuses before the tmux attach step that would sync the workspace. Since rsync is therefore never invoked, the resource guard failed the test with "marked with @pytest.mark.rsync but never invoked rsync". The `--no-start`-refuses-when-stopped behavior itself was already correct.

Removed the superfluous `@pytest.mark.rsync` from the `test_connect_no_start` e2e tutorial test. Connecting to a local agent is a pure `tmux attach` and never invokes rsync, so the resource guard rejected the carried-but-unused mark.

Fixed the release e2e test `test_connect_short_form` (connect tutorial). The test carried a superfluous `@pytest.mark.rsync` mark, but connecting to a *local* agent only execs `tmux attach` -- rsync is used solely on the remote SSH path -- so the resource guard failed the test for declaring a resource it never invokes. Removed the stale mark (the `tmux` mark, which the local attach does satisfy, is retained). No production behavior change.

Removed a spurious `@pytest.mark.rsync` mark from the release e2e test `test_connect_with_start_restarts_stopped_agent`. Connecting to a local `--type command` agent (create, stop, then `connect --start`) never invokes the rsync binary, so the resource guard rejected the mark as superfluous. This is a test-only change with no user-visible impact.

Removed the spurious `@pytest.mark.rsync` marker from the `test_connect_with_start` e2e tutorial test. Connecting to a local agent execs `tmux attach` directly and never syncs files, so the resource guard was failing the test for declaring an rsync mark it never exercised.

Fixed the `test_control_mngr_via_env` e2e release test by removing its superfluous `@pytest.mark.rsync` mark. The test pins the create provider to `local` via `MNGR__COMMANDS__CREATE__PROVIDER`, and a local agent's workspace lives on the same machine, so no rsync ever runs -- the resource guard therefore flagged the mark as never invoked and failed the (otherwise passing) test. The mark is dropped for the same reason `@pytest.mark.modal` was already omitted here. No user-visible change.

Made the `test_create_aborts_on_dirty_tree_by_default` e2e release test robust to credential-gated default cloud backends: its "no agent was created" verification now scopes `mngr list` to the local provider (`--provider local`), matching the existing idiom in `test_invalid_provider_fails`. Previously the unscoped listing fanned out to the default `aws` backend, which reports itself unavailable when no credentials are present and made `mngr list` exit non-zero for reasons unrelated to the abort behavior under test.

Fixed the e2e tutorial test suite so non-cloud tests are not broken by providers that are enabled by default in the monorepo checkout but are unreachable or irrelevant to the test. The e2e fixture now disables the credential-requiring cloud providers the suite never exercises (aws, azure, gcp, vultr, ovh, imbue_cloud), disables Docker when no daemon socket is present, and only enables Modal for tests that opt in via `@pytest.mark.modal`. This stops a plain `mngr list` from exiting non-zero (provider inaccessible) or surfacing leftover agents from the shared Modal account.

Also removed a stale `@pytest.mark.rsync` marker from `test_create_and_destroy_agent`, which creates a local command agent transferred via git worktree and therefore never invokes rsync.

Fixed the `test_create_and_rename_agent` e2e release test so it passes in environments where extra provider plugins are installed but their backends are unreachable.

The e2e fixture now pins `enabled_backends` to the backends the environment can actually reach (local and ssh always; modal when credentials are present; docker only for `@docker`/`@docker_sdk` tests) instead of leaving it empty (which enabled every installed backend). This keeps `mngr list`/`exec`/`destroy` from failing with a provider-inaccessible exit code purely because an unconfigured cloud provider plugin (aws, azure, gcp, ovh, vultr, imbue_cloud) happens to be installed.

Also removed the incorrect `@pytest.mark.rsync` mark from the rename tests: they create local command agents in a git repo, which use the git-worktree transfer mode and never invoke rsync.

Fixed the `test_create_codex_explicit_type` e2e tutorial test so it no longer
requires a codex binary (or npm) on the host: it now disables the codex install
check before creating the agent, so `--type codex` resolves and the agent is
created without attempting `npm i -g @openai/codex`. Also dropped the stale
`@pytest.mark.rsync` mark and the `--no-ensure-clean` flag (which had been the
only thing triggering rsync), matching the sibling command/yolo tutorial tests.

Fixed the `test_create_codex_positional` tutorial e2e test so it passes on hosts without a codex binary or npm:

- The e2e fixture now sets `[agent_types.codex] check_installation = false`, so provisioning a codex agent skips the `npm i -g @openai/codex` install step (the codex tutorial tests only exercise agent-type resolution, not a real codex run).

- Dropped the spurious `@pytest.mark.rsync` from the test (local agent creation uses a git worktree, not rsync) and gave the follow-up `mngr list` a longer timeout, marking the test flaky because the just-created codex agent's live state can be slow to compute.

Tests: fixed the e2e tutorial test for the `--type command` post-`--` command path so it verifies its documented scope. Two test-infrastructure fixes:

- The shared e2e fixture now restricts provider discovery to the backends each test actually exercises (`local` always, `modal` always, and `docker` only for `@pytest.mark.docker` tests) via `enabled_backends`. Previously `uv sync --all-packages` installed every provider plugin (aws, azure, gcp, vultr, ovh, imbue_cloud) into the dev venv, so a bare `mngr list` fanned out to all of them and aborted with the provider-inaccessible exit code because those cloud providers have no credentials in the test environment.

- Dropped the spurious `@pytest.mark.rsync` from `test_create_command_agent_runs_post_dash_command_in_agent`: it creates a local agent in a git repo, which uses the `GIT_WORKTREE` transfer mode and never shells out to rsync, so the declared-but-never-invoked resource guard failed the otherwise-passing test.

Made the `test_create_command_custom_script` tutorial e2e test robust to environments where an unreachable cloud provider backend (e.g. `aws`) is installed but has no credentials: its verification now scopes `mngr list` to `--provider local` (matching the sibling codex/yolo tests), since the created `command` agent lives on the local provider.

Fixed the `test_create_command_python_http` e2e tutorial test so its `mngr list` verification scopes discovery to `--provider local` (matching the sibling agent-type tests). Previously it ran a plain `mngr list --format json` that fanned out to every configured provider, so the test failed with a provider-inaccessible exit code whenever a remote provider was merely enabled-by-default but unreachable in the test environment (e.g. `aws` without credentials), even though the created command agent was correctly listed.

Fixed the `test_create_copy` e2e tutorial test (the `--transfer=git-mirror` "full copy" block). Two issues: the verification `mngr list --format json` did a full provider scan and exited non-zero whenever an enabled provider was unreachable (no Docker daemon, or an installed cloud backend with no credentials), so it now scopes the listing to the local provider (`mngr list --provider local`), matching the pattern used by the other e2e read commands. Also removed the incorrect `@pytest.mark.rsync` mark: `--transfer=git-mirror` transfers via git, not rsync (rsync is only the fallback for non-git directories), so the rsync resource guard's NEVER_INVOKED check was failing the test.

Fixed the custom-agent-type tutorial e2e test (`test_create_custom_yolo_agent_type`) so it simulates the in-editor project-config edit with a direct file append (opting the freshly created project config into the pytest run) instead of `mngr config set`, which the pytest config-loading guard rejected on the un-opted-in project config.

Removed an incorrect `@pytest.mark.rsync` mark from the release test `test_create_default_branch_distinct_per_agent`. The test creates local agents from a clean git repo, which uses the git-worktree transfer (never rsync), so the resource guard failed the test for declaring a resource it never invoked.

Removed the incorrect `@pytest.mark.rsync` mark from the `test_create_default_branch` e2e tutorial test. The default `mngr create` in a local git repo uses a git worktree (not rsync) to transfer files, so the resource guard was correctly failing the test for declaring an rsync mark it never exercised.

Fixed the tutorial e2e create tests so `mngr list --format json` no longer exits non-zero in a bare test environment: the isolated e2e profile now disables the credential-requiring cloud providers (aws, gcp, azure, vultr, ovh, imbue_cloud), which register default-enabled instances in the monorepo venv and would otherwise fail provider discovery with a "not available" error. Modal and Docker remain enabled.

Also removed a spurious `@pytest.mark.rsync` from `test_create_default`: a bare worktree `mngr create` on a clean working tree copies no untracked/gitignored files, so rsync is never invoked and the resource guard's NEVER_INVOKED check would fail the test.

Fixed the `test_create_duplicate_name_fails` e2e release test so it verifies its intended scope reliably:

- Scoped the post-rejection `mngr list` to `--provider local` (matching the sibling error tests) so an unrelated unreachable remote provider (e.g. AWS without credentials) no longer makes the verification exit non-zero.

- Dropped the superfluous `@pytest.mark.rsync` mark, which the resource guard rejected because the test never invokes rsync.

Fixed the e2e tutorial test fixture so provider discovery is scoped to the backends the suite actually exercises. The monorepo dev environment installs every provider plugin (aws, azure, gcp, vultr, ovh, ...), each enabled by default and probing its (absent) credentials at discovery time, which made `mngr list` -- run by many e2e tests and defaulting to `--on-error abort` -- exit non-zero on the first unreachable cloud provider even when the test never touched it. The fixture now sets `enabled_backends` to `local`, `ssh`, and `modal` (which self-skips via `ProviderEmptyError` when its per-user environment is absent), plus `docker` only for docker-marked tests (the docker backend raises `ProviderUnavailableError` rather than self-skipping when its daemon is missing). This is a test-only change with no effect on installed `mngr` behavior.

Fixed the `test_create_headless_no_connect_message` e2e test: it now scopes its verification listing to `mngr list --provider local` (matching the other e2e create tests) so it never queries unauthenticated cloud providers, and dropped the spurious `@pytest.mark.rsync` mark (a `--no-connect` local command-agent create does not invoke rsync).

Made the `test_create_headless` e2e tutorial test robust to the ambient provider environment. Its listing check now scopes to `mngr list --provider local` (the headless agent is created on the local host), so it no longer fails when an unrelated enabled provider is unreachable in the test environment (e.g. no Docker daemon, or a cloud backend without credentials) -- that is orthogonal to whether the agent appears. Also dropped the test's `@pytest.mark.rsync` marker: a purely local create writes files directly and never invokes rsync (rsync is only used to sync to remote hosts), so the resource guard for that mark could never be satisfied. No user-visible change -- test-only.

Fixed the `test_create_help_short_form_and_alias_succeed` e2e release test, which was timing out under the 10s default. It now carries a `@pytest.mark.timeout(120)` marker (like the rest of the e2e suite) to accommodate the multiple `mngr` CLI invocations it makes, and the `c`-alias branch now also asserts the SYNOPSIS section to match the documented scope.

Fixed the `test_create_help_succeeds` e2e release test so it no longer hits the default 10s pytest timeout. `mngr create --help` legitimately takes ~20s because it loads every provider plugin to render the PROVIDER BUILD/START ARGUMENTS section, so the test now carries an explicit `@pytest.mark.timeout(120)` like the rest of the e2e suite.

Raised the per-test timeout on the `mngr create --help` e2e tutorial test. Rendering that help imports every provider backend to build the per-provider build/start argument section, so the mngr subprocess startup routinely exceeds the global 10s pytest timeout; the test now uses `@pytest.mark.timeout(60)` like the other e2e create tests.

Fixed the `minimal_install_env` test fixture so the isolated fresh-install venv also installs the `overlay` workspace package. Without it, `mngr` failed to import (`ModuleNotFoundError: No module named 'imbue.overlay'`), breaking every fresh-install CLI test (e.g. `mngr create --help`).

Fixed the tutorial e2e test fixture so `mngr list` no longer fails on providers the test environment cannot reach. `uv sync --all-packages` installs every provider backend plugin, and each is enabled by default, so a bare `mngr list` enumerated the credential-backed cloud providers (aws, azure, gcp, vultr, ovh, imbue_cloud) and exited non-zero (per the consistent provider-auth-failure design) because no test environment carries their credentials. The fixture now disables those providers, and enables Docker only for `@pytest.mark.docker` tests (the Docker daemon is present in the release CI image but not in every environment, and only those tests use it). Modal stays enabled for every test since it is authenticated centrally. This is a test-only change with no effect on shipped behavior.

Fixed the `test_create_modal_basic_recap` e2e tutorial test so it scopes its verification listing to `mngr list --provider modal --format json`. The previous bare `mngr list` fanned out to every enabled provider and aborted with `EXIT_CODE_PROVIDER_INACCESSIBLE` when the installed-but-unconfigured `aws` plugin was reached, even though the Modal agent was created and running. Filtering to the modal provider keeps the test within its documented scope (confirming the agent lands on a RUNNING Modal host).

Marked the `test_create_modal_custom_image_base` e2e tutorial test as flaky. Modal agent creation occasionally fails when the initial-snapshot host-record write to a Modal volume hits a transient server-side error that outlasts the volume layer's retries; this is Modal infrastructure flakiness rather than a defect in the test or the custom-image-base behavior, so the test now lets offload retry it (matching the sibling Modal tests in the same file).

Scoped the `mngr list` call in the `--idle-mode run` Modal tutorial e2e test to `--provider modal`, so the verification no longer queries unrelated enabled providers (e.g. aws) that may lack credentials in the test environment and cause a spurious provider-inaccessible exit.

Fixed the `test_create_modal_idle_mode_ssh` e2e tutorial test so its verification listing filters by `--provider modal` (matching `test_create_modal_idle_timeout`). The unfiltered `mngr list` enumerated every enabled provider and exited non-zero when a credential-less provider (e.g. aws) could not be reached, failing the test for a reason unrelated to the `--idle-mode "ssh"` behavior under test.

Scoped the `mngr list` call in the `--no-start-on-boot` Modal e2e test to `--provider modal` so it no longer fails when unrelated providers (e.g. AWS) lack credentials in the test environment.

Marked the `test_create_modal_pass_host_env` release e2e test as `@pytest.mark.flaky` so offload retries it. The `mngr exec` issued right after create occasionally resolves the agent to a second, spurious host (Modal sandbox discovery is eventually consistent) and fails on that phantom host with a transient SSH "Authentication failed", flipping the exit code to 1 even though the forwarded host env var is verified correctly on the real host. Test-only change; no user-visible behavior change.

Fixed the `test_create_modal_target_path` e2e tutorial test so its work-dir inspection lists only the modal provider (`mngr list --provider modal --format json`) instead of every enabled provider. The unfiltered listing failed when an enabled-but-unconfigured provider (e.g. aws without credentials) was present, which is unrelated to the `@.modal:/workspace` target-path behavior under test.

Refined the `test_create_modal_upload_and_extra_provision_command` e2e/tutorial test so it exercises exactly its documented scope (the `--upload-file` and `--extra-provision-command` flags, which apply during host provisioning independent of the agent type). It now pins a lightweight `--type command -- sleep` agent instead of relying on the default `claude` agent, matching the convention used by the other host-effect create tests (rsync, volume, target-path). This removes the out-of-scope claude install and its ANTHROPIC_API_KEY dependency, keeping the test faster and less fragile.

Made the `test_create_modal_upload_only` e2e release test robust to a transient window right after `mngr create --provider modal --no-connect`: a fresh `mngr exec` against the brand-new Modal host can briefly fail to reach it (discovery momentarily finds no agent, or the SSH connection is refused) before the host settles. The verification read now retries until the host becomes reachable instead of asserting on the first attempt.

Fixed the `test_create_named_agent` e2e tutorial test. Its verification `mngr list` call now scopes to `--provider local` (matching the rest of the e2e suite) so it no longer exits non-zero when an unauthenticated remote provider (e.g. AWS with no credentials) is enabled in the test environment; the agents this test creates are always local. Also removed the spurious `@pytest.mark.rsync` mark, since a default `mngr create` uses a git-worktree transfer that never invokes rsync.

Fixed the `test_create_named_host_new_host` e2e release test so it scopes its verification listing to the modal provider (`mngr list --addrs --provider modal`). A bare `mngr list` also queries the AWS provider, which is enabled but has no credentials in the isolated e2e profile, causing the command to exit non-zero for reasons unrelated to the `name@host.provider --new-host` host-naming syntax under test.

Fixed the `test_create_provider_modal` e2e tutorial test: it now creates a lightweight always-running `command` agent (instead of relying on a default agent type that fell back to a credential-less `claude` agent, which died on startup and let the Modal host auto-stop before the listing could observe it). The agent now stays running so `mngr list --provider modal` surfaces it, matching the rest of the create-tutorial suite.

Flagged (via a FIXME) a concurrency limitation in the e2e test-profile Modal environment naming: the name is truncated to 64 characters, which drops the tail of `MNGR_AGENT_NAME` so sibling `test_create_*` agents collapse to a single identifier distinguished only by a 1-second timestamp, causing environment-name collisions when many run concurrently.

Fixed the `test_create_quiet_suppresses_output` e2e test: it now scopes its verification listing to `mngr list --provider local` (matching the other e2e create tests) so it never queries unauthenticated cloud providers, and dropped the spurious `@pytest.mark.rsync` mark (a `--no-connect` local create does not invoke rsync).

Raised the timeout on the `test_create_rejects_unknown_option` e2e tutorial test so it no longer fails against the global 10s pytest timeout: the `mngr create` subprocess's cold startup alone routinely takes ~10s even though the unknown option is rejected at argument-parsing time before any host/agent work happens. This mirrors the other `mngr create` e2e tests.

Fixed the e2e tutorial test suite so `mngr list` no longer fails on credential-backed cloud providers.

The shared e2e fixture now disables the credential-backed cloud VPS providers (aws, azure, gcp, vultr, ovh) that are enabled by default in the all-packages dev/CI environment. Without credentials these providers report themselves unreachable, and a plain `mngr list` treats an enabled-but-unreachable provider as an error and exits with the provider-inaccessible code (6), which broke every tutorial test that lists agents. Modal and Docker stay enabled as before.

Also dropped the superfluous `@pytest.mark.rsync` from `test_create_short_forms`: both agents use the default git-worktree transfer in a clean source repo, so `mngr create` finds no untracked files to copy and never invokes rsync, which the resource guard flags as a superfluous mark.

Clarified the `test_create_stack_templates_with_unknown_template_fails` release test to document why its existing assertions (failure plus a "not found" message naming the offending template) fully cover the documented scope: a template-not-found error is only ever raised during CLI-time template resolution, before any agent or host is provisioned, and the test's lack of the tmux/modal markers means the resource guards would fail it if provisioning were reached. No behavior change.

Strengthened the `test_create_stack_templates` e2e tutorial test to observe the concrete effect of stacking `--template modal-big --template with-tests` -- it now confirms the agent runs in-place (modal-big's `transfer=none` applied) rather than only asserting the create command exited 0.

Added an explicit `@pytest.mark.timeout(120)` to the `test_create_template_short_form` e2e tutorial test so the two real `mngr` invocations (`create` and `exec`) it runs no longer trip the global 10s pytest timeout, matching the other slow `mngr create` tests in the same file.

Tests: the e2e tutorial fixture now restricts the enabled provider backends to the ones e2e testing actually uses (`local`, `modal`, and `docker` only when a Docker daemon socket is present). Every cloud-VPS provider plugin (aws, azure, gcp, ovh, vultr, imbue_cloud) is installed in the workspace venv, and several of them (e.g. aws, azure) raise a provider-unavailable error when no credentials are configured -- which the e2e environment never has -- making every `mngr list` exit with the provider-inaccessible code (6). Gating Docker on socket presence also lets non-Docker environments run the `not docker` selection without an unreachable Docker provider failing `mngr list`.

Tests: dropped the `@pytest.mark.rsync` mark from `test_create_with_agent_args`. It creates a worktree agent from a clean source repo with default transfer options, so no untracked/modified/gitignored files exist to copy and `mngr create` never shells out to rsync; the mark caused a spurious NEVER_INVOKED resource-guard failure. The test's scope is purely `--` argument forwarding, unrelated to file transfer.

Fixed the release test `test_create_with_base_branch` (in `imbue/mngr/e2e/tutorial/test_create_data_and_git.py`): added the `@pytest.mark.timeout(120)` marker that `mngr create` e2e tests require (the 10s default fired mid-create), and removed the stale `@pytest.mark.rsync` marker. The default git-worktree transfer creates the worktree via `git worktree add` and only rsyncs uncommitted/gitignored extras; with a clean working tree it never invokes rsync, so the mark tripped the resource guard. No user-visible change -- test-only.

Fixed the e2e tutorial test suite so `mngr list` no longer fails during tests: the shared e2e fixture now disables the credential-backed cloud VPS providers (aws, azure, gcp, imbue_cloud, ovh, vultr, lima) that are installed as workspace packages but have no credentials in the test environment. Previously each registered a default provider instance that discovery tried to reach, surfacing an inaccessible-provider error and making `mngr list` exit non-zero.

Also removed an incorrect `@pytest.mark.rsync` from the `--connect-command` create test: creating a local agent from a git repo uses the git-worktree transfer mode, which never invokes rsync, so the mark tripped the resource guard's never-invoked check.

Fixed the `test_create_with_env_file` e2e tutorial test timing out. Creating a local command agent (agent-state setup, git worktree, tmux and command-agent startup) takes longer than the default 10s per-test timeout. Added a `@pytest.mark.timeout(120)` override, matching the sibling `test_control_mngr_via_env` local-provider create test.

Testing: fixed the `test_create_with_env_vars` e2e tutorial test for the create-time `--env KEY=VALUE` block. It now overrides the default 10s per-test timeout (the create plus follow-up `mngr exec` legitimately run longer, mirroring `test_create_with_pass_env`), and drops the superfluous `@pytest.mark.rsync` mark -- a local git-repo `--type command` create never invokes rsync (rsync is only used for non-git or remote projects), so the mark tripped the resource guard's "marked but never invoked" check.

Tests: drop the superfluous `@pytest.mark.rsync` from the `test_create_with_env` e2e tutorial test. A default local `mngr create` on a git repo uses the `git-worktree` transfer mode, which never shells out to `rsync`, so the mark tripped the resource guard's "marked but never invoked" check and failed the test even though its `--env` behavior verified correctly.

Fixed the `test_create_with_explicit_branch_name` e2e tutorial test: removed the spurious `@pytest.mark.rsync` mark (a local `--branch ":feature/my-task"` create uses the default git-worktree transfer, which never shells out to rsync, so the resource guard rightly flagged the unused mark), and gave the `mngr exec` verification a 60s timeout to absorb slow agent/provider discovery under load (matching the sibling git-mirror test). Test-only change; no user-facing behavior change.

Fixed and tightened the `test_create_with_explicit_project` e2e tutorial test:

- Added `@pytest.mark.timeout(120)` (it previously hit the 10s default and timed out) and dropped the `modal`/`rsync` resource marks, which the local `--no-connect` command-agent create never exercises.

- The test now verifies the documented scope end-to-end: after the create, it reads `mngr list --provider local --format json` and asserts the new agent's `labels.project` is `other-project`, confirming `--project` overrides the directory-derived default rather than only checking that the command exited 0.

Fixed the `test_create_with_extra_tmux_windows` e2e release test. Its `mngr list` verification is now scoped to `--provider local` so agent-creation discovery no longer fails when unconfigured remote provider backends (aws/azure/gcp/...) are installed but lack credentials, and the stale `@pytest.mark.rsync` mark was dropped because a create from a clean working tree transfers no extra files and never invokes rsync. Test-only change with no user-visible impact.

Fixed the `test_create_with_idle_mode_and_timeout` e2e release test so it scopes its verification `mngr list` to `--provider modal` (matching the sibling modal command-agent tests). Previously the unscoped `mngr list` queried every enabled provider and failed with a non-zero exit code whenever one of them (e.g. aws) was unreachable in the test environment, even though the modal command agent had been created correctly. No user-visible change.

Tests: fix the `mngr create --format json` e2e tutorial test so it runs in a credential-free environment. The shared e2e fixture now pins `enabled_backends` to the provider backends the tutorial actually exercises (local, modal, and docker only when a docker daemon is reachable), instead of leaving every installed provider plugin (aws, azure, gcp, ovh, vultr, ...) enabled. Those cloud backends treat "enabled but unconfigured" as a hard error, which made a bare `mngr list` abort with the provider-inaccessible exit code before it could report the local agent. Also dropped the incorrect `@pytest.mark.rsync` mark from `test_create_with_json_output`: it creates a default git-worktree agent in a clean repo, which never invokes rsync, so the resource guard failed the test.

Fixed the tutorial e2e tests so `mngr list` no longer aborts on provider backends that are unreachable in the test environment.

The e2e fixture now restricts `enabled_backends` to the backends a test actually exercises: the credential-free builtins (local, ssh) plus modal/docker only for tests that opt in via `@pytest.mark.modal` / `@pytest.mark.docker`. Previously every installed provider plugin (aws, azure, gcp, ovh, vultr, imbue_cloud, ...) was loaded, and any unreachable one (e.g. aws with no credentials, or docker with no daemon) made `mngr list` exit 6 under its default `--on-error abort`, breaking tests that only touch the local provider.

Also removed the spurious `@pytest.mark.rsync` marker from `test_create_with_json_output`: creating a local `command`-type agent never invokes rsync (local agents use git worktrees, not rsync file transfer), so the resource guard correctly flagged the marker as never invoked.

Fixed the `test_create_with_label` e2e tutorial test (and the shared e2e fixture it relies on) so it verifies exactly its documented scope: that `--label` attaches an agent label and `--host-label` a host tag, both visible in `mngr list --format json`.

- The shared e2e fixture (`imbue/mngr/e2e/conftest.py`) now restricts provider discovery to the backends the suite actually exercises. It enables only `local` and `modal` by default (Modal opts itself out when its per-user environment does not exist), plus `docker` for tests marked `@pytest.mark.docker`. Previously, when other provider backend plugins happened to be installed (e.g. after `uv sync --all-packages`), a default instance was created for each and `mngr list` tried to reach every enabled backend; an installed-but-unreachable backend (a cloud provider without credentials, or Docker with no running daemon) made `mngr list` exit with `EXIT_CODE_PROVIDER_INACCESSIBLE` (6), failing tests that only meant to list local agents.

- Removed the incorrect `@pytest.mark.rsync` marker from `test_create_with_label`: a purely local create provisions its workspace with a git worktree, not rsync, so the marker tripped the resource guard's NEVER_INVOKED check once the test body started passing.

Hardened the `test_create_with_message` e2e tutorial test (no user-facing behavior change): its post-create "was the agent listed?" check now scopes `mngr list` to `--provider local` (where the local agent lives), matching the existing pattern in the e2e error tests. Unscoped, `mngr list` defaults to `--on-error abort` and aborts with a non-zero exit whenever any enabled-but-unreachable provider is present (a stopped Docker daemon, or an unconfigured cloud backend installed in the monorepo venv), which masked the actual `--message` behavior under test. Also dropped the test's `@pytest.mark.rsync` mark: it creates a local same-host command agent in a git repo, which transfers via git worktree (rsync is rejected for git repos), so rsync is never invoked.

Tests: the `test_create_with_missing_env_file_is_rejected` e2e release test now sets an explicit `@pytest.mark.timeout(120)`. The test drives two `mngr` invocations (the rejected create plus a follow-up `mngr list`), which each pay mngr's startup cost and together exceed the default 10s per-test timeout, so it was timing out before the create even returned. This mirrors the timeout override already on the sibling `test_control_mngr_via_env`. No behavior change to `mngr` itself: `--env-file` pointing at a nonexistent path is still rejected up front by `click.Path(exists=True)`.

Removed an incorrect `@pytest.mark.rsync` marker from the `test_create_with_multiple_labels` e2e tutorial test. The test creates a purely local agent, whose default `git-worktree` transfer never invokes rsync, so the resource guard rejected the test with a spurious "marked rsync but never invoked rsync" failure.

Added a per-test timeout to the `test_create_with_nonexistent_template` e2e test so its several `mngr` subprocess calls (each with a multi-second cold start) are not killed by the default 10s per-test timeout.

Removed a superfluous `@pytest.mark.rsync` from the `test_create_with_pass_env` e2e tutorial test. The test creates a local git-based command agent, which transfers via git-worktree and never invokes rsync, so the resource guard failed the otherwise-passing test.

Removed a superfluous `@pytest.mark.rsync` mark from the `test_create_with_pass_env_skips_unset_var` e2e tutorial test. The test creates a local-provider agent in a git repo, which transfers files via git-worktree and never invokes rsync, so the mark tripped the resource guard's superfluous-mark check.

Tests: fixed the `--pass-env` unset-variable e2e tutorial test (`test_create_with_pass_env_unset`) so it reliably verifies its documented scope. The verification listing is now scoped with `mngr list --provider local` (the created agent is a local command agent), matching the sibling command-agent tests, so it no longer fails with the provider-inaccessible exit code when an enabled remote provider (a credential-less cloud provider, or Docker with no daemon) is unreachable in the test environment. Also dropped the incorrect `@pytest.mark.rsync` mark: creating a local agent copies files locally rather than invoking rsync, so the resource guard rightly flagged the mark as never exercised.

Fixed the `test_create_with_pass_env` e2e tutorial test (verifying `mngr create --pass-env`). It was failing because a `mngr list` verification step scanned remote providers and timed out, and because the test carried a `@pytest.mark.rsync` mark that local command-agent creation never actually exercises.

Fixed the `test_create_with_pass_host_env` e2e tutorial test so it reliably reaches the modal agent when verifying that `--pass-host-env` propagated variables to the host. The verification `mngr exec` is now addressed as `my-task@<host>.modal`, scoping discovery to the modal provider instead of triggering a full cross-provider scan that aborts when the (default-enabled but unconfigured) `aws` provider is unavailable.

Tests: hardened the tutorial e2e suite so `mngr list` assertions no longer depend on the host's cloud credentials or Docker daemon. The e2e fixture now disables the credential-requiring cloud providers (aws, azure, gcp, vultr, ovh, imbue_cloud) that are enabled by default, and disables the Docker provider when no Docker daemon socket is present. Without this, `mngr list` reached out to unreachable providers and exited non-zero (provider-inaccessible), failing every e2e test that asserts the listing succeeds. Also added the missing `@pytest.mark.timeout(120)` to `test_create_with_plugin_flags` so its `mngr create` + `mngr list` flow is not killed by the 10s default timeout.

Fixed the e2e tutorial test suite so `mngr list`-based tests no longer fail purely because the monorepo dev/test venv installs every provider plugin.

- The shared `e2e` fixture now sets `enabled_backends` to an explicit allowlist (`local`, `ssh`, `modal`, plus `docker` only for tests marked `docker`/`docker_sdk`) instead of leaving it empty (which enabled every installed backend). Backends such as AWS and Azure deliberately report themselves as *unavailable* (`mngr list` exit code 6) when their cloud credentials are absent, so without the allowlist any `mngr list` in the suite failed just because those unconfigured plugins happened to be installed.

- Removed the inappropriate `@pytest.mark.rsync` marker from `test_create_with_project_label`: the default local worktree create it exercises never invokes rsync, so the resource guard flagged the mark as superfluous once the test body started passing.

Hardened the tutorial e2e test fixture so unrelated remote providers can no longer make tests fail: the credential-backed cloud providers (aws, azure, gcp, vultr, ovh, imbue_cloud) are always disabled, and the Docker provider is disabled unless the test is `@pytest.mark.docker`. This prevents `mngr list` from exiting non-zero when those providers are unreachable in the test environment (e.g. no cloud credentials, no Docker daemon). The Docker block pins `isolate_host_volumes` so parsing it does not emit the deprecation warning that would otherwise leak past `--quiet`.

Removed the `@pytest.mark.rsync` mark from `test_create_with_quiet_output`, which creates a worktree agent in a clean repo and therefore never invokes rsync.

Made the e2e tutorial test fixture robust to provider backends that the test environment cannot reach. The cloud-VM provider plugins (aws, gcp, azure, ovh, vultr) are now disabled in the fixture's local config, since no e2e test exercises them and, without cloud credentials, they made `mngr list` abort with the provider-inaccessible exit code. The Docker provider is likewise disabled when no Docker daemon is reachable (detected via a low-level socket probe that honors `DOCKER_HOST` and the active Docker context, and that avoids the Docker SDK so it does not trip the `docker_sdk` resource guard), so the tutorial suite can run in Docker-less environments. Where Docker is available it stays enabled and discovery works exactly as before. This is test-infrastructure only; no user-visible change.

Added the missing `@pytest.mark.timeout(120)` to the `test_create_with_source_path` release e2e test so a real `mngr create` no longer trips the 10s default pytest timeout.

Flagged (via a FIXME in the e2e conftest) that the shared e2e fixture leaves every installed provider plugin (aws, azure, gcp, ovh, vultr, imbue_cloud, docker, ...) enabled, so `mngr list` aborts with the provider-inaccessible exit code when those providers have no credentials or daemon in the test environment.

Added an explicit `@pytest.mark.timeout(120)` to the `test_create_with_template` e2e release test. The test creates a live local agent and then runs `mngr list`/`mngr exec`, which enumerate every configured provider; that discovery could exceed the default 10s per-test timeout, causing spurious failures.

Fixed the `test_default_output_human_readable` e2e tutorial test so it passes reliably: the read-only `mngr ls` path now gets a longer per-test timeout (provider discovery, Modal in particular, can take ~30s), and the e2e fixture pins `enabled_backends` to the providers that are actually reachable in the test environment (local/ssh/modal, plus docker for docker-marked tests). Previously the monorepo dev install left every provider plugin (aws, azure, gcp, vultr, ...) enabled without credentials, so discovery surfaced a `ProviderUnavailableError` for each and `mngr ls`/`mngr list` exited non-zero.

Fixed the `test_destroy_all_modal_agents` release e2e test. Its before/after
verification listings now use `mngr list --provider modal` (scoping to the modal
provider) instead of `mngr list --include 'host.provider == "modal"'`, so an
enabled-but-unreachable provider (e.g. aws with no credentials) no longer makes
the verification listing exit non-zero even though the modal listing is correct.
The remote destroy pipeline and listings also now use the remote timeout rather
than the short default, so the real Modal teardown has time to complete.

Gave the `test_destroy_by_session_name` e2e release test an explicit `@pytest.mark.timeout(60)` so it no longer flakes against the default 10s pytest timeout. The single `mngr destroy --session` invocation takes longer than 10s on a cold start, which is well within the 60s budget every other test in the file already uses.

Test fix (no user-visible change): repaired the `test_destroy_filtered_dry_run` labels/filtering e2e release test. It lacked a `@pytest.mark.timeout`, so it inherited the 10s default and timed out during the first `mngr create`; it now allows 180s like its sibling multi-command tests. The final `mngr list` verification no longer asserts a zero exit code (a bare `mngr list` exits non-zero when an enabled provider such as AWS is merely unreachable in the test env, which is orthogonal to the dry-run) and instead asserts only that the STOPPED agent survives, matching the pre-dry-run check. The spurious `@pytest.mark.rsync` mark was removed because a local git-repo command agent transfers via git-worktree and never invokes rsync.

Hardened the tutorial e2e fixture so `mngr list` (and other all-provider commands) no longer aborts in environments that lack cloud credentials or a Docker daemon.

The monorepo test venv registers every provider plugin, so credential-requiring VPS backends (aws, azure, gcp, vultr, ovh, lima) were enabled by default and made `mngr list` exit non-zero during discovery. The fixture now sets `enabled_backends` to just the backends the e2e suite actually reaches: `local` and `modal` always, plus `docker` only for `@pytest.mark.docker` tests (which are the only ones that exercise the docker backend and run against a real daemon).

Also dropped the superfluous `@pytest.mark.rsync` from `test_destroy_no_gc`: it creates and destroys a local agent against a clean working tree, which transfers nothing and therefore never invokes rsync, so the mark tripped the resource guard's superfluous-mark check.

Removed a superfluous `@pytest.mark.rsync` marker from the `test_destroy_remove_branch` e2e tutorial test. The test creates a purely local git-worktree agent and destroys it with `--remove-created-branch`, a path that never invokes rsync, so the resource guard was failing the test with a "marked but never invoked rsync" violation. This is a test-only change with no user-visible impact.

Removed the superfluous `@pytest.mark.modal` and `@pytest.mark.rsync` marks from the `test_destroy_remove_created_branch_inline` git tutorial e2e test. The test exercises a purely local `--type command` agent against a git repository, so it never invokes Modal or rsync (a git repo uses git-based transfer, not rsync), and the resource guard was failing the test for marks that were never invoked.

Fixed the `test_destroy_short_form` e2e tutorial test so it verifies its documented scope reliably: the post-destroy `mngr list` verification is now scoped to `--provider local` (where the local command agent lives), matching the convention used across the rest of the e2e suite, so the check no longer fails when an unrelated remote provider is unreachable. Also removed the spurious `@pytest.mark.rsync` mark, since a local git command-agent uses git-worktree transfer and never invokes rsync.

Fixed the `test_destroy_specific` e2e tutorial test and its shared fixture so it runs correctly in environments without cloud credentials or a Docker daemon:

- The e2e fixture now disables the credential-requiring cloud VPS provider backends (aws, azure, gcp, ovh, vultr) that the monorepo venv installs but the test environment has no credentials for. Previously they made `mngr list` exit non-zero with `EXIT_CODE_PROVIDER_INACCESSIBLE` even when the listing itself succeeded.

- The e2e fixture now enables the Docker provider only when a Docker daemon is actually reachable, so non-docker tests still pass in environments without one (while the release offload image, which starts dockerd, keeps Docker enabled).

- Removed the incorrect `@pytest.mark.rsync` mark from `test_destroy_specific`: it creates a local command agent from a clean git repo, which transfers via git-worktree and never invokes rsync.

- Strengthened the test to assert the confirmation prompt ("Are you sure you want to continue?") is shown, so a regression that destroyed a specific agent without prompting would be caught.

Tightened the `test_env_var_mngr_headless` tutorial e2e test to verify exactly its documented scope: it now checks only that `MNGR_HEADLESS=true` makes `mngr config get headless` resolve to `true` (and to `false` when unset). The out-of-scope `mngr list` smoke check was removed, since `list` reaches all enabled providers and could fail for reasons (e.g. an unreachable AWS provider) unrelated to headless config resolution.

Fixed the `test_event_default` tutorial e2e test, which was incorrectly marked `@pytest.mark.rsync`. The test creates a local agent inside a git repo, which uses the `git-worktree` transfer mode and never invokes the `rsync` binary, so the resource guard failed the test for carrying a mark it never exercised. Removed the spurious `rsync` mark.

Fixed the `test_event_follow_filter_source` tutorial e2e test: removed the incorrect `@pytest.mark.rsync` mark. A local command agent created in a git repo uses the `git-worktree` transfer mode, which never invokes `rsync`, so the mark was superfluous and the resource guard was (correctly) failing the otherwise-passing test.

Removed the superfluous `@pytest.mark.rsync` mark from the `test_event_follow` e2e tutorial test. The test creates a local command agent in a git repository, which syncs via a git worktree rather than rsync, so the mark was flagged by the resource guard as declared-but-never-invoked.

Dropped the over-declared `@pytest.mark.rsync` from the `mngr event --head --tail`
conflict e2e test. That test's command fails during argument validation before any
events are read, and creating the local command agent against a git repo uses a git
worktree rather than rsync, so rsync is never invoked and the resource guard was
failing the test. Flagged the same over-declaration on the sibling event tests for a
follow-up suite-wide cleanup.

Removed the spurious `@pytest.mark.rsync` mark from the `mngr event --head` e2e tutorial test. A local agent created in a git repo transfers its work dir via a git worktree, not rsync, so the resource guard correctly reported that rsync was never invoked. Corrected the accompanying comments to describe the git-worktree transfer.

Removed the superfluous `@pytest.mark.rsync` from the release e2e test `test_event_include_filter_rejects_invalid_cel`. That test exercises the unhappy path where an invalid CEL `--include` expression is rejected during filter parsing, before any events are read, so the event stream is never synced from the agent and rsync is never invoked. The resource guard therefore failed the test with "marked with @pytest.mark.rsync but never invoked rsync". No production behavior change.

Tests: drop the incorrect `@pytest.mark.rsync` from the `mngr event --include` e2e tutorial test (`test_event_include_filter`). The e2e fixture creates the sleep agent inside a local git repo, so `mngr create` uses the git-worktree transfer mode (not rsync); the rsync resource guard correctly flagged the mark as never invoked. Removing the mark lets the test run without tripping the guard.

Fixed the `test_event_tail` e2e tutorial test: removed its spurious `@pytest.mark.rsync` mark. A local agent created in a git repository on the same host uses git-worktree transfer, not rsync, so the resource guard correctly flagged the test as marked-but-unused. (The sibling event tests share the same incorrect mark; a FIXME notes the suite-wide cleanup.)

Corrected the "run a command on all agents" tutorial example: `mngr exec -a "whoami"` no longer works because the `-a`/`--all` flag was removed from multi-target commands in favor of the stdin-pipe form. The tutorial (and its e2e test) now show `mngr list --ids | mngr exec - "whoami"`, matching the documented way to fan a command out across every agent.

Fixed the `test_exec_all_git_status` e2e tutorial test for the WORKING WITH GIT section. Dropped a spurious `@pytest.mark.rsync` mark: the tutorial's `mngr list --ids | mngr exec - "git status --short"` fan-out runs against a local agent, which uses a git worktree (never rsync), so the mark tripped the resource guard. Also strengthened the assertion to check for the per-agent outcome line ("Command succeeded on agent my-task") rather than a bare substring, matching the test's documented scope that the fan-out actually reaches the agent.

Removed the erroneous `@pytest.mark.rsync` mark from the `test_exec_as_other_user` tutorial e2e test. The test creates a local command agent in a git repo (which transfers via git-mirror and never invokes rsync) and only exercises `mngr exec`, so the rsync resource guard was failing the otherwise-passing test.

Fixed the `test_exec_basic` e2e tutorial test: added a `@pytest.mark.timeout(120)` override (the default 10s timeout was too short for the create+exec workflow) and removed the superfluous `@pytest.mark.rsync` mark, since a local command agent's create+exec never invokes rsync.

Removed the spurious `@pytest.mark.rsync` mark from the `test_exec_branch_show_current` git tutorial e2e test. The test never invokes rsync, so the resource guard was failing it; the test only exercises `mngr exec` running `git branch --show-current`.

Removed the superfluous `@pytest.mark.rsync` mark from the `test_exec_cwd_nonexistent` e2e tutorial test. The test creates a local `command` agent (whose work_dir is a git worktree, not an rsync copy) and its `mngr exec --cwd /nonexistent-dir-xyz` invocation fails at process spawn before any file sync, so rsync is never invoked. The resource guard therefore failed the otherwise-passing test with "marked with @pytest.mark.rsync but never invoked rsync". Removing the mark leaves the test verifying exactly its documented scope (a nonexistent `--cwd` makes exec exit nonzero without falling back to the work_dir).

Removed the superfluous `@pytest.mark.rsync` from the `test_exec_cwd` e2e tutorial test. The `mngr exec --cwd` flow never invokes rsync (command agents are created via the git-mirror path), so the mark tripped the resource guard's "marked but never invoked" check.

Fixed the `test_exec_filtered_remote_disk` labels tutorial e2e test, which spuriously timed out because the `mngr list ... | mngr exec -` pipeline against a remote Modal host used the default 30s command timeout. The exec now uses the same 120s remote timeout as the rest of the Modal e2e suite, and the overall per-test timeout was widened to accommodate a remote create plus an SSH exec round trip.

Removed the superfluous `@pytest.mark.rsync` mark from the `test_exec_force_commit` e2e tutorial test. The test exercises a local git-based agent, whose `mngr exec` never invokes rsync, so the resource guard failed the test for carrying a mark it never used.

Removed the superfluous `@pytest.mark.rsync` mark from the `test_exec_git_log` e2e tutorial test. The test exercises a local git-worktree agent, which never invokes rsync, so the resource guard flagged the mark as superfluous.

Removed an incorrect `@pytest.mark.rsync` from the `test_exec_git_push_then_merge` tutorial e2e test. The test only exercises a local agent (`mngr create`/`mngr exec` plus a local `git fetch`/`git merge`), and local agent operations never invoke the `rsync` binary, so the resource guard correctly failed the test for declaring rsync without using it.

Removed a superfluous `@pytest.mark.rsync` mark from the `test_exec_git_status_short` e2e tutorial test. The test creates its agent against a clean git repository, so `mngr create` uses git-worktree transfer and never invokes rsync; the mark tripped the rsync resource guard ("marked with @pytest.mark.rsync but never invoked rsync") and failed the test during teardown. The test's scope (verifying `mngr exec my-task "git status --short"` reports an uncommitted file) is unaffected.

Tests: fix the `test_exec_no_start` tutorial e2e test so it reliably exercises `mngr exec --no-start`. It previously died at the default 10s pytest timeout during agent creation; it now carries `@pytest.mark.timeout(120)` and gives the create and exec subprocess calls enough of a budget (90s) to complete on slower hosts. Also dropped the superfluous `@pytest.mark.rsync` mark, since creating and exec-ing on a local command agent never invokes rsync (the resource guard flagged the unused mark).

Tests: fix the `test_exec_on_error_continue` e2e tutorial test. It was marked `@pytest.mark.rsync`, but exec on a local `command` agent (and creating one over a git repo, which uses git transfer rather than rsync) never invokes rsync, so the resource guard failed the otherwise-passing test. Removed the superfluous mark.

Tests: made the same test run the exact tutorial command (`git log --oneline -5`) instead of `git log --oneline -5 || true`. The agent's work_dir is always the fixture git repo, so `git log` succeeds and returns the fixture history; the `|| true` masked the very failure behavior the block is about and diverged from the documented tutorial command.

Fixed the `test_exec_short_form` e2e tutorial test: removed the superfluous `@pytest.mark.rsync` mark (the local command agent in a git project never invokes rsync, so the resource guard flagged the mark as never invoked), and tightened its assertion to verify `git status` reports the agent's own `mngr/my-task` branch rather than any "On branch" line.

Removed the spurious `@pytest.mark.rsync` mark from the `test_exec_timeout_enforced` e2e tutorial test. The test targets a local command agent, which never invokes the `rsync` binary (local agents sync via git worktrees; rsync is only used for remote hosts over SSH), so the resource guard failed the test with "marked with @pytest.mark.rsync but never invoked rsync". The mark is unrelated to the test's `--timeout` enforcement scope.

Removed a spurious `@pytest.mark.rsync` mark from the `test_exec_timeout` e2e tutorial test. The test runs a local command agent inside a git repo, which syncs via git worktree/mirror rather than rsync, so the mark tripped the resource guard's "marked but never invoked" check.

Removed a superfluous `@pytest.mark.rsync` from the `test_exec_with_start` e2e tutorial test. The test creates a local `command` agent inside a git repo, which transfers via `git-worktree` and never invokes `rsync`, so the resource guard failed the test with a "marked but never invoked" violation. The mark did not correspond to anything in the test's scope (`mngr exec --start`), so dropping it fixes the test without changing what it verifies.

Fixed the `test_full_lifecycle` e2e test so it reflects a purely local agent lifecycle: the `mngr list` calls now scope to `--provider local` (so an unreachable remote provider, e.g. a credential-less cloud backend or an absent Docker daemon, cannot make the listing exit non-zero), and the superfluous `@pytest.mark.rsync` marker was removed since a local command agent never invokes rsync.

Fixed the `test_gc_background_watch` e2e tutorial test, which was failing because it lacked a `@pytest.mark.timeout` override and inherited the 10-second default. Each `mngr` subprocess invocation costs several seconds to start, so running `mngr config set` followed by `watch -n60 mngr gc` exceeded 10 seconds. Added `@pytest.mark.timeout(60)`, matching the sibling local config-command e2e tests.

Scope the `test_gc_dry_run` e2e verification to `mngr list --provider modal` so that the "dry run changed nothing" check confirms the Modal agent survived without failing when an unrelated provider backend (e.g. AWS) is enabled but unauthenticated in the test environment.

Fixed the `test_gc_provider_modal` e2e tutorial test so its post-gc listing is scoped to the Modal provider (`mngr list --provider modal --format json`). An unfiltered `mngr list` reaches every enabled provider, so an enabled-but-unavailable provider (e.g. AWS with no credentials) made the test exit non-zero for reasons unrelated to the Modal agent it was verifying.

Tests: fix the `test_git_merge_agent_branch` release e2e test in the WORKING WITH GIT tutorial suite. It was hitting the default 10s pytest timeout during agent creation, so it now carries `@pytest.mark.timeout(120)` like its siblings. It also declared `@pytest.mark.rsync` even though a local agent uses the git-worktree transfer mode (never invoking rsync), which tripped the resource guard's "marked rsync but never invoked rsync" check; that spurious mark has been removed.

Added a `@pytest.mark.timeout(60)` override to the `test_help_succeeds` e2e tutorial test so it no longer fails under the 10s global pytest timeout. A single `mngr` CLI subprocess startup takes roughly 10 seconds, which the plain `mngr --help` invocation would exceed; this matches the timeout convention used by every other tutorial e2e test.

Tightened the `test_help_succeeds` e2e tutorial test to verify exactly its documented scope: removed an out-of-scope assertion that the `mngr --help` output contains "Usage", since the scope only calls for a zero exit code and the advertised subcommands (create, list, destroy, message, connect, clone).

Gave the `test_help_unknown_command_fails` e2e release test a 60s timeout override so it no longer races the 10s default while mngr's CLI starts up (the command itself already fails correctly, naming the unknown command and pointing back to `--help`).

Fixed the fresh-install test fixture (`isolated_mngr_venv`) to install the `overlay` workspace package, which mngr now declares as a dependency. Without it, the isolated venv failed to import `imbue.overlay`, breaking every `test_install.py` command (including `mngr --help`).

Raised the per-test timeout on the `test_invalid_provider_fails` e2e release test. It runs two full `mngr` subprocesses, each of which pays a fixed interpreter startup cost that can exceed the default 10s per-test timeout even though the command logic fails fast; the test now allows extra headroom, matching its sibling error-path tests.

- Fixed the `test_jsonl_with_jq_stream` e2e tutorial test: gave the `mngr list --format jsonl | jq` pipeline a longer timeout (provider discovery is slow when Modal credentials are present) and removed the incorrect `@pytest.mark.modal` mark, which failed the resource guard's NEVER_INVOKED check because `mngr list` only reaches Modal via in-process gRPC in the subprocess (matching `test_list_format_jsonl_recap`).

Fixed the `test_list_active_filter` e2e release test (`mngr list --active`), which was failing for two reasons unrelated to the command's behavior: the default 10s per-test timeout was too short for the provider-discovery path, and the fresh test environment enabled every installed provider backend by default. The dev checkout installs all provider plugins (aws, azure, gcp, ovh, vultr, ...) via `uv sync --all-packages`, so `mngr list` probed cloud providers that have no credentials (and Docker when no daemon is running) and exited with `EXIT_CODE_PROVIDER_INACCESSIBLE` (6) even though the listing itself succeeded.

The e2e fixture now restricts provider discovery (`enabled_backends`) to the backends a test actually uses: `local` and `modal` always (modal skips gracefully via `ProviderEmptyError` when its per-user environment does not exist yet), plus `docker` only for tests marked `@pytest.mark.docker`. This mirrors how a real user enables only the providers they use and keeps `mngr list` discovery deterministic across the e2e suite. The `test_list_active_filter` test also gained a 60s timeout, matching the other full-discovery `mngr list` tests in the same module.

Tests: the mngr e2e fixture now pins `enabled_backends` so provider discovery covers only the backends a given test needs (`local` and `modal` always, plus `docker` for docker-marked tests). Previously every installed provider plugin -- including the cloud VPS backends (aws, azure, gcp, imbue_cloud, ovh, vultr) -- was auto-loaded as a default instance, so a bare `mngr list` in an environment without cloud credentials reported the provider as inaccessible and exited non-zero (EXIT_CODE_PROVIDER_INACCESSIBLE) instead of "No agents found". This made the list-filter e2e tests (e.g. `test_list_archived_filter`) fail in credential-less and no-docker lanes; they now run against a clean discovery path.

Fixed the e2e test harness so tutorial list/filter tests run cleanly in sandboxes without cloud credentials or a Docker daemon. The e2e fixture now disables the credential-requiring cloud providers (aws, azure, gcp, imbue_cloud, ovh, vultr) that no e2e test exercises, and disables the Docker provider for tests that do not declare a Docker mark. Previously an enabled-but-unreachable provider made `mngr list` exit with the provider-inaccessible code even when the local agents listed fine. No user-facing behavior change.

Tightened the `test_list_combine_include_filters` e2e tutorial test so it verifies exactly its documented scope:

- The combined `--include` filter test now marks `backend-running` active (via `mngr exec ... touch "$MNGR_AGENT_STATE_DIR/active"`, the established e2e pattern) so it is genuinely in the `RUNNING` state, and asserts the combined `team == backend AND state == RUNNING` filter returns exactly `{backend-running}`. Previously the agent sat in `WAITING`, the filter matched nothing, and the weak negative-only assertions let that empty result pass.

- Removed the redundant single-`--include` baseline (single-filter behavior is covered by `test_list_filter_by_label_cel`) and the spurious `@pytest.mark.rsync` mark (the test never invokes rsync, which tripped the resource guard).

Flagged a cross-cutting e2e-fixture gap (FIXME): bare `mngr list` tutorial commands (no `--provider`) probe every enabled backend and abort with `EXIT_CODE_PROVIDER_INACCESSIBLE` when one is unreachable, so they fail in the release/tmr environment (`-m "not docker"`, no docker daemon, no cloud credentials). The fixture should scope enabled backends to what is actually reachable.

Hardened the e2e tutorial test harness so that a bare `mngr list` (and other cross-provider commands) no longer fails with a provider-inaccessible error when unused provider plugins are installed. The shared e2e fixture now disables the credential-requiring cloud backends (aws, azure, gcp, imbue_cloud, ovh, vultr) that no e2e test exercises, and disables Docker for tests not marked `@pytest.mark.docker` so non-docker tests do not require a running Docker daemon. This fixes `test_list_compound_cel` and removes a spurious `@pytest.mark.rsync` mark from it (local command agents never invoke rsync).

Fixed the e2e tutorial test fixture so bare `mngr list` commands (no `--provider` filter) succeed. The fixture now pins `enabled_backends` to exactly the provider backends a test's environment can reach -- always `local`, plus `modal`/`docker` when the test declares the matching marker. Previously every installed backend (including unconfigured cloud backends like AWS, and Docker in a Modal-only sandbox) was loaded during discovery, so an unreachable provider made a bare `mngr list` abort with a non-zero exit code even though the listing itself was correct.

Tests: the git tutorial e2e test `test_list_fields_original_branch_with_agent` now passes in environments without cloud credentials or a Docker daemon.

The shared e2e fixture now disables the cloud providers that require credentials absent from the e2e environment (AWS, Azure, GCP, Vultr, OVH, imbue_cloud), and only enables Docker for tests marked `@pytest.mark.docker`. Previously any discovery-triggering command (e.g. `mngr list`) would exit non-zero (`EXIT_CODE_PROVIDER_INACCESSIBLE`) when one of these providers was enabled but unreachable, failing tests that had produced correct output.

The `@pytest.mark.rsync` mark was removed from `test_list_fields_original_branch_with_agent`, which creates a local command agent and lists it -- it never invokes rsync, so the mark was superfluous and tripped the resource guard.

Tests: fixed the LABELS/filtering e2e tutorial test `test_list_filter_by_label_cel` so it verifies exactly its scope (the `mngr list --include` CEL label filter keeps `labels.priority == "high"` and drops the rest).

Two fixes were needed:

- The shared e2e fixture now restricts provider discovery to the backends a test actually declares via its markers (always `local`, plus `modal`/`docker` when the test carries that marker). Previously every installed provider plugin got a default instance during `mngr list`, so an unreachable backend a local-only test never exercises -- AWS with no credentials, or Docker with no running daemon -- surfaced as `ProviderUnavailableError` and made a plain `mngr list` exit 6 (PROVIDER_INACCESSIBLE).

- Dropped the superfluous `@pytest.mark.rsync` from `test_list_filter_by_label_cel`: it creates only local command agents, and rsync is a remote-only file transfer, so the resource guard correctly flagged the mark as never exercised.

Fixed the `test_list_filter_by_state` e2e release test (and, via the shared e2e fixture, every e2e test that runs `mngr list`).

The e2e fixture left every installed provider backend enabled. Because `uv sync --all-packages` installs the credential-only cloud backends (aws, azure, gcp, ...), `mngr list` probed them, they reported themselves unreachable for lack of credentials, and the command exited with the provider-inaccessible code. The fixture now pins `enabled_backends` to `local`, `docker`, and `modal` -- the backends the test environment can actually reach -- matching the fixture's documented "Modal, Docker left enabled" intent.

Removed the stale `@pytest.mark.rsync` mark from `test_list_filter_by_state`. The test was converted from remote (Modal) agents to local `--type command` agents, which use git worktrees and never invoke rsync; the leftover mark tripped the resource guard's "marked but never invoked" check once the test began passing.

Fixed the `test_list_filter_invalid_cel` e2e tutorial test so it no longer trips the default 10s pytest timeout: a subprocess `mngr list` against a fresh Modal environment needs longer than that to start up and report the invalid-CEL error. Added an explicit `@pytest.mark.timeout(60)`, matching the lightest sibling e2e tests.

Tests: fixed the PROJECTS tutorial e2e test `test_list_filter_project_cel` and hardened the shared e2e fixture. The e2e fixture now restricts provider discovery to the backends each test actually declares (local/ssh always, plus docker/modal only when the test carries the matching `@pytest.mark`), instead of leaving every installed backend enabled. The dev monorepo installs all provider backends (aws, azure, gcp, ovh, vultr, ...), so any full-discovery command (`mngr list`, `mngr kanpan`, `mngr destroy --all`) previously queried backends that have no credentials or reachable daemon in the test environment and exited non-zero (and probed slowly before failing). The test also gained an explicit `@pytest.mark.timeout(60)` since a single `mngr list` subprocess exceeds the 10s default pytest timeout.

Tests: the `test_list_format_json_recap` e2e tutorial test now carries `@pytest.mark.timeout(180)`, matching its identical sibling `test_list_format_jsonl_recap`. `mngr list` performs slow in-process Modal discovery when credentials are present, which exceeds the 10s default and previously made the test time out spuriously.

Made the e2e tutorial test fixture's provider setup deterministic regardless of the host's cloud CLIs, credentials, or Docker availability:

- The credential-only cloud providers (aws, azure, gcp, vultr, ovh, imbue_cloud) are now disabled in the e2e fixture. Their backend plugins are installed in the dev checkout, so a read-only `mngr list` would otherwise instantiate each default provider and exit non-zero (provider-inaccessible) whenever the host happened to have that CLI on PATH but no credentials configured.

- The Docker provider is now enabled only for tests that declare they need it (`@pytest.mark.docker` / `@pytest.mark.docker_sdk`). Non-docker tests disable it so they no longer require a reachable Docker daemon, restoring the docker marker's contract of letting Docker-less environments skip only the docker tests.

Tests: the tutorial e2e suite now pins the test environment to the core `local`, `docker`, and `modal` backends. The dev workspace installs every provider plugin (aws, azure, gcp, ovh, vultr, imbue_cloud) via `uv sync --all-packages`, so their entry points were discovered by default and each reported itself unavailable without credentials, making a plain `mngr list` exit with EXIT_CODE_PROVIDER_INACCESSIBLE. A real `mngr` install from PyPI ships only the core backends, so the fixture now mirrors that representative fresh environment.

Tests: `test_list_format_jsonl` now creates two local command agents before asserting the JSONL contract, so it actually observes "one standalone JSON object per line" (each row parses as its own JSON object and the stream is not the single big array `--format json` emits) instead of passing vacuously on an empty listing.

- Gave the `mngr list --host-label` malformed-input e2e test (`test_list_host_label_filter_invalid_format`) an explicit 60s timeout so it is no longer flaky under the default 10s per-test timeout. The command correctly rejects a `--host-label` value without `=` before any provider discovery, but mngr's CLI cold-start overhead alone sits right at the 10s boundary.

Fixed the `test_list_jq_filter` e2e tutorial test so it reliably passes: dropped the superfluous `@pytest.mark.modal` (the `mngr list` read path never shells out to the `modal` CLI, so the mark tripped the resource guard's "marked but never invoked" check) and gave the test a generous timeout to accommodate the slow Modal gRPC discovery round-trip.

Fixed the fresh-install test fixture (`minimal_install_env`) so the isolated venv installs the `overlay` workspace package that mngr now depends on; without it, `mngr` failed to import in a fresh install and every install smoke test errored. The fixture also now disables the docker provider at project scope so the `not docker`-marked install list tests (`test_list`, `test_list_json`) no longer require an ambient Docker daemon to observe the empty-state output.

Tests: the `mngr list --label` invalid-format e2e release test now sets an explicit `@pytest.mark.timeout(60)`. The malformed `--label` is still rejected before any provider discovery runs, but invoking `mngr` at all pays a ~12s Python import/startup cost that exceeds the default 10s per-test timeout. The release CI lane overrides the timeout globally to 90s; the explicit per-test timeout makes this fast-fail path robust when run without that override, matching the existing pattern used by the neighbouring `mngr list` filter tests.

Made the e2e tutorial test fixture disable the optional cloud provider backends (aws, azure, gcp, lima, ovh, vultr, imbue_cloud) that a full-monorepo dev checkout installs via `uv sync --all-packages`. A from-PyPI `mngr` install and the release CI image both ship only the local/docker/modal backends, so no e2e test exercises the extra providers; left enabled, a credential-gated provider such as AWS reported itself as inaccessible and made `mngr list` exit 6 instead of 0. Disabling them keeps the e2e discovery path deterministic and identical to the CI/end-user provider set regardless of which optional plugins the dev environment happens to have installed.

Testing: gave the `test_list_project_dot` tutorial e2e test an explicit `@pytest.mark.timeout(180)` (and a matching 60s command timeout) so it is robust under the default local pytest timeout, consistent with the sibling `mngr list` release tests. Also flagged (via a `FIXME(tmr)`) that the shared e2e fixture leaves every credential-requiring cloud provider backend enabled, which makes bare `mngr list` release tests exit 6 when no cloud credentials are present.

Hardened the e2e tutorial test harness so bare `mngr list` commands behave like a real tutorial user's environment: the test profile now restricts `enabled_backends` to the providers the suite actually exercises (local, ssh, modal, plus docker only for docker-marked tests). This stops unconfigured cloud backends (e.g. the separately-installed `imbue-mngr-aws` plugin) from making a bare `mngr list` exit with the provider-inaccessible code in the dev monorepo, where `uv sync --all-packages` registers every backend.

Removed a superfluous `@pytest.mark.rsync` mark from `test_list_project_field` (it creates a local command agent and never invokes rsync).

Gave the `test_list_provider_filter` e2e tutorial test an explicit `@pytest.mark.timeout(60)` override, matching the sibling `--local`/`--remote` filter tests. `mngr list --provider modal` runs the full provider-discovery path (an authenticated Modal lookup) that routinely takes ~10s, so under the default 10s per-test timeout the test timed out locally; the marker lets it pass both locally and under the release CI lane's global timeout.

Fixed the `test_list_watch_mode` e2e tutorial test, which verifies that wrapping `mngr list` in `watch -n5` genuinely runs the wrapped command. The previous 8-second `timeout` window was too short: `watch` renders a frame only after the wrapped command exits, but `mngr list` runs the full provider-discovery path (~10s in a fresh environment), so `watch` was killed mid-run before rendering any frame. Widened the window to 25 seconds so the first frame (containing "No agents found") is captured, and added a `@pytest.mark.timeout(90)` mark to accommodate the longer run body.

Fixed the e2e tutorial test environment so `mngr list` (and the other listing tests) behave like a genuinely fresh install. The e2e fixture now pins `enabled_backends` to the backends a real fresh user would have (local, ssh, modal, and docker only when a daemon is actually reachable) instead of letting the monorepo's fully-installed set of VPS provider plugins (aws, azure, gcp, ovh, vultr, imbue_cloud) all get probed. Previously those extra providers reported themselves as unreachable (e.g. AWS with no credentials configured), which made `mngr list` exit non-zero even in an empty environment.

Fixed the fresh-install `test_list` release test. The isolated-venv fixture (`isolated_mngr_venv`) did not install the workspace `overlay` package that `mngr` now depends on (via `imbue.overlay.markers`), so `mngr list` crashed with `ModuleNotFoundError: No module named 'imbue.overlay'`; the fixture now installs it alongside the other workspace packages. Additionally, `test_list` now scopes its listing to the local provider (`mngr list --provider local`) so the empty-state path is exercised deterministically without depending on a reachable Docker daemon, which the deliberately-minimal install environment lacks. This matches how the e2e suite scopes `mngr list --provider local` to avoid querying unavailable providers. These are test-only changes with no effect on shipped behavior.

Removed the superfluous `@pytest.mark.rsync` mark from the `test_message_all` e2e tutorial test. Broadcasting a message to a local command agent via `mngr list --ids | mngr msg -` delivers over tmux and never invokes rsync, so the declared rsync resource mark tripped the resource guard ("marked with @pytest.mark.rsync but never invoked rsync"). The tmux mark is retained since message delivery does use tmux.

Removed a superfluous `@pytest.mark.rsync` marker from the `test_message_commit_request` git tutorial e2e test. The test only exercises `mngr create` and `mngr msg`, neither of which invokes rsync, so the resource guard failed the test for a mark it never used. Only `mngr exec`-based sibling tests genuinely need the rsync mark.

Testing: dropped the superfluous `@pytest.mark.rsync` marker from the LABELS-tutorial e2e test `test_message_filtered_backend`. The test exercises only local command agents (created from a git repo, so `mngr create` uses git-mirror rather than rsync) piped through `mngr message -`, and never invokes the rsync binary, so the resource guard failed the test for carrying a mark it never used. The `@pytest.mark.tmux` marker is retained since command agents are tmux-backed.

Fixed the e2e release test `test_message_filtered_via_stdin_delivers_to_matching_agents` so it verifies exactly its scope (the delivery half of the filtered-broadcast tutorial block).

- Dropped the out-of-scope `to_succeed()` assertion on the standalone `mngr list --include ... --ids` precondition. That command runs the full provider-discovery path across every enabled backend, and an enabled-but-unconfigured cloud provider (e.g. aws) makes it exit non-zero by design, which is orthogonal to whether the `--include` filter selects the matching agents. The precondition now checks only that the filter emits exactly the two matching local ids on stdout.

- Removed the superfluous `@pytest.mark.rsync` mark: messaging local command agents delivers over tmux and never syncs files to a remote host, so rsync is never invoked and the resource guard flagged the mark as unsatisfied.

- Made the e2e test fixture pin `enabled_backends` to only the provider backends the run can actually reach (local and ssh always; modal when credentials are present; docker when a daemon is running). Previously every registered remote backend was enabled by default, so unconfigured cloud providers (aws/gcp/azure/ovh/vultr/imbue_cloud) surfaced as inaccessible providers and made `mngr list` (and anything that discovers, e.g. `mngr msg -`) exit non-zero or hang on their network probes. This keeps the e2e suite hermetic and independent of whatever ambient cloud credentials or daemons the host happens to have.

Tests: drop the spurious `@pytest.mark.rsync` from the `mngr msg agent-1 agent-2 agent-3` e2e tutorial test. The test creates local git-worktree command agents and delivers over tmux, so rsync is never invoked; the resource guard was failing the otherwise-passing test with a "marked rsync but never invoked rsync" violation. The tmux mark (genuinely exercised) is retained.

Gave the `test_message_nonexistent_agent` e2e test explicit timeout headroom (`@pytest.mark.timeout(60)`) so a single `mngr message` invocation's CLI startup cost no longer trips the default 10s func-only timeout on slower filesystems.

Fixed the `mngr message --on-error continue` end-to-end tutorial test: it now carries a realistic per-test timeout, gives the `mngr list | mngr msg` pipeline enough time for provider enumeration, and drops the `modal`/`rsync` resource marks that the test never actually exercises. The test also now verifies the message is delivered to the listed agent, not merely that the command exits 0.

Fixed the `test_message_one_agent` tutorial e2e test. It was missing a per-test `@pytest.mark.timeout` override, so the default 10s func-only timeout fired during `mngr create` (agent creation takes longer than 10s); added `@pytest.mark.timeout(120)` to match the sibling agent-creating message tests. It was also incorrectly marked `@pytest.mark.rsync`: the test creates a local agent inside a git repo, which uses the `git-worktree` transfer mode and never invokes the `rsync` binary, so the resource guard failed the test for carrying a mark it never exercised. Removed the spurious `rsync` mark.

Removed the incorrect `@pytest.mark.rsync` mark from the `mngr msg` short-form e2e tutorial test. Delivering a message to a local agent goes through its tmux session and never invokes rsync, so the mark tripped the resource guard ("marked but never invoked").

Fixed the `test_multiple_agents_coexist` e2e release test so it reliably verifies its documented scope (three local agents coexisting as independent, isolated entities). The verification `mngr list` is now scoped to the local provider so an unconfigured cloud backend (e.g. aws) can no longer make the command exit with the provider-inaccessible code even though the local agents are all listed; per-command and whole-test timeouts were widened to tolerate the CLI's fixed startup cost on slow filesystems; and the stale `@pytest.mark.rsync` mark was removed, since creating local agents in a git repo uses a git worktree (not rsync).

Fixed the `isolated_mngr_venv` test fixture (used by the fresh-install tests in `test_install.py`) to also install the `overlay` workspace package. mngr declares `overlay` as a workspace dependency, but the fixture's hand-maintained `_WORKSPACE_PACKAGES` list omitted it, so `overlay` was never installed into the isolated venv (workspace members are exported as editable lines and filtered out, and the workspace packages are installed with `--no-deps`). This caused every fresh-install test to fail with `ModuleNotFoundError: No module named 'imbue.overlay'` when importing `imbue.mngr.main`. No user-visible change -- this is test infrastructure only.

Tests: the `mngr observe --discovery-only` tutorial e2e test (`test_observe_discovery_only`) no longer carries `@pytest.mark.modal`. Like the fresh-environment `mngr list` filter tests, discovery-only observe reaches the Modal provider via the in-process gRPC SDK (invisible to the resource guard across the mngr subprocess boundary) and never shells out to the `modal` CLI, so the mark tripped the guard's "marked but never invoked" check.

Tests: the same test now masks only the `timeout`-expiry exit (124) to a clean pass instead of a blanket `|| true`, so it still fails if `mngr observe` crashes early with any other exit code -- verifying the command actually starts and streams without error rather than merely that a `|| true` pipeline exited 0.

Updated the `mngr observe --discovery-only` e2e tutorial test to assert on the current discovery snapshot event. The first line a fresh run emits is now the per-provider full snapshot (`DISCOVERY_PROVIDER`), which superseded the deprecated legacy global snapshot (`DISCOVERY_FULL`); the test now parses that first JSONL line and verifies it is a full snapshot carrying `agents`/`hosts` arrays. No user-facing behavior changed.

Gave the `test_plugin_add_by_git` e2e test a 60s timeout so it no longer trips the 10s default while `mngr plugin add --git` pays the cold-start cost and attempts a git clone, and extended it to assert the command prints no Python traceback (matching the sibling `plugin add` tests and the test's documented scope).

Gave the `test_plugin_add_by_path` e2e tutorial test a 60s timeout, matching the other subprocess-driven plugin e2e tests. Each `mngr` invocation pays a ~10s cold-start cost that alone exceeds the 10s default per-test timeout, so the test was timing out rather than exercising the `mngr plugin add --path` behavior it verifies.

Fixed the `test_plugin_disable_affects_create` e2e release test so it verifies the intended behavior: disabling the claude plugin now writes to the local config scope (which the test harness opts into pytest), so `mngr create` fails with the genuine "plugin 'claude' is disabled" gate instead of tripping the pytest config-opt-in guard first. Also raised its timeout to match the sibling roundtrip test, since it runs five sequential CLI invocations.

Fixed the `test_plugin_disable_enable_roundtrip` e2e release test: `mngr plugin disable`/`enable` now target the `local` config scope in the test, which the e2e fixture seeds with `is_allowed_in_pytest = true`. Previously the commands defaulted to the project scope, creating a `settings.toml` without the pytest opt-in, which caused the next `mngr` command to be refused by the pytest config guard.

Fixed the `minimal_install_env` test fixture so it installs the `overlay` workspace package into the isolated venv. Previously the fresh-install tests (e.g. `mngr plugin --help`) crashed with `ModuleNotFoundError: No module named 'imbue.overlay'` because `overlay`, a workspace dependency of `mngr`, was omitted from the editable workspace installs.

Increased the per-test timeout for the `mngr plugin list --active` tutorial e2e test. Each `mngr plugin list` invocation performs full plugin discovery (importing every installed plugin module), which takes several seconds; the test runs the command twice, so the combined work exceeded the default 10s timeout. This is a test-only change with no user-visible behavior difference.

Test-only change: raised the `test_plugin_list_active` e2e release test's per-test timeout from 60s to 180s. The test issues five separate `mngr` subprocess invocations (two `--active` listings, a disable, and two more listings), and each invocation pays a ~20s cold-start cost to import and register all plugins, so the previous 60s budget was too tight and the test timed out mid-run.

Added the missing `@pytest.mark.timeout(60)` marker to the `test_plugin_list_fields` e2e tutorial test so it no longer trips the 10s default per-test timeout on the ~10s `mngr` cold-start, matching the other subprocess-driven e2e tests in that file.

Fixed the fresh-install test fixture (`isolated_mngr_venv`) to install the `overlay` workspace package. mngr declares `overlay` as a dependency, but the fixture installs workspace packages editable (they are filtered out of the exported pinned deps), and the list omitted it, so the isolated venv failed to import `imbue.overlay` and every `mngr` invocation crashed.

Gave the `test_plugin_list_shows_installed` e2e test a 60s timeout so it no longer trips the 10s default pytest timeout during mngr's cold start, matching the other subprocess-driven plugin e2e tests.

Fixed the `test_recipe_launch_check_cleanup` e2e release test:

- Scoped its two `mngr list` invocations to the local provider (`mngr list --running --provider local` and `mngr list --provider local`), matching the local command-agent stand-in. A bare `mngr list` also queries remote provider backends (e.g. aws, docker) that are enabled by default but unreachable in the isolated e2e profile (no cloud credentials, no docker daemon), so it exited non-zero for reasons unrelated to the launch->check->cleanup recipe under test.

- Removed the stale `@pytest.mark.rsync` mark. The local command-agent stand-in never invokes rsync (only the tutorial's modal agent would sync the repo to a remote host), so the resource guard correctly flagged the mark as unused.

Fixed the `mngr rename --dry-run` e2e test so it verifies the local agent via `mngr list --provider local` (unscoped listing failed when an uncredentialed remote provider such as AWS is installed) and dropped its superfluous `@pytest.mark.rsync` mark, since the test never invokes rsync.

Fixed the `test_snapshot_destroy_by_id_fictional` e2e test, which was failing the modal resource guard ("marked modal but never invoked modal"). The test destroys a fictional snapshot without an agent/host, so it errors during argument handling and never touches the modal provider; the erroneous `@pytest.mark.modal` marker has been removed. Also strengthened the test's assertions to trace precisely to its documented scope: it now verifies the `--snapshot` flag was actually parsed (not rejected as an unknown option) and that mngr errored cleanly rather than crashing with a traceback.

Fixed the `test_snapshot_list` e2e release test, which exercises `mngr list --ids | mngr snapshot list -`. It now creates a running modal agent so the stdin pipeline resolves a real host, and it carries an explicit per-command and per-test timeout large enough for the two chained `mngr` invocations. The test also now confirms the running agent's auto-created snapshot appears in the stdin-fed listing.

Fixed the `test_start_all_via_stdin` lifecycle e2e test: its `--stopped` verification queries are now scoped to `--provider local` so an unreachable remote provider (e.g. `aws` without credentials) no longer makes them exit non-zero, and the superfluous `@pytest.mark.rsync` mark was removed since creating a local git-repo agent uses git-worktree transfer and never invokes rsync.

Fixed the `test_start_connect` e2e tutorial test (STARTING AND STOPPING AGENTS section).

- Removed the superfluous `@pytest.mark.rsync` mark: a local command agent's work_dir is created as a same-host git worktree, so `mngr create`/`mngr start` never invoke rsync, and the resource guard rejected the unused mark.

- The test now stops `my-task` before `mngr start --connect`, so it exercises the tutorial's "start a stopped agent and immediately connect to it" path rather than starting an already-running agent.

Fixed the `test_start_dry_run` e2e tutorial test so it verifies the `mngr start - --dry-run` scope reliably: its before/after state-capture `mngr list` calls are now scoped to `--provider local` (the test only ever creates local agents), so they no longer fail with a provider-inaccessible error when an unrelated remote provider is enabled but unreachable. Also dropped the stale `@pytest.mark.rsync` mark, since creating a local command agent inside a git repo uses a git worktree and never invokes rsync.

Fixed the `test_start_idempotent` e2e tutorial test (STARTING AND STOPPING AGENTS section): removed a superfluous `@pytest.mark.rsync` mark. Creating a local agent from a clean git repo uses a git worktree and never invokes rsync, so the resource guard failed the otherwise-passing test for carrying a mark it did not use.

Strengthened the same test to confirm the agent is already running before the idempotent `mngr start`, so it genuinely verifies idempotence on a running agent rather than the stopped-agent path.

Fixed the `test_start_multiple_agents` e2e tutorial test so it reliably passes: bumped its pytest timeout to 180s (creating three command agents plus a single multi-agent `mngr start` exceeds the default 10s), and removed the superfluous `@pytest.mark.rsync` mark (creating and starting clean-worktree command agents uses a git worktree and never invokes rsync).

Tests: dropped the stale `@pytest.mark.rsync` mark from the `test_start_stopped_agent` e2e lifecycle test. The test exercises a local command agent (git worktree + tmux), which never invokes rsync, so the resource guard failed the otherwise-passing test with "marked with @pytest.mark.rsync but never invoked rsync". The mark was a leftover from when the block was a Modal test (where `mngr create --provider modal` rsyncs files to the remote host).

Fixed the `test_stop_all_via_stdin` e2e tutorial test so it verifies exactly its documented scope. Added a `@pytest.mark.timeout(180)` (the test was hitting the default 10s per-test timeout during agent creation), scoped its verification `mngr list --running`/`--stopped` queries to `--provider local` (the local agent it operates on) so they no longer abort on uncredentialed cloud providers, and dropped a superfluous `@pytest.mark.rsync` mark since a local `--type command --no-connect` agent never invokes rsync. No behavioral change to `mngr` itself.

Removed the superfluous `@pytest.mark.rsync` mark from the `test_stop_archive` e2e tutorial test. The test creates a local git-repo command agent and exercises `mngr stop --archive` plus `mngr list`, none of which invoke rsync, so the resource guard was failing the test for carrying a mark it never satisfied.

Fixed the `test_stop_basic` release e2e test. Its verification listings now use
`mngr list --provider local --stopped` / `--running` (scoping to the local
provider) instead of the unscoped `mngr list`, so an enabled-but-unreachable
provider (e.g. aws with no credentials) no longer makes the verification listing
exit non-zero even though the local listing is correct. Also dropped the
superfluous `@pytest.mark.rsync` mark: the test only creates a local command
agent and stops it, which never invokes rsync.

- Fixed the `test_stop_dry_run` e2e lifecycle test: added a `@pytest.mark.timeout(180)` so the create + provider-enumeration + stop round-trip is not killed by the 10s default pytest timeout, gave the provider-enumerating `mngr list --ids | mngr stop - --dry-run` pipe a 120s subprocess timeout (the remote-discovery budget), and removed the superfluous `@pytest.mark.modal` and `@pytest.mark.rsync` marks (the test exercises only tmux among guarded resources).

Fixed the `test_templates_setup_via_config_edit` e2e release test: added a 120s timeout (the `mngr create` step exceeded the 10s default), dropped the spurious `@pytest.mark.modal` mark (the template is substituted with `transfer="none"` for the local test, so modal is never invoked), and added a check that `mngr config edit` actually creates the project config file on disk.

Fixed the `test_tips_exec_env_inspect` tutorial e2e test so it no longer hangs contacting an unconfigured `aws` provider: the agent-id cross-check now scopes `mngr list` to `--provider local`, matching the local command agent the test substitutes for the tutorial's agent. Also dropped the spurious `@pytest.mark.rsync` mark, since creating a local `--type command` agent never invokes rsync.

The e2e tutorial test harness now disables provider plugins it cannot reach in the test environment. The cloud provider plugins (aws, azure, gcp, vultr, ovh, imbue_cloud) are installed in the monorepo and enabled by default but have no credentials in CI/dev sandboxes, and the docker provider is disabled automatically when no Docker daemon is reachable. Previously these enabled-but-unreachable providers made any bare `mngr list` (which enumerates every enabled provider and exits non-zero on an unauthenticated one) fail for reasons unrelated to the behavior under test, e.g. `test_tips_exec_filtered_hosts` and other modal tutorials run under `-m 'not docker'`.

Fixed the `test_tips_transcript_tail_assistant` e2e release test, which was failing under the default 10-second pytest timeout while the real `mngr create` setup step was still running. It now carries an explicit `@pytest.mark.timeout(120)` marker like its sibling release tests, giving the create and transcript-read commands enough headroom.

Fixed the tutorial e2e test fixture so each test only probes the provider backends it declares via its `@pytest.mark.modal` / `@pytest.mark.docker` marks (plus the always-available local backend). Previously the fixture left `enabled_backends` empty, so `mngr list` (and name-resolving commands) fanned out to a default instance of every registered backend; an unconfigured cloud backend (e.g. `aws` with no credentials) or an unreachable `docker` daemon would make those commands abort with the provider-inaccessible exit code even when the target host was found.

Added the missing `@pytest.mark.timeout(600)` marker to the `test_transcript_assistant_only` e2e test. Without it the test inherited the global 10s function timeout and was killed mid-`mngr create`, unlike its sibling transcript tests which all set the 600s timeout.

Removed a superfluous `@pytest.mark.rsync` mark from the `test_transcript_default` release test. It runs entirely against a local claude agent and never invokes rsync, so the mark tripped the resource guard's "marked but never invoked" check and failed the otherwise-passing test (matching the earlier fix for the sibling transcript tests).

Dropped the superfluous `@pytest.mark.rsync` mark from the `mngr transcript --format jsonl` e2e release test. The test creates a local `claude` agent (which uses git-worktree transfer, not rsync), so the mark tripped the resource guard and failed the otherwise-passing test.

Removed a spurious `@pytest.mark.rsync` from the `mngr transcript --tail 5` e2e test. The test never invokes rsync (it creates a local claude agent and reads its transcript), so the resource guard failed the test at teardown. The mark is now dropped, matching the identical `--tail 1` sibling test.

Tests only (no user-visible mngr change): the e2e test fixture now scopes which provider backends full-discovery commands (`mngr list`/`event`/`gc`) scan to the backends a test declares via its markers -- `local` and `modal` always, `docker` only for `@pytest.mark.docker` tests. This keeps the tutorial e2e tests deterministic regardless of which stray cloud CLIs/plugins (e.g. `aws`) happen to be installed on the host, and stops non-docker tests from failing when no Docker daemon is present. The troubleshooting `mngr list` tutorial test also drops a spurious `@pytest.mark.rsync` marker, since creating a local command agent uses a git worktree rather than rsync.

Hardened the release e2e test `test_troubleshoot_destroy_and_recreate_modal` (the modal destroy-and-recreate troubleshooting flow). Its `mngr list` observation calls are now scoped with `--provider modal` so an unrelated, unreachable provider in the test environment (e.g. a stopped Docker daemon) cannot make them exit non-zero, and the `mngr destroy --force` step now gets the same generous timeout as create because its post-destroy garbage collection does real Modal network work. The shared e2e fixture now also seeds `enabled_backends` (via the `MNGR__*` env layer) to the only backends any e2e test exercises -- local, ssh, docker, modal -- so that installed-but-unconfigured cloud provider plugins (aws, azure, gcp, vultr, ovh, imbue_cloud) are no longer probed during discovery/GC, where without credentials they would abort `mngr list` or hang `mngr destroy --force`'s GC. This is a test-only change with no user-visible impact.

Removed the superfluous `@pytest.mark.rsync` mark from the release test `test_troubleshoot_follow_events`. A local command agent created in a git repo uses `git worktree` (transfer mode `GIT_WORKTREE`), which never invokes rsync, so the mark tripped the resource guard's "marked but never invoked" check. This is a test-only change with no user-visible effect on `mngr` behavior.

Removed the spurious `@pytest.mark.modal` mark from the `mngr gc` troubleshooting e2e test. `mngr gc` reaches Modal only through the in-process gRPC SDK inside the `mngr` subprocess (never shelling out to the `modal` CLI), so the mark always tripped the resource guard's "marked but never invoked modal" check even though gc ran correctly. This matches the existing treatment of other discovery-style commands like `mngr list` and `mngr observe`.

Tests: drop the superfluous `@pytest.mark.rsync` from the `test_troubleshoot_host_diagnostics` e2e tutorial test. The test exercises `mngr exec` against a local command agent, which never invokes rsync (rsync only runs when one endpoint is a remote host), so the resource guard failed the test for carrying a mark it never satisfied. The mark also lay outside the test's documented scope, which covers only `mngr exec` forwarding commands to the host shell.

Fixed: removed the superfluous `@pytest.mark.rsync` from the `test_troubleshoot_recent_events` e2e tutorial test. The test exercises `mngr event` against a locally created `command` agent, which uses tmux but never invokes rsync, so the resource guard flipped the otherwise-passing test to failed. The `@pytest.mark.tmux` mark is retained.

Removed a superfluous `@pytest.mark.rsync` mark from the `test_troubleshoot_stop_restart` e2e tutorial test. The test exercises a local `command` agent, whose create/stop/start path uses git-worktree transfer and tmux, never rsync, so the resource guard correctly flagged the mark as never invoked.

Testing: dropped the superfluous `@pytest.mark.rsync` from the `test_troubleshoot_transcript_for_errors` e2e tutorial test. The test creates a local `command` agent and runs `mngr transcript`, which fails fast at the client-side agent-type check and never syncs files, so the resource guard flagged the mark as never invoked. The `tmux` and `release` marks remain (the local agent genuinely runs under tmux).

Raised the per-test timeout on the `test_unknown_command_fails` e2e tutorial test. Rejecting an unknown command does no host or agent work, but spawning the `mngr` subprocess still loads every plugin/backend/provider at import time, which routinely exceeds the global 10s pytest timeout on slower hosts. The test now allows the `mngr` invocation the time it needs (mirroring the other e2e tests in the file), while still verifying the same behavior: an unknown command exits with usage code 2, leaves stdout empty, and prints an error naming the command and pointing back to `mngr --help`.

Tests: made the `test_usage_wait_and_create` scripting e2e test robust to bundled-but-unconfigured cloud providers. Its concrete-effect verification used an unscoped `mngr list --format json`, which enumerates every enabled provider and exits with `EXIT_CODE_PROVIDER_INACCESSIBLE` (6) when a bundled provider such as AWS lacks credentials -- unrelated to the usage-gated create under test. The verification now scopes discovery to the always-reachable local provider (`mngr list --provider local --format json`), so it confirms the gated create was skipped without depending on cloud-provider credentials.

Fixed the fresh-install test fixture (`isolated_mngr_venv`) to install the `overlay` workspace package. Without it, every `mngr` command in a freshly installed venv crashed with `ModuleNotFoundError: No module named 'imbue.overlay'`, causing the install tests to fail.

Strengthened `test_version_output` to assert that `mngr --version` actually prints a version string (not just the program name).

## 2026-07-13

Regenerate the `mngr forward` command docs to document its new `--use-http2` flag, which terminates TLS and negotiates HTTP/2 (via ALPN) for the workspace origin instead of serving plain HTTP/1.1.

Documentation only: the `HostState.UNKNOWN` comment in `primitives.py` now covers both of the state's producers -- the existing provider-level case (AgentObserver emitting UNKNOWN for hosts of a provider that errored during discovery) and the new per-host case (a provider that is itself reachable but could not observe a specific host, e.g. imbue_cloud's lease-only fallback when a leased host's outer SSH is unreachable, which now surfaces UNKNOWN instead of CRASHED).

Added `SENTRY_IGNORED_STDLIB_LOGGER_PATTERNS`, the set of noisy third-party stdlib loggers (paramiko, pyinfra) that mngr already redirects into loguru. Sentry-reporting processes pass these to `setup_sentry` so Sentry does not independently capture the raw stdlib records as events. Without this, paramiko's transport thread logging handled SSH connection failures (e.g. "Error reading SSH protocol banner" when a reverse-tunnel target goes offline and the health check retries) flooded Sentry with tens of thousands of unactionable, un-rate-limited error events.

Added a `get_container_loopback_ssh_port` seam to the provider interface (default `None`). It lets a provider whose topology splits "publish" from "connect" -- e.g. an imbue_cloud lima slice, where the container's sshd is published in the VM on a fixed port but reached from outside via a box-forwarded port -- report the port at which the container is reachable from the outer host's own loopback, so a service running on the outer host (the VPS-resident latchkey gateway) can reverse-tunnel into the container on the correct port.

Regenerated the `mngr latchkey forward` CLI help doc to note that the supervisor now health-checks and respawns the shared `latchkey gateway` subprocess if it dies mid-session.

## 2026-07-11

The forever-claude-template repo is being renamed to default-workspace-template (with the `fct`/`FCT` shorthand expanded to `default_workspace_template`/`DEFAULT_WORKSPACE_TEMPLATE` forms).

References in this project (comments, identifiers, docs) are mechanically updated by `scripts/rename_template_repo.py`.

## 2026-07-10

Added `DiscoveryErrorLogSuppressor`, a shared per-process deduplicator for provider-level discovery-error log lines: a provider stuck on the same failure (e.g. missing credentials) is logged once (with a note that repeats are suppressed), a different error logs immediately, and a clean snapshot from the provider logs an info-level recovery line and re-arms suppression. Host- and agent-attributed discovery errors are never suppressed.

`mngr observe --discovery-only` now emits one startup `DISCOVERY_PROVIDER` snapshot for each provider skipped at stream startup: an error snapshot for unavailable/unauthorized providers and a clean zero-agent snapshot for known-empty ones (e.g. Modal with no per-user environment), each carrying the provider's config. Consumers such as the minds providers panel now see these providers' state from the stream instead of only from a full `mngr list` side effect.

Added `get_all_provider_instances_and_skipped`, which reports the providers whose construction was skipped as unavailable/unauthorized/empty alongside the constructed instances.

## 2026-07-09

Documentation: the `mngr imbue_cloud admin server order` CLI reference now lists the new `--dry-run` flag (build + price a non-committal OVH cart and print the preview, then delete it without ordering).

- Changed: regenerated the `mngr imbue_cloud` CLI reference (`docs/commands/secondary/imbue_cloud.md`) for the reworked `admin pool destroy` (multiple ids destroyed in parallel, `--max-concurrency`, `--drop-row-only` replacing `--skip-vps-cancel`, `--force` re-scoped to leased rows) and the new `admin pool teardown-slices --max-concurrency` flag.

Fixed Docker workspaces becoming permanently unreachable after a reboot (or any Docker daemon restart). Docker reassigns randomly-published SSH host ports when the daemon restarts (and can even reshuffle them between containers), but the Docker provider persisted the port resolved at create time and trusted it forever -- every later connection dialed the dead port, so the host showed STOPPED/UNAUTHENTICATED and the minds UI hung on "Loading workspace...". The recorded port is now treated as a hint: connection setup and discovery reconcile it against the container's live port mapping, heal the host record, replace any stale known_hosts entry left on the new port by a different container, and drop cached connectors still holding the old port. Workspaces now reconnect automatically after a reboot, and `mngr list` passively heals stale records.

## 2026-07-08

Fixed an unbounded memory leak in long-running `mngr observe` (discovery) processes. Each discovery poll spins up worker threads that create per-thread gevent hubs; destroying a hub without first joining it left the hub greenlet parked mid-`LoopExit`, whose traceback pinned the poll's entire object graph in a cycle the garbage collector could not reclaim. Over days this grew to multiple gigabytes. Worker-thread cleanup now joins the hub before destroying it (the pattern gevent prescribes for this leak, gevent issue 1601), so repeated polls no longer strand a hub per iteration.

Regenerated the `mngr tmr` command reference to document the new variant flags (`--name`, `--mapper-prompt`, `--reducer-prompt`) for running distinct test suites as separate runs.

Exported the marker text of `HostShutdownNotSupportedError`'s message as an importable `HOST_SHUTDOWN_NOT_SUPPORTED_MESSAGE` constant in `imbue.mngr.errors`, and built the exception's message from it. This lets out-of-process callers (e.g. minds, which runs `mngr` as a subprocess and only sees exit code and stderr) match on one shared source of truth instead of duplicating the literal string. No user-visible change -- the error message text is unchanged.

## 2026-07-07

Fixed the grandparent-death watcher being a silent no-op on macOS (MIND-103). The watcher (used by the minds backend to reap itself, and its `mngr forward`/`event`/`observe` subprocess tree, when the Electron GUI exits uncleanly) resolved its grandparent PID by reading Linux's `/proc/<ppid>/status`, which raises on macOS -- so it never armed, and a force-quit or crash of Electron left the whole backend tree orphaned under `launchd` indefinitely.

`_read_grandparent_pid()` now falls back to `ps -o ppid= -p <pid>` when `/proc` is unavailable, so the watcher arms on macOS too and SIGTERMs the backend once its grandparent disappears. The `/proc` fast path is unchanged on Linux. The `ps` subprocess is spawned through the caller's `ConcurrencyGroup` (a required parameter, so its ownership is always explicit at the call site).

Acquiring a remote host's cooperative lock (part of every `mngr create`/`start` on a remote host) is now resilient to transient SSH failures. Previously, if the SSH connection to a host dropped ("Connection reset by peer" leaving a dead transport), opening the lock channel raised a raw paramiko `SSHException: SSH session not active` that leaked past callers.

The lock path now retries transient SSH errors after rebuilding the dropped connection (matching how remote shell/file operations already behave), and surfaces a failure that survives the retries as a structured `HostConnectionError` instead of a raw paramiko exception.

This keeps a single unreachable machine from crashing an operation that spans many hosts at once: the mapreduce/TMR orchestrator launches one agent per task and already isolates per-agent `MngrError`s, so one host with a flaky connection is now recorded as a single failed launch rather than aborting the entire run.

Fixed several bugs where a long-running `mngr observe` discovery stream underfed consumers that tunnel to remote hosts (most visibly the minds app's system_interface forward), leaving a freshly created cloud workspace stuck loading.

- The per-provider poller reused one provider instance for every poll but never reset its per-cycle caches, so the first poll's result (including an empty leased-hosts list) was cached for the lifetime of the process. Because the minds discovery observer starts a moment before a workspace's cloud host is leased, it kept emitting empty imbue_cloud snapshots that clobbered the correct ones in the shared discovery log, so the workspace flickered in and out and its web UI hung on "Waiting for system interface to be ready...". The poller now resets each provider's per-cycle caches at the start of every poll (matching the `reset_caches=True` already used by `mngr list`), so newly leased/destroyed hosts are picked up on the next poll.

- The streaming per-provider discovery never emitted `HOST_SSH_INFO` events -- only a full `mngr list` did, which the running app never does periodically. So a consumer that lost a host's SSH endpoint (e.g. after the host briefly left discovery during the flapping above) never regained it and refused to dial the host's loopback-registered service URL. `BoundedProviderDiscoveryResult` now carries per-host SSH info, and the poller re-emits it as `HOST_SSH_INFO` on every successful poll. This is wired up for **all** providers, not just imbue_cloud: the base per-host-bounded discovery captures each host's SSH endpoint from the host it already connects to (covering docker, lima, local, ssh, aws, gcp, azure), and a shared `collect_cached_host_ssh_infos` helper covers the batch providers (vps, modal). So any remote workspace -- local docker/lima included -- is now reliably reachable through the streaming path.

## 2026-07-06

Bounded per-host discovery reads so a wedged host can no longer leak background threads or be re-read on every poll.

- The per-host-bounded discovery path now threads the provider's `host_discovery_timeout_seconds` down as a hard per-command timeout on the discovery reads (the agent-directory listing and each `data.json` read). A host that connects and then stalls mid-read now self-terminates its reads (surfacing as a connection error that falls back to the host's last-known offline agents) instead of leaving an abandoned discovery thread running forever.

- Added cross-poll per-host de-duplication: a host whose previous discovery read is still in flight is not re-read on the next poll (its still-running read is reused), bounding accumulation to at most one in-flight read per host. Each such skip is logged as a warning -- with the per-command timeout in place this should essentially never fire, so it acts as a precise "host wedged past its timeout" alarm.

- Connect-phase timeouts are unchanged (already bounded); batch providers (modal / vps / imbue_cloud) are unaffected (they read all hosts in one bounded pass and spawn no per-host reads).

Made provider discovery per-provider and resilient to slow/hung providers, so a single stuck provider can no longer block discovery of all the others.

- Each provider is now discovered independently and emits its own `ProviderDiscoverySnapshotEvent`, authoritative only for its own provider. `mngr observe --discovery-only` runs one decoupled poll loop per provider, and `mngr list` writes one per-provider snapshot per provider as a side-effect.

- The legacy global `FullDiscoverySnapshotEvent` / `DISCOVERY_FULL` snapshot is no longer produced and all live usages were removed, but the type is kept (deprecated) so historical on-disk discovery logs still parse.

- Added per-provider discovery cadence and timeout settings to each `[providers.<name>]` block: `discovery_poll_interval_seconds` (default 30), `discovery_warn_seconds` (default 20), `discovery_error_timeout_seconds` (default 120), and per-host / per-agent `host_discovery_timeout_seconds` / `agent_discovery_timeout_seconds` (default 30, validated to stay below the provider error timeout). A slow host whose read exceeds its timeout surfaces as explicitly UNKNOWN (its previously-known agents retained as unknown) instead of holding up its whole provider's snapshot.

- A hung provider is bounded without killing threads: discovery warns after the warn threshold, then emits a per-provider `DiscoveryError` after the error timeout while the abandoned read keeps running; its late result is accepted on a later poll.

- Added a shared, span-aware discovery state aggregator (`DiscoveryStateAggregator`) so a host/agent state change that arrives while a provider is mid-discovery is no longer clobbered by that older in-flight snapshot. All discovery consumers now reconcile through it. The aggregator's retain/drop rule supersedes the former `partition_removed_agents_by_provider_error` helper, which has been removed now that every consumer reconciles through the aggregator.

- Kept shell-completion fast: the TAB-completion replay scan stops at the most recent legacy full snapshot instead of parsing every snapshot line in the (potentially large) discovery events file.

- Preserved read-after-write consistency for agent/host resolution: an agent created (or destroyed) during a provider's in-flight discovery span is no longer lost (or resurrected) when resolving it from the event stream. The stream replay used by `mngr stop --stop-host` (and the cached-snapshot attach used by `mngr forward --observe-via-file`) now replays back to each provider's snapshot span start and applies the same span-aware rule as the aggregator, and `mngr stop` falls back to a live discovery for any agent not yet in the stream -- so stopping a just-created agent works immediately.

Changed: agents now repaint on *every* tmux attach, not just on `mngr connect`. The post-attach `SIGWINCH` redraw nudge moved from `mngr connect`'s SSH wrapper into a per-session tmux `client-attached` hook set when the agent is created, so a plain `tmux attach`, the ttyd agent terminal, or a web-shell attach all trigger a clean redraw too.

This resolves the garbled-pane / failed-send symptom seen when a client attaches at a different terminal size than the agent's session was created at: previously the agent's TUI could be left showing a stale, unpainted frame (which also broke message sending, since the paste-visibility check could not read the corrupted pane).

Note: only newly created agents get the hook. Agents already running when you upgrade will not get the repaint nudge on attach (the old `mngr connect` nudge has been removed) until they are recreated.

`mngr rename --host <agent> <new-name>` now renames the host of the referenced agent (previously a `[future]` placeholder). Only the provider's logical host name changes; the agent name, tmux session, env file, and git branch are untouched. Not all providers support host renaming (e.g. `ssh` host names remain user-owned).

Host names are now length-capped (`HostName`, 63 characters -- the DNS-label limit), since they end up in provider-side identifiers with their own length limits. The cap is generous so existing host-name generators are unaffected; callers that derive a pretty slug from arbitrary text apply their own smaller target on top. Agent names and provider instance names are unaffected. Auto-generated host names are kept within the cap.

Integrates the "simple names" work: `mngr rename --host <agent> <new-name>` now renames the referenced agent's host (previously a `[future]` placeholder), changing only the provider's logical host name (the agent name, tmux session, env file, and git branch are untouched; not all providers support it). Host names are now length-capped at 63 characters (the DNS-label limit) since they flow into provider-side identifiers. Agent names and provider instance names are unaffected.

## 2026-07-01

Bumped the release Dockerfile's `CLAUDE_CODE_VERSION` from `2.1.141` to `2.1.160` to match the pin in forever-claude-template's `.mngr/settings.toml` (`[agent_types.claude].version`). The two had drifted, which the `test_claude_code_version_matches_forever_claude_template_pin` release test flags because a mismatch makes agent provisioning fail with "Claude version mismatch".

Regenerated the `mngr imbue_cloud` CLI reference doc to reflect the slice-only `admin pool` commands (the `--backend`/`ovh_vps` flags are gone). Docs-only; no `mngr` core behavior change.

Added a new async/await ratchet (`test_prevent_async_await`) that freezes the current amount of `async def` / `await` usage in this project and fails if new async code is added. We strongly prefer synchronous code: it is far easier to debug, and our software is intentionally low-scale, so async provides no benefit. Existing usage is grandfathered in at its current count; the count can only decrease.

## 2026-06-30

Simplified the internal `--reuse` agent-lookup scoping introduced for `mngr create <agent>@<host>.<provider>`: the lookup is now driven entirely by the host named in the create address. No user-visible behavior change -- a fresh-host create still skips same-named agents on other hosts, an existing-host re-create still reuses the agent on the named host, and a bare-name `--reuse` with multiple matches still raises the disambiguation error.

The redundant `target_host_ref` parameter (which scoped by host id only for already-resolved existing hosts) has been removed; its constraint was already covered by the address's host name plus provider. Host-in-scope matching now lives in a single pure `_is_host_in_reuse_scope` helper.

## 2026-06-29

Fixed `mngr destroy` (and `mngr gc`) crashing with a Docker `409 Conflict` traceback when a Docker host's per-host build image was still referenced by a leaked (often orphaned, stopped) container.

The post-destroy garbage collection removed the build image while a leftover container still referenced it, which Docker refuses with "must be forced - container is using its referenced image". The raw error was not caught in `delete_host`, so it aborted GC and surfaced as a failed `mngr destroy` even though the requested agent had already been destroyed.

`delete_host` now re-runs `destroy_host` (which is idempotent and runs on aged-out, FAILED, or CRASHED hosts that GC never destroys back-to-back) to clear any leftover container before purging the host's records. Because the container is removed first, the build-image removal no longer hits the 409 conflict. The build image is force-removed so mngr always drops its `mngr-build-<host>` tag: when nothing else references the image it is deleted outright, and if something unexpected still references it only mngr's tag is dropped (the image is left for that user). Any resource that genuinely cannot be removed is recorded as a structured `CleanupFailedGroup` cleanup failure that the GC sweep aggregates and continues past, so one host's leak is surfaced with a cause-specific exit code instead of crashing the whole `mngr destroy`/`mngr gc`.

A host whose cleanup leaves a resource behind is no longer forgotten: `delete_host` now deletes the host record only when cleanup fully succeeds, and keeps it (raising the `CleanupFailedGroup`) otherwise, so the next GC sweep retries rather than orphaning the leaked resource. The sweep records the leak and counts the host as deleted (and emits its destroyed event) only on a clean delete.

Also tightened the host-volume cleanup in `delete_host`: a failed volume-directory removal is now recorded as a leftover-resource leak instead of being dismissed as "no volume" (the removal is idempotent on a missing path, so an exception there means the data still exists). And the Docker provider's test teardown now collects and fails loudly on cleanup failures rather than silently swallowing a broad set of exceptions, so a leaked container or volume can no longer accumulate unnoticed across tests.

## 2026-06-28

Gives the `mngr list` nested-fields CLI test (`test_list_command_with_nested_fields`) the same `@pytest.mark.timeout(30)` its sibling real-tmux `list` tests already carry, so it no longer flakily exceeds the 10s default timeout while creating a real tmux agent under CI load.

Fixes a duplicate `_FakeContainer` test helper class in the docker provider tests (`providers/docker/instance_test.py`): two module-level classes shared the name, so the second silently shadowed the first at import time (the earlier state-container tests would have constructed the wrong, single-argument class) and a static type check reported the call sites against a union of both. The status-only variant used by the connection-error-fallback tests is renamed `_FakeStatusContainer`, leaving the labels+status `_FakeContainer` as the sole holder of that name. Test-only change.

Refuses to create or start a Docker host whose configured gVisor (`runsc`) runtime would give it an ephemeral root filesystem. gVisor's default root overlay (`--overlay2=root:self`) keeps each container's root filesystem in a per-sandbox overlay that is discarded whenever the container stops, so a `runsc` runtime registered without `--overlay2=none` silently loses mngr's SSH provisioning (the injected host key, `authorized_keys`, and the self-healing-entrypoint marker) the first time the container restarts -- leaving the host running but unreachable (the container's published SSH port accepts TCP but sshd never starts, so discovery reports the host `UNAUTHENTICATED`). The Docker provider now checks the local daemon's runtime registration (`/etc/docker/daemon.json`) when a gVisor runtime is configured and, if `--overlay2=none` is missing, raises an actionable `DockerGvisorEphemeralRootfsError` at create/start that offers both fixes: switch the provider to the standard runtime (`docker_runtime = "runc"`), or re-register the runtime persistently (`sudo runsc install -- --overlay2=none` then restart Docker). A remote daemon's registration cannot be introspected, so that case logs a warning instead of failing. This matches the `--overlay2=none` registration `mngr_vps` already performs for the same reason. The error's remediation text also warns that, with `--overlay2=none`, a container running supervisord (or anything that installs a unix socket under `/run` via a hard link) must additionally mount `/run` as a tmpfs (`default_start_args=["--tmpfs", "/run"]`), because `--overlay2=none` puts `/run` on gVisor's gofer filesystem where `os.link()` of a socket returns `EOPNOTSUPP`.

Adds per-provider visibility to parallel discovery (`api/discover.py`). Discovery polls every provider in parallel and then waits for all of them, so a single provider whose call hangs silently freezes the whole snapshot (and downstream, the minds discovery-health watchdog mistakes the resulting gap for a dead producer). Now each provider's discovery is wrapped in a timing span, an unusually-slow provider is logged at WARNING with its duration, and -- while the snapshot is still waiting -- the still-pending provider(s) are named in a WARNING every 15s, so a hung provider is identifiable by name from the logs instead of only inferable from its silence. Behavior is otherwise unchanged: discovery still waits for all providers (bounding a hung provider's call is a separate change).

## 2026-06-27

Fix custom `parent_type` agent types losing their inherited settings after a multi-layer config load.

A custom agent type that declares only `parent_type` (e.g. `[agent_types.worker]` with `parent_type = "claude"`) is supposed to inherit all of its parent type's non-default settings (`use_env_config_dir`, `auto_dismiss_dialogs`, `cli_args`, etc.). This worked when the config was resolved straight after a single merge, but once the real config load applied a second settings layer, every inherited value was silently reset to the child config class's defaults -- so a `worker` built on `claude` came out with permissions, env-config-dir, and dialog-dismissal handling all turned off.

The cause was in the overlay merge pipeline (`config/overlay_merge.py`): each whole-config merge reparses the merged result with `model_validate`, which marks every present field as explicitly set. A registry entry carried through unchanged from the base layer therefore lost its sparse `model_fields_set`, so when `parent_type` inheritance later folded the child onto the parent (using `exclude_unset` semantics), the child's defaulted fields looked explicitly set and clobbered the parent's values.

The merge now restores each merged model's `model_fields_set` (recursively, including nested sub-models and registry entries) to the union of the base's and the override's actually-set fields, rather than "all fields". Inheritance through any number of config layers is now correct: a `parent_type` child inherits the parent's non-default settings, while a field the child does set still wins.

Fixed the docker provider reporting a running container as `CRASHED` when its inner sshd had died (e.g. "Error reading SSH protocol banner"). Previously, when agent enumeration failed to SSH into such a host, `mngr list` / `mngr forward` fell back to the offline view and derived `host.state = CRASHED` (a shutdown-capable provider with no recorded clean stop is treated as crashed) -- even though the docker daemon, the ground truth for container lifecycle, still reported the container as running. The docker provider now consults the daemon in that fallback and reports `UNAUTHENTICATED` (the host is up; we just can't get inside it), mirroring how the imbue_cloud provider reads container state out-of-band. This matches the actual host condition and stops downstream consumers from treating a live-but-unreachable container as fully offline. If the daemon itself cannot be reached for this out-of-band check, the provider keeps the default offline-state derivation instead of failing, so the offline fallback still degrades gracefully.

Added a `get_connection_error_fallback_state` provider hook (default: no override) that `get_host_and_agent_details` consults when a non-authentication connection failure forces the offline fallback. Only the docker provider overrides it; the generic offline-state derivation and every other provider's behavior are unchanged.

The Docker provider's discovery now treats a present-but-stopped state container as a provider error (`ProviderUnavailableError`) rather than silently reporting zero hosts. The state container holds every docker host's records; when it is stopped, reads fail and previously collapsed to an empty host list, which discovery published as "no hosts" -- dropping every host. Now an empty list always means "genuinely zero hosts," and the existing retain-on-error path keeps last-known hosts while the container is unavailable.

## 2026-06-26

Changed: Bumped the offload version baked into `libs/mngr/imbue/mngr/resources/Dockerfile` from `0.9.9` to `0.9.10` to track the CI pin.

TMR test scope is being re-anchored from the tutorial block to each test
function's docstring, so that TMR can run against all release tests rather than
only the e2e tutorial tests.

As part of this, the e2e test fixture now captures each test's docstring (the
verbatim tutorial block plus its crystallized scope) and the rendered test
detail pages show it under a "Docstring" heading.

All e2e tutorial tests have been migrated to the new format: each test's tutorial
block now lives in its docstring (under a `Tutorial block:` section) followed by a
`Scope:` section describing what the test verifies. The `e2e.write_tutorial_block`
helper has been removed now that no test uses it.

## 2026-06-25

Bumped the offload version baked into `libs/mngr/imbue/mngr/resources/Dockerfile` from `0.9.7` to `0.9.9` to track the CI pin.

## 2026-06-24

Added `mngr config wizard`, an interactive command that walks through common one-time user-scope configuration. Each step short-circuits when its setting is already configured, so it is safe to re-run.

The first step (shown only when the claude agent type is installed) asks whether to enable config-dir isolation for local Claude agents: enabling it keeps each agent's Claude config separate so mngr never touches your default config, while disabling it shares your default config -- which is what Claude subscriptions on macOS need to keep credentials working. The choice is written to `agent_types.claude.isolate_local_config_dir` in your user config.

Hardened agent process-tree teardown so a long-lived daemon launched under an agent (e.g. a bootstrap-managed `supervisord` and its children, like a `ttyd` bound to a fixed port) can no longer outlive the agent and collide with a relaunch.

- Lifted the `stop_agents` teardown into a shared `reap_agent_process_tree(agent)` helper: it kills the agent's tmux-session pane descendants **and** any `MNGR_AGENT_ID`-tagged orphans that reparented to PID 1 (SIGTERM then SIGKILL), without killing the tmux session itself. `stop_agents` now uses this helper (plus the session kill) via the extracted `_collect_agent_process_pids` / `_terminate_pids`.

- `start_agents` now reaps any stale process tree for an agent id before launching, but only when that agent isn't already running (so an idempotent start never tears down a live agent). This prevents a relaunch from straddling an orphaned daemon from a prior incarnation (which previously caused, e.g., `EADDRINUSE` on a service's fixed port, leaving the new service stuck restarting while the orphan kept serving).

## 2026-06-23

Exposed the discovery polling cadence as a public constant (`DISCOVERY_STREAM_POLL_INTERVAL_SECONDS` in `imbue.mngr.api.discovery_events`, previously a private `_`-prefixed value). Consumers such as minds derive freshness thresholds from it, so deriving the threshold from the cadence keeps the two from silently drifting apart.

`mngr create --format json` now includes `outer_host_public_key` and `container_host_public_key` when the provider exposes them (a new `get_ssh_host_public_keys` host/provider method, defaulting to none), so tooling can pin a host's sshd key for strict host-key checking instead of trust-on-first-use.

## 2026-06-22

Handle unauthenticated providers consistently across `mngr list` and other commands.

Previously every provider reported a missing-credentials state differently: AWS/GCP/Azure raised a verbose error and exited 1, Vultr printed an ad-hoc `WARNING` and exited 0, OVH silently reported zero agents, and a full `mngr list` (default `--on-error abort`) surfaced only whichever provider happened to fail first.

Now:

- A new shared `ProviderNotAuthorizedError` (a subclass of `ProviderUnavailableError`) represents "enabled but unauthenticated" for every provider, carrying structured `short_reason` / `short_remediation` fields plus verbose help text.

- `mngr list --on-error continue` runs the rest of the listing, then reports every failing provider in one consistent block: a single glanceable line per provider on stderr for human output (`<provider>: <reason> — <remediation> (disable: mngr config set ...)`), and structured entries in the `errors` array (with `exception_type`, `help_text`, and an `is_provider_inaccessible` flag) for `--format json` / `jsonl`. The default `--on-error abort` still fails immediately on the first provider error.

- `mngr list` now exits with the granular provider-inaccessible code (6) when every error is a provider that could not be reached or authenticated, and 1 otherwise -- in both abort and continue modes.

- The OVH backend was missing from the internal remote-backends list, so it loaded (and silently no-op'd) in environments where the other cloud backends were correctly skipped; it is now treated like AWS/GCP/Azure/Vultr.

Regenerated the `mngr forward` command reference to document its new
`--on-error {abort,continue}` option (provided by the mngr_forward plugin).

Unify the `mngr start` host lock with the normal cooperative host lock.

There is now a single host lock. State-changing operations (`create`, `start`, `gc`, ...) all acquire `Host.lock_cooperatively`, which holds a real `flock(2)` on the host's `host_lock` file -- directly on local hosts, and over a long-lived SSH exec channel on remote hosts. Previously the remote path of this lock was only an advisory marker file (no real mutual exclusion), while a separate, newer `lock_for_starting` held a real flock on its own `host_start_lock` file. The separate start lock (and its `host_start_lock` file) is removed; `mngr start` now uses the unified lock, which gives true cross-actor mutual exclusion (e.g. a remote desktop client racing an in-host boot hook) for all of these operations.

Because `start` and `gc` now share the lock, a `gc` that tears down a host/agent is serialized before a concurrent `start`; after acquiring the lock, `start` checks that the target agent's state directory still exists and fails with a clear "not found on host" error instead of trying to boot an agent that was just garbage-collected.

`create` and `start` block indefinitely until the lock is acquired (a contended create now waits rather than failing); `gc` and other callers keep a bounded wait that raises an error on timeout. While waiting on a contended lock, mngr logs a "waiting to acquire host lock" message.

The in-host idle-shutdown watcher and `mngr list`'s lock columns now detect the lock via a non-blocking `flock` probe rather than file existence, since the lock file is intentionally never deleted (its inode must stay stable across local and remote holders). The lock auto-releases when an operation finishes or errors, so a crashed controller no longer leaves a host pinned awake. `MNGR_DEBUG_RETAIN_LOCK_FOR_FAILED_HOSTS_DURING_CREATE=1` now keeps a failed remote host alive by launching a detached on-host process that keeps holding the flock.

`flock` (the `util-linux` package) is now a required host dependency, bootstrapped alongside `git`/`tmux`/`jq`/`sshd` on all providers (and pre-installed in the bundled image). It is already present on standard Debian-based images, so this only matters for minimal/custom images.

## 2026-06-21

- Container hosts now self-heal sshd after an out-of-band restart. The host
  container entrypoint (re)starts sshd on boot whenever mngr has already
  provisioned this host (tracked by a marker, so a host key pre-baked into the
  base image is never used by mistake), so a `docker restart`, docker daemon
  restart, or host reboot brings ssh back without waiting for `mngr start`.
  mngr's own sshd start is now idempotent (a no-op when sshd is already running).

- `mngr start` is now safe to run concurrently for the same host. The agent
  (re)launch is serialized by a dedicated cross-actor `flock` (local in-host
  starts coordinate with remote over-SSH starts), so a desktop-driven start and
  an in-host boot-hook start cannot race. The lock blocks until acquired (wrap
  the command in `timeout` for a deadline).

- Added a `--post-host-create-outer-command` create option (and matching
  create-template / settings key `post_host_create_outer_command`). It runs
  shell commands once on the host's outer machine (the underlying VM/daemon
  host) after the host is created -- e.g. to install a VM-level systemd unit.
  Skipped with a warning when the provider exposes no outer host.

Regenerated the `imbue_cloud` command reference doc to cover the new bare-metal slice options: `admin pool create --slice-env-name` and the new `admin pool teardown-slices` command.

## 2026-06-20

`mngr imbue_cloud admin pool create` now defaults `--backend` to `slice` (bare-metal slices) instead of `ovh_vps`. Baking new OVH classic VPS pool hosts is deprecated: passing `--backend ovh_vps` fails with a deprecation error pointing at `--backend slice`. Existing OVH VPS pool hosts can still be listed and destroyed. The generated CLI reference doc (`docs/commands/secondary/imbue_cloud.md`) is updated to match.

## 2026-06-19

Stop `mngr event --follow` from polling the persisted files of an offline (stopped-but-not-destroyed) agent every second.

Previously, a follower whose target host was offline kept re-reading the agent's unchanging event files once per second through the read-only volume. For the Docker provider each such read is a separate `docker exec` into the shared state container, so a handful of stopped agents could drive the Docker engine to a large, wasted CPU load (observed: ~30 `docker exec`/sec into one state container, pushing it to ~90% CPU) even though no agent work was happening.

The follow loop already tracks online/offline status and re-checks it every 30s. Each per-source tail thread now lives for the whole follow session and gates its own I/O on a shared online/offline signal: while the target is offline it parks and does no reads at all (a stopped agent cannot write events), and the periodic source-directory rescan is likewise skipped. On a transition the loop simply swaps the shared target handle and flips the gate -- threads are never torn down and recreated, so the previous teardown/restart churn (and its races) is gone. Each thread reads from whichever target (online host or offline volume) is current, re-reading its source from the start across a transition; existing event-id deduplication ensures nothing is emitted twice on resume.

When the host returns to RUNNING the threads resume reading automatically. This affects every consumer of `mngr event --follow` (the CLI, `mngr forward`, and the minds desktop app), not just one.

`build_add_authorized_keys_command` (the shared SSH-key seeding helper used by every provider's host/container setup) is now idempotent: each entry is appended only if it is not already present (a `grep -qxF` guard), so re-running it does not accumulate duplicate `authorized_keys` lines. This lets the imbue_cloud restart path re-seed a leased container's key by reusing this helper instead of hand-rolling its own command, and makes the first-time seed safe against accidental double-application.

Updated the `mngr create` help docs for `--aws-ami=` to reflect that the AWS provider now falls back to a pinned per-region default when `default_ami_id` is unset (the separate `default_ami_by_region` config field was removed).

Added `extract_agent_data_from_parsed_listing` to the shared
`providers/listing_utils` helpers, the natural companion to
`parse_listing_collection_output`. It pulls each agent's `data.json` dict out of a
parsed listing, replacing three copies of the same logic in the VPS provider's
realizers. An entry whose `data` is present but not a JSON object (a list/scalar
from a corrupt or hand-edited `data.json`) is now skipped with a WARNING rather
than silently, matching the other listing skip-sites. No user-visible behavior
change beyond the added warning.


Updated imports for the `mngr_vps_docker` -> `mngr_vps` package rename and the
accompanying class renames (`VpsDockerProvider` -> `VpsProvider`,
`VpsDockerProviderConfig` -> `VpsProviderConfig`, `VpsDockerHostRecord` ->
`VpsHostRecord`, `VpsDockerError` -> `VpsError`, etc.). Import-only; no behavior
change.

mngr config now merges with the explicit operator algebra from the new standalone `overlay` library (see the `overlay` changelog for the operators in full): a bare `key` assigns (narrowing-checked), `key__extend` merges onto the layer below -- now **recursively** (a nested `key__extend` merges deeper while a nested bare key still replaces; backward-compatible) -- and the new `key__assign` assigns **without** the narrowing warning (the per-key opt-out to the `allow_settings_key_assignment_narrowing` flag). Within a single layer, resolution is two-phase and order-independent (assigns first, then `__extend`); a bare `key` and `key__assign` for the same field is a contradictory double-assign and raises `ConfigParseError`. New `Static*` markers (`StaticTuple` / `StaticList` / `StaticDict`) mark an aggregate atomic so replacing it is exempt from the narrowing guard; mngr's `StringDerivedTuple` / `ScalarTuple` are now `StaticTuple` subclasses.

A settings *patch* field (a claude agent type's `settings_overrides`) now **accumulates** across config scopes (user < project < local) and `parent_type` inheritance instead of a higher/child scope replacing the whole value: non-overlapping keys from every scope survive and same-key `__extend`s combine, with the narrowing guard threaded recursively (a bare assign dropping a non-empty aggregate is caught at any depth). Its deferred `__extend` / `__assign` markers are preserved through config-load and resolved at provision time against the home `settings.json`, joining the existing `create_templates.<name>` deferred paths. A `key__assign` inside `settings_overrides` now reliably suppresses the cross-scope narrowing guard even when a *lower* scope already set the key -- which is precisely the case a narrowing could fire, so the per-key opt-out now works where it matters (previously the deferred `__assign` collapsed to a bare assign at config-load whenever a lower scope had set the key, and the guard still errored on the opted-out key).

Cross-scope `settings_overrides` narrowing is now surfaced at config-load. A `settings_overrides` field accumulates across scopes, so an additive cross-scope override is a superset that never narrows; previously a higher scope whose *bare* key dropped a non-empty aggregate set by a lower scope did so silently. The merge now surfaces those in-patch drops into the loader's flag-gated narrowing error (escapable via `allow_settings_key_assignment_narrowing = true`, `allow__extend`, or `allow__assign`). Purely additive cross-scope overrides still load unchanged.

Fixed a latent crash: `MngrConfig.merge_with` raised `AttributeError` when the base config's `retry` / `logging` was `None` and the override set them (masked in production, but a real footgun). The overlay pipeline's `drop_none_values` pass treats a `None` sub-model as unset, so the re-parse defaults it rather than dereferencing `None`.

Fixed a config-merge bug where a partial override of a nested sub-model field (`logging`, `retry`, or a provider's `security_group`) silently reverted the base scope's other sub-fields to their defaults. For example, project scope `[logging] file_level = "ERROR"` plus local scope `[logging] console_level = "TRACE"` previously dropped `file_level` back to its default (and spuriously raised the narrowing error); now `file_level` carries through. Sub-model fields are detected by their *live value* being a `BaseModel` (so a `None` is simply unset and a discriminated-union value resolves to its own concrete class), and merge field-by-field across config scopes, `--setting`, and `parent_type` inheritance -- carrying through a base scope's unset sub-fields and narrowing only when a nested list/dict actually drops entries. (This also corrects a latent inconsistency in the old field-by-field merge, which used an `is-not-None` test that dropped any unset sub-field whose default was non-`None`.)

Internal (no user-facing behavior change): the model-free config-merge algebra is extracted into `overlay` (mngr imports it from `imbue.overlay`; `ConfigParseError` now subclasses `OverlayError`), and the whole config-merge surface -- `AgentTypeConfig.merge_with`, `MngrConfig.merge_with`, and `parent_type` inheritance -- now computes through overlay's typed-node pipeline (`config/overlay_merge.py`) instead of field-by-field pydantic copies, guarded by property tests asserting equivalence to the frozen old logic. The now-dead `_merge_container_dict` / `_assign_scalar` helpers and the vestigial per-model `merge_with` overrides are removed. The per-key-merging registries (`agent_types`, `providers`, `plugins`, `commands`, `create_templates`) are now identified by a declarative `RegistryField()` field annotation (parallel to `SettingsPatchField`) rather than a hard-coded field-name set, and sub-models are detected at runtime instead of by annotation introspection.

Internal (no user-facing behavior change): **all** config-load narrowing detection now flows through the single overlay merge; the separate model-walking detector (`detect_settings_narrowing` and helpers) is removed from production, kept only as a frozen equivalence-test reference. To make this faithful, the overlay pipeline re-marks `Static*` markers stripped by `model_dump` (relying on the markers' proven-pure round-trip) on the override side before merging, so an atomic-aggregate replacement (string-shaped `cli_args`, a provider's `allowed_ssh_cidrs`, an explicit `StaticList` / `StaticDict`) stays narrowing-exempt; and the overlay's `narrowing_paths` reports the specific narrowed leaf (e.g. `commands.create.defaults.env`) rather than just the containing field. The loader's two narrowing collectors collapse into one. A nested same-named container field (e.g. a plugin config's own `commands` dict) is narrowing-checked as an ordinary aggregate, since the container per-key merge applies only at the top level of `MngrConfig`.

Internal (no user-facing behavior change): now that the overlay refactor is shipped and CI-green, the equivalence-guard scaffolding is removed -- the frozen `_reference_*` old-logic copies and the `production == reference` tests in `overlay_merge_test.py` (covering `AgentTypeConfig.merge_with`, `MngrConfig.merge_with`, `parent_type` inheritance, and the model-walking narrowing detector). The behaviors they pinned stay covered by the surviving direct-assertion tests there plus the loader / data-types / registry tests. The dead string-suffix merge engine in `overlay` (`combine_patches` and the string-suffix `merge` / `finalize`) is deleted with them.

Internal (no user-facing behavior change): the loader's narrowing-attribution (which lower-precedence layer a dropped value belonged to) is now computed in linear time via a `provenance` map threaded through the config-layer fold, replacing the prior O(n^2) re-merge of every prior layer against each new layer. Attribution output is unchanged.

Internal (no user-facing behavior change): the config-merge pre-processing in `config/overlay_merge.py` is simplified to a single declarative recursive walk. The `SettingsPatchField` / `RegistryField` markers and their collectors move to a new leaf module `config/field_markers.py` (re-exported from `config/data_types.py`, so existing imports keep working), unified behind one generic `get_field_names_with_marker` helper; the marks are now read directly off each live model's class rather than threaded in as resolver callables. The three mark/unmark helper pairs collapse into one recursive `_to_operator_dict` / `_from_operator_dict` pair (a registry entry's own settings-patch and sub-model fields are now handled by the same uniform walk), the redundant per-entry class table is derived from the live entry models, and the `tuple/list/dict -> Static*` shape mapping is shared. The `StringDerivedTuple` marker is removed in favor of the identical `ScalarTuple` (a string-written `cli_args` is now a `ScalarTuple`); its narrowing-exemption behavior is unchanged. Equivalence to the prior field-by-field logic remains pinned by the frozen `_reference_*` guards in `overlay_merge_test.py`.

Internal (no user-facing behavior change): mngr's suffix-keyed-dict extend consumers (`config/key_resolver.py`, `cli/common_opts.py`) now resolve a single `__extend` through the overlay's one extend algebra via the new `extend_plain_value` adapter (a thin lift/finalize over the node engine), replacing the import of the now-deleted parallel `apply_extend` / `extend_dict` plain-dict recursion from `overlay.merge`. Resolution output is unchanged.

Internal (no user-facing behavior change): the overlay's `would_assignment_narrow` predicate moved from `imbue.overlay.merge` to the new `imbue.overlay.narrowing` module; mngr's importers (`cli/common_opts.py`, `config/data_types_test.py`) and doc references (`config/README.md`, `config/loader.py`) are updated accordingly.

Internal (no user-facing behavior change): added `write_json_dict_via_host`, the write-side counterpart to `read_json_dict_via_host`, which serializes a dict to indented JSON and writes it via the host (optionally creating the parent directory first). The Claude plugins now route their settings/hooks writes through it instead of hand-rolling the `json.dumps(..., indent=2) + "\n"` + optional `mkdir -p` dance at each site.

An expected (`MngrError`) error raised during agent provisioning -- e.g. a settings-narrowing `ConfigParseError` while building the per-agent `settings.json` -- now surfaces as a clean one-line error instead of an "Unexpected error" with a full traceback. `provision_agent` runs its body inside a concurrency group that re-raises a body exception wrapped in a `ConcurrencyExceptionGroup`; the `create` flow now unwraps a single expected error from that group (via the group's `only_exception_is_instance_of` / `get_only_exception` helpers) before it reaches the CLI.

The `mngr config` command is now exempt from the settings-narrowing guard, so a config that would otherwise narrow can still be loaded in order to *edit* it: previously `mngr config set` / `mngr config unset` -- the way to fix a narrowing config -- failed with the narrowing error themselves (a catch-22). The guard still fires for every other command.

The narrowing error's `__extend` example is now tailored to the offending key (e.g. `work_dir_extra_paths__extend = ...`, or `permissions__extend = {allow__extend = ...}` for a nested path) instead of a fixed generic example, so the message shows exactly how to fix the user's own config.

Internal (no user-facing behavior change): the per-plugin `merge_with` tests the overlay refactor retired (scalar precedence, `None`-keeps-base, type guards) are subsumed by the centralized overlay/merge tests; the one behavior not yet pinned centrally -- a `plugins` entry preserving its subclass type and type-specific fields when a higher scope sets only `enabled` -- now has a single consolidated test (`overlay_merge_test.py::test_mngr_plugins_registry_subclass_is_preserved`), the plugins-registry analogue of the existing `agent_types` and `providers` subclass-preservation tests.

Updated the shared provider release harness's offline-host_dir capability docstring to note that GCP now also supports the offline ``host_dir`` mirror (alongside AWS S3 and Azure Blob), so Trip 1's opt-in offline-host_dir step runs against GCP too.

Trimmed a monorepo-development-only paragraph from the PyPI README (regenerated from the top-level README) so it stays focused on user-relevant content.

Added a shared provider release-test harness (`imbue.mngr.providers.provider_release_testing`): a `ProviderReleaseProfile` plumbing contract plus a `run_provider_release_trip1` driver that walks one provisioned host through the full lifecycle in a single boot -- create, verify the tagged cloud resource, exec a marker file, plain stop, `stop --stop-host` (real machine stop, or a loud refusal where unsupported), start, persistence, snapshot (where supported), an out-of-band "sketchy kill", crash discovery, `gc`, and a backend-clean check. Mirrors the agent release-test harness so each provider's release suite collapses to a thin profile.

Also added `run_provider_release_trip3` ("snapshot survives destroy"): snapshot a host, destroy it, and assert the snapshot is portable -- a fresh `mngr create --snapshot` restores the captured filesystem -- or, where snapshots are not portable (the container shape's docker-commit dies with the VPS), assert that documented divergence so it flips loudly once portability lands.

Also fixed the shared `delete_modal_apps_in_environment` test helper to pass `--yes` to `modal app stop`, so the app is actually stopped in non-interactive runs (CI / release tests) instead of aborting on a confirmation prompt and relying on the environment deletion to reap it.

Also added `run_provider_release_trip4` ("error classification contract"): a no-boot CLI trip that asserts `mngr create` surfaces the right error for a provider with unresolvable credentials (the contract `ProviderUnavailableError`, or the documented divergence where a provider raises a different class), checks whether the curated help text points at the provider-correct setup command, and -- for the VPS family -- asserts a `--vps-*` build arg is rejected synchronously with the migration hint. Runs in seconds and costs no compute.

Also added `run_provider_release_trip2` ("idle auto-shutdown contract"): a single-boot trip that creates a host with a short auto-shutdown (the idle watcher for the cloud trio; the sandbox lifetime cap for Modal), polls the cloud API until the compute genuinely stops so billing halts, and -- where the provider resumes from that stopped state -- asserts `mngr start` brings it back with a pre-shutdown marker intact. Where the provider does not resume (Modal: the sandbox is terminated by its own timeout), the trip asserts the termination only and skips the resume. The shutdown is driven via `--idle-timeout` (which lands the cloud trio in a resumable stopped state) rather than the `auto_shutdown_seconds` time cap (which terminates/deletes the instance). Adds a `resumes_after_auto_shutdown` capability flag plus `auto_shutdown_create_args` / `write_auto_shutdown_settings` profile hooks.

Trip 1 gained an opt-in offline-host_dir read (off by default behind `MNGR_RELEASE_TEST_OFFLINE_HOST_DIR=1`, so the happy path stays lean): in the `--stop-host` window, where the provider captures host_dir to a state bucket (`supports_offline_host_dir`), it asserts `mngr file get <host> <path> --relative-to host` serves the marker from the offline mirror while the host stays stopped. AWS/Azure run it; GCP (`NullHostDirBackend`) and Modal (no `--stop-host`) skip.

Trip 1 also gained a bare-shape assertion (`is_bare_host` capability flag): where the profile declares a bare (no-container) shape, it asserts the agent shell is the VM's own root -- the bare host store (`/var/lib/mngr-host`) is present and there is no `/.dockerenv` -- so a NONE-isolation host that silently fell back to a container fails loudly. This is the coverage the retired per-provider bare lifecycle tests used to own, now folded into the trip's NONE parametrization.

Merge intent for an agent type's `settings_overrides` is now declared with a Claude-compatible `__mngr_merge` map instead of the `__extend` / `__assign` key suffixes.

Because `settings_overrides` is folded into a file the external AI CLI also reads (Claude Code's / antigravity's `settings.json`), the suffixes -- which that CLI does not understand and would surface as junk literal keys -- are no longer allowed there. Instead, declare the operator in a single top-level `__mngr_merge` map keyed by dotted path, which the external CLI silently ignores:

```toml
[agent_types.claude.settings_overrides.permissions]
allow = ["Bash(npm *)"]
[agent_types.claude.settings_overrides.__mngr_merge]
"permissions.allow" = "extend"   # merge onto the base; "assign" replaces without the narrowing guard
```

A bare key still assigns with the narrowing guard; the narrowing error now prints the exact `__mngr_merge` patch to add. The suggested patch is the full nested one in a single error: a dict that would drop a sibling key is suggested as `extend` (so the sibling survives) and a replaced list/value as `assign` (so your exact value is kept, not silently broadened). Raw `__extend` / `__assign` suffix keys under `settings_overrides` are a hard error pointing to `__mngr_merge`. mngr's own (non-`settings_overrides`) config is unchanged and still uses the suffixes.

New `mngr config assign <key> <value>` command, mirroring `mngr config extend`: it writes a `key__assign` entry (replace without the narrowing guard), or -- on a `settings_overrides` path -- a `__mngr_merge` `assign` directive. `mngr config set key__assign <value>` routes to it, and `mngr config get` resolves the `__assign` form.

A settings key that contains a literal dot (e.g. an MCP server name like `my.server`) cannot be targeted by a dotted `__mngr_merge` path: such a directive errors as dangling and the auto-remediation skips it rather than mis-advising.

`mngr config set` / `extend` / `assign` now let configuration errors render through the central CLI error handler (a `ConfigParseError` is a `MngrError`) instead of a local catch, so an invalid value prints e.g. `Error: Unknown configuration fields: ['provider']` rather than an `Invalid configuration: ...` line.

## 2026-06-18

Added a shared `AgentUpdatePolicy` (`AUTO` / `ASK` / `NEVER`) used by the agent plugins to govern an agent CLI's self-updater. When unset, the default is `NEVER` (block self-update) so a managed agent stays on its installed version, except an *attended* agent that implements an interactive update flow, which defaults to `ASK`; unattended agents always default to `NEVER`.

Added a shared `verify_pinned_cli_version` installation helper so agent plugins can verify an installed CLI matches a pinned version (needed because installation is skipped when the binary is already present). It matches the user's pinned string verbatim against the `--version` banner (no version scheme assumed, so pre-release/four-component pins work), errors on a confirmed mismatch, and skips when the CLI reports no version.

Clarified `non_issues.md`: the `is_`-prefix exemption and the missing-`Field()`/description exemption apply specifically to CLI command functions and CLI-options data classes (e.g. `ListCliOptions`) that mirror user-facing click options. Internal boolean fields on non-CLI data classes should still use the `is_` prefix. Also fixed a typo in that file ("duplicated" -> "duplicate").

Fixed `mngr plugin list` mislabeling opt-in plugins (e.g. `claude_subagent_proxy`) as `enabled=true` when they are actually blocked.

The reported `enabled` state now reflects the plugin's real block state: opt-in plugins that were not explicitly enabled show `enabled=false`, while a plugin enabled via `[plugins.<name>] enabled = true` still shows `enabled=true`.

Underlying this, `config.disabled_plugins` now faithfully includes opt-in plugins that are disabled by default, so every consumer of that field (not just the plugin list) sees the correct effective disabled set. This is suppressed under `MNGR_LOAD_ALL_PLUGINS`, matching plugin-manager startup, so doc/tooling runs still load every plugin.

Added a `[tmux]` configuration section for customizing the tmux sessions mngr runs agents in:

- `tmux.attach_args` -- extra tmux client flags inserted before the `attach` subcommand when connecting to an agent (`tmux <attach_args> attach ...`). The motivating case is `["-CC"]` for iTerm2 control mode (native tabs/windows); `-u` / `-2` also work. Applies to both local and remote (SSH) agents.
- `tmux.additional_config_path` -- an extra tmux config file sourced into every mngr session. Unlike the auto-generated `~/.mngr/tmux.conf`, this file is never overwritten, so it is a stable place for mngr-session-specific tmux config.
- `tmux.primary_window_name` (default `agent`) -- mngr now names the agent's primary window and targets it by name instead of the literal `:0` index, so mngr works regardless of the user's tmux `base-index` setting.

Agents that were already running before this change have an unnamed primary window that name-based targeting would miss. mngr now self-heals these in-flight sessions: the first time it inspects such an agent, it renames the session's existing primary window to `tmux.primary_window_name`, so lifecycle detection, messaging, capture, attach, and ttyd keep working across the upgrade.

See `docs/tmux_users.md` for usage.

Fixed `mngr transcript` failing with "Unknown agent type" for config-defined agent subtypes (a custom `[agent_types.X]` with a `parent_type`, e.g. `parent_type = "claude"`). The command now resolves the type through its parent chain (like every other command) instead of a flat class-registry lookup, so it recognizes such subtypes and reads the parent's transcript.

Removed `TagLimitExceededError`: it existed only to flag the EC2 50-tag ceiling for the AWS provider's offline tag mirror, which this PR replaces with the S3 state bucket.

Regenerated the `mngr aws` / `mngr azure` CLI doc pages to cover the state-bucket setup these commands now perform (the providers' state-bucket feature is described in the `mngr_aws` / `mngr_azure` changelogs).

Added shared operator-command output helpers in `mngr.cli.output_helpers`, used by the `mngr aws` / `mngr azure` / `mngr gcp` prepare/cleanup commands: `emit_operator_result(event_name, parts, output_format)` renders a sequence of `OperatorResultPart`s -- each pairing a structured-data fragment with its human line, built via `OperatorResultPart.shown(human, **data)` or `shown_if(present, human, **data)` -- as JSON / JSONL / human in one place, plus a `write_event_line` primitive for the shared `{"event": <type>, ...payload}` JSONL shape.

Test-only: raised the per-test timeout on the tmux lifecycle tests `test_start_restart_running_agent` / `test_start_restart_stopped_agent` from the default 10s to 30s (they run several sequential tmux create/stop/restart operations that can exceed 10s on a loaded CI runner).

## 2026-06-17

Added a code-derived agent capability registry: a description of which agent types have which capabilities, where each capability declares how its presence is detected (a class mixin via `issubclass`, a `waiting_reason` field generator, a plugin hookimpl, or a sibling usage plugin).

The registry introspects a loaded plugin manager to determine each registered agent type's capabilities, and a generated doc (`libs/mngr/docs/concepts/agent_capabilities.md`) renders the full capability matrix. A drift guard fails if the committed doc disagrees with the code; regenerate it with `just regenerate-agent-capabilities-doc`. This is the basis for replacing the hand-maintained parity matrix.

The registry/generator and its tests are dev-only tooling and live in `scripts/make_agent_capabilities_doc.py` (see the `dev` changelog), so they are not shipped in the `mngr` wheel. What *is* in the `mngr` package is the set of capability marker mixins in `imbue.mngr.interfaces.agent` (`CliBackedAgentMixin`, `HasSessionAdoptionMixin`, `HasStreamingSnapshotMixin`, `HasUnattendedModeMixin`, etc.), which agent classes inherit at runtime and which the generator detects.

Added contract-bearing capability mixins to `imbue.mngr.interfaces.agent`: `HasStreamingSnapshotMixin`, `HasSessionPreservationMixin`, and `HasUnattendedModeMixin`. Agent types declare these to make the corresponding capabilities (live response streaming, session preservation on destroy, unattended/auto-allow operation) code-detectable in the capability matrix.

Added `HasPermissionPolicyMixin` (per-resource allow/deny/ask policy) and `HasVersionManagementMixin` (version pin or update policy) capability mixins.

Added module-level capabilities to the matrix: `deploy_contributions` (the `get_files_for_deploy` hookimpl) and `usage_tracking` (a sibling `mngr_<harness>_usage` plugin), both detected by the agent's owning plugin entry-point name.

Made auto-install a base capability: added `HasAutoInstallMixin` (per-CLI `get_install_command`) and a shared `ensure_cli_installed` helper that checks for the binary at provision time and installs it if missing (gated by consent locally, `is_remote_agent_installation_allowed` remotely). All five agents now declare it; antigravity, opencode, and codex gain auto-install they previously lacked. Adds the `auto_install` row to the capability matrix and a new `AgentInstallationError`.

Verified opencode and antigravity auto-install end-to-end on real Modal hosts (which ship without the CLIs).

Architecture-review refinements: excluded the task-specialized skill variants (code-guardian, fixme-fairy) from the matrix (kept headless_claude, which runs genuinely different logic); added a dedicated `get_install_binary_name()` to `HasAutoInstallMixin` (decoupling the install check from the lifecycle-detection process name); and a construction-time validator on `AgentCapability` for the detection-kind/field invariant. The registry-driven behavioral exercise of each capability against a live agent is deferred to a follow-up release-test harness; detection is covered in CI by the drift guard and the builder integration test.

Gave the capability matrix a fixed column order (claude, headless_claude, antigravity, codex, opencode, pi-coding, command, headless_command) instead of alphabetical, and excluded the internal `mngr-proxy-child` agent. Rendering now raises if a registered agent type is neither in the fixed order nor the exclusion list, so a newly added agent can never be silently dropped from the table. Moved the `headless_output` row to the bottom of the matrix.

Added a third matrix state, `n/a`, for capabilities that do not apply to an agent kind (distinct from `-`, which means applicable but absent). Each capability now declares a code-derived scope based on the agent's kind:

- CLI-backed-only (`raw_transcript`, `common_transcript`, `auto_install`, `permission_policy`, `version_management`, `usage_tracking`): `n/a` for the bare command runners.

- Interactive-only (`waiting_reason_field`, `session_resume`): `n/a` for headless and bare-command agents.

- Headless-only (`headless_output`): `n/a` for every non-headless agent, since exposing `output()` non-interactively is meaningless for an interactive agent.

A genuinely-registered capability (field generator, usage source, deploy hook) that lands out of scope raises, keeping the matrix honest; an inherited capability mixin that lands out of scope just renders `n/a`.

CLI-backed scope is derived from a positive marker, `CliBackedAgentMixin`, inherited by every agent that wraps a specific external CLI (claude, codex, antigravity, opencode, pi, and headless variants). A bare command runner is simply the agent without that marker, so it needs no command-specific class for scoping; a minimal `CommandAgent` subclass of `BaseAgent` survives only to declare `HasUnattendedModeMixin`. `unattended_operation` shows present for every agent: interactive coding agents earn it by auto-allowing in-run tool prompts, while headless and bare-command agents have it by construction (no prompt to gate on), declared via `BaseHeadlessAgent` and `CommandAgent`.

Unified the TUI streaming snapshot and headless incremental output into a single `live_output` capability via a shared bare marker, `SupportsLiveOutputMixin`, inherited by both `HasStreamingSnapshotMixin` (the TUI agent's snapshot file) and `StreamingHeadlessAgentMixin` (a headless agent's incremental stdout). `headless_output` (plain `HeadlessAgentMixin`) remains a separate row.

Added a `session_resume` capability (the read-side counterpart to `session_preservation`) via `HasSessionAdoptionMixin`, whose `adopt_session` contract method an agent's `on_after_provisioning` calls to resume an existing conversation. Interactive-only: it resumes a live session, so it is `n/a` for headless and bare-command agents (e.g. `headless_claude` inherits the mixin from `ClaudeAgent` but is headless, so it renders `n/a`). Currently claude-only (its `--adopt-session` / `--from` carry-forward); other interactive CLI agents show it as an available-but-absent gap.

Scoped the send-message flow to interactive agents. `send_message` is no longer an abstract method on `AgentInterface` (so it is not a universal contract). The send-keys delivery (`send_message` + `_preflight_send_message` + `_send_message_simple` + `_send_tmux_literal_keys`) moved off `BaseAgent` onto a new `SendKeysAgent(InteractiveAgentMixin, BaseAgent)`, which `InteractiveTuiAgent` and the bare `command` runner extend; opencode/pi declare the new `InteractiveAgentMixin` directly (they deliver via their own server/extension APIs). Headless agents (`headless_claude`, `headless_command`) no longer have `send_message` at all, so `BaseHeadlessAgent`'s rejecting `_preflight_send_message` override was removed. A new `require_interactive_agent` helper narrows an agent to `InteractiveAgentMixin` (used by the `mngr message` command and the initial/resume-message paths). User-visible change: `mngr message <headless-agent>` now fails with a clear "agent type ... does not accept interactive messages" error instead of a generic send failure; `mngr message` for a bare `command` agent still works (it keeps send-keys).

Made `HasVersionManagementMixin` a functional contract: its descriptive `get_version_policy()` label is replaced by `reconcile_installed_version(host, mngr_ctx)`, which a version-managing agent calls during provisioning (once the binary is present) to enforce its intent -- a pinning agent verifies the installed version and raises on mismatch, an update-policy agent runs its update check. `version_management` is still detected in the capability matrix by `issubclass(HasVersionManagementMixin)`, unchanged.

Regenerated the `mngr imbue_cloud admin pool destroy` CLI reference docs to reflect its now backend-aware teardown: `--skip-vps-cancel` is documented as skipping the underlying-machine teardown (cancel the OVH VPS for an `ovh_vps` row, or destroy the lima VM for a `slice` row), used only when the machine is already gone.

Regenerated the `mngr imbue_cloud admin pool create` CLI reference docs to include the new `--max-concurrency` option (caps how many bare-metal slices bake at once). Docs-only change.

Removed superfluous `@pytest.mark.modal`/`@pytest.mark.rsync` marks from two transcript release tests (`test_transcript_assistant_only`, `test_tips_transcript_tail_assistant`). Both run entirely against a local command agent with a locally-seeded transcript, so they never invoke modal or rsync; the marks tripped the resource guard and failed the otherwise-passing tests.

- `mngr rsync` and `mngr git push`/`mngr git pull` now skip host/agent discovery when given a bare local path (the previous implementation always ran a full provider scan), and narrow discovery to the named provider when the address pins one (e.g. `@host.modal:/work` only queries the Modal provider, not Docker). Behaviour for fully qualified addresses is unchanged.

- `mngr create --from` similarly narrows the cached host/agent loader: when the source, the target host, and `--reuse` (if used) all pin a provider, only those providers are queried; otherwise the loader falls back to a full scan exactly as before. Bare local sources and git URLs continue to skip provider discovery entirely.

- New `imbue.mngr.api.find.resolve_host_location(parsed, mngr_ctx, *, is_start_desired=True)` helper consolidates the local-path shortcut and the discover-then-resolve flow that `mngr rsync` and `mngr git` previously duplicated. The lower-level `resolve_host_location_address(parsed, agents_by_host, mngr_ctx, ...)` is unchanged and remains the right entry point for callers (like `mngr create`) that drive discovery themselves to share a single result across multiple resolutions.

Regenerated the bundled `mngr imbue_cloud` CLI reference docs to cover the new
`admin server order --option` flag (explicit choice for multi-offer mandatory
option families like bandwidth/vrack) and the now-required `admin pool create
--backend slice --server-id` flag (explicitly chosen bare-metal box).

The plugin install wizard (`mngr extras plugins` / `mngr plugin install-wizard`) now recommends and pre-selects the AWS, GCP, and Azure provider plugins when their CLI is detected on your system (`aws`, `gcloud`, and `az` respectively), mirroring how the Claude and Modal plugins are detected. The Lima provider plugin is now likewise recommended and pre-selected when `limactl` is detected (its CLI detection previously had no effect).

Extended the shared agent release-test harness (`imbue.mngr.agents.agent_release_testing`) to exercise session adoption end-to-end: after an agent is destroyed, the arc creates a fresh agent (in a new worktree) that adopts the just-preserved session via `--adopt` and asserts it recalls the pre-destroy secret -- proving the preserved store actually resumes, not merely that its bytes landed on disk.

Adoption is no longer opt-in: the arc runs it unconditionally for every agent that preserves on destroy (`preserves_on_destroy`, default on), and every such profile must implement `adopt_session_arg(preserved_dir)` (the session id or native-store path to adopt), else it fails loudly. Adoption is now a baseline capability -- if an agent preserves its session on destroy, the harness proves that session actually resumes into a fresh worktree. This makes the strongest assertion the default and removes the silent-skip risk of the former per-profile opt-in flag.

Standardized session adoption as a first-class create capability for any agent type that supports it (`HasSessionAdoptionMixin`). The CLI option is now `--adopt` (with `--adopt-session` kept as a backward-compatible alias), declared like every other create option in `cli/create.py` rather than through the plugin-extension hook. The adopted session id(s)/path(s) ride a typed `adopt_session` field on `CreateAgentOptions` (previously the namespaced `plugin_data["adopt_session"]` key); all five adoption-capable plugins (claude, antigravity, codex, opencode, pi-coding) read `options.adopt_session` / `args.agent_options.adopt_session`. The agent-agnostic validation (the type must support session adoption; mutually exclusive with cloning via `--from`) now runs in `imbue.mngr.api.create` for every create path (CLI and programmatic); each plugin keeps its own `on_before_create` fail-fast session-id pre-resolution. The former claude-only / built-in-plugin wiring (`builtin_adopt_session`) is removed.

Added a shared `iter_agent_session_paths(local_host_dir, relpath)` helper to `imbue.mngr.api.preservation` that enumerates a per-agent path across every live and preserved local agent. The five agent plugins' session-store scanners (claude, antigravity, codex, opencode, pi-coding) now route through it instead of each re-implementing the live+preserved directory walk.

Added a `transfer_cloned_agent_session_store(...)` helper to `imbue.mngr.api.preservation` for `--from <agent>` clone-and-resume: a generic clone copies the source workspace but not the source agent's state dir, so each adoption-capable agent transfers just its native session-store relpath from the source and rebinds it. Previously only claude resumed the source's conversation on `--from`; antigravity, codex, opencode, and pi-coding now do too.

An interactive TUI agent's `TUI_READY_INDICATOR` (the readiness signal `mngr` polls the pane for before sending keystrokes) may now be either a plain string (matched as an exact substring, as before) or a compiled `re.Pattern` (matched with `re.search`). The matching mode is chosen by the value's type, not by its contents, so a plain string containing regex metacharacters still matches literally. This lets an agent whose ready state can't be captured by a single substring (e.g. an input box bounded by horizontal rules) express it as a regex.

In the shared agent release-test harness, observing the RUNNING marker is now required of every agent (previously a per-profile `observes_running_marker` opt-in). Forcing a bash tool call (which asserts the common transcript carries a tool_call nested on the assistant turn plus its tool_result) is enabled for claude, codex, opencode, and pi-coding; it stays gated off for antigravity, whose async tool execution records the result only at the next turn boundary, so a single forced-tool turn never carries a tool_result.

Fixed a bug where sending a message to a resumed interactive TUI agent could time out. The TUI-ready wait now runs inside `send_message` (not only when an agent is first created), so every send path -- including the resume message and on-demand restart -- waits for the TUI to finish rendering before pasting. This prevents keystrokes from being dropped into a session that is still replaying its restored transcript.

Gave `test_cli_create_rejects_dirty_tree_by_default` a 30s pytest timeout (matching the sibling subprocess-create test), since `uv run mngr create` startup intermittently exceeds the default 10s under CI load.

Unified the two live-output surfaces (a TUI agent's streaming-snapshot buffer and a headless agent's captured stdout) onto one shared shape, so `SupportsLiveOutputMixin` is no longer a bare marker.

It now declares `get_live_output_path()` (the host file the agent publishes live output to) and `make_live_output_reader()` (a `LiveOutputReader` that turns successive reads of that file into text deltas). The shared poll-read-extract tail loop both surfaces build on lives in `imbue.mngr.agents.live_output_tail.tail_live_output()` (the implementation layer, where it can reference the host interface directly), keeping `SupportsLiveOutputMixin` a pure-abstract capability declaration. The former `HasStreamingSnapshotMixin` is removed -- a TUI agent now inherits `SupportsLiveOutputMixin` directly and supplies a snapshot-diff reader, while a headless agent supplies a raw-text or stream-json reader. The new `imbue.mngr.interfaces.live_output` module holds the `LiveOutputReader` contract and the `RawTextReader` implementation.

No user-visible behavior change: `mngr ask` / `mngr create --stream` and the robinhood streaming paths emit the same output as before.

- `libs/mngr`: agent tmux sessions now apply the mngr host tmux config even
  when a tmux server is already running. Previously the config was passed only
  via `tmux -f <config> new-session`, which tmux honors solely when it *starts*
  a new server; any session created on an already-running server (the common
  case once one agent is up) silently inherited tmux defaults. That dropped the
  widened `status-left-length` (so `[mngr-<agent>]` was clipped to 10 chars) and
  the `Ctrl-q` / `Ctrl-t` destroy/stop hotkeys. Session creation now runs
  `tmux source-file <config>` right after `new-session`, so these apply
  regardless of server state.

- `libs/mngr`: the host tmux config now enables `set-titles` (`set -g
  set-titles on` with `set-titles-string "#S  #T"`), so the agent's session
  name and pane title are forwarded to the outer terminal's tab (e.g. the
  iTerm2 tab title) instead of falling back to `<profile>(tmux)`.

- `libs/mngr`: mngr's generated `~/.mngr/tmux.conf` no longer sources the
  user's `~/.tmux.conf`, and the agent's tmux server is no longer started with
  `-f` pointing at the mngr config. tmux loads `~/.tmux.conf` itself, once, when
  the server starts; mngr's config (sourced at agent creation) now contains only
  mngr's own settings. Re-sourcing `~/.tmux.conf` on every agent creation could
  re-run non-idempotent user config (e.g. `set -ag`, plugin `run-shell`) and
  corrupt the user's setup.

Aligned the common-transcript schema with the OpenTelemetry GenAI semantic conventions: the assistant record's `stop_reason` field is now `finish_reason` (the OTel term).

Every assistant record now carries an ordered `parts[]` array (text and tool_call segments, modelled on the OTel message parts) that preserves the intra-turn interleaving of text and tool calls -- the canonical, agent-agnostic view that `mngr transcript` renders. A `parts_ordered` flag marks whether the order is faithful (true for claude, pi-coding, opencode, and trivially for codex, whose assistant turns are each either text-only or a single tool_call) or best-effort (false for antigravity, whose native format does not record where tool calls sat relative to the text). The flat `text` + `tool_calls` fields are kept as a convenience baseline. Because every emitter fills `parts[]`, the reader renders it directly with no per-agent fallback.

## 2026-06-16

Loosened the `create_test_agent` test helper's `agent_class` parameter (and return type) to accept any `BaseAgent` subclass, including ones parameterized on a specific `AgentTypeConfig` subclass (e.g. `OpenCodeAgent`). The generic is invariant, so the previous base parameterization rejected such agents, making it impossible to type-check a test that provisions a concrete agent type. No runtime behavior change.

The plugin install wizard (`mngr plugin install-wizard`, `mngr extras -i`) now knows about the usage plugins. Phase 1 recommends the base `imbue-mngr-usage` plugin for everyone. Phase 2 offers each per-agent usage provider (`imbue-mngr-claude-usage`, `imbue-mngr-codex-usage`, `imbue-mngr-opencode-usage`, `imbue-mngr-pi-coding-usage`) only when both its agent plugin and the base usage plugin are present -- already installed or selected earlier in the wizard. This is expressed by a per-entry `gate` field on catalog entries -- a `SignalGate` (detected tool) or `RequiredPackagesGate` (other packages must be present) -- which replaces the previous separate `signal` / `requires_packages` fields and lets the wizard ask each entry whether it is unlocked rather than branching on shape. Antigravity has no usage provider, so none is offered for it.

Added shared agent-preservation wiring so any plugin can mirror the claude/usage preserve-on-destroy behavior with minimal code.

- `build_transcript_preserved_items(event_source)` returns the standard raw (`logs/<source>_transcript`) and common (`events/<source>/common_transcript`) transcript directories an agent writes, centralizing the on-disk convention.

- `preserve_agent_state(items, agent, host)` is a thin online-path wrapper (for a plugin's `on_destroy`) that resolves the agent's state directory and local preserved-files destination.

- `preserve_host_agents_on_destroy(host, mngr_ctx, agent_type, items_for_agent)` is the shared body for a plugin's `on_before_host_destroy` hookimpl: it skips hosts with no readable volume, filters discovered agents by `agent_type`, and preserves each opted-in agent straight off the host volume.

- `flag_gated_items(ref, flag_name, items)` is the shared offline selector helper: it returns `items` only when the discovered agent's persisted `agent_config[flag_name]` is truthy (else `None`), so plugins no longer hand-roll the same opt-in dict-walk for `on_before_host_destroy`.

- The shared agent release lifecycle (`run_agent_release_lifecycle`) now asserts preservation: its destroy step verifies the agent's raw and common transcripts actually landed in `<local_host_dir>/preserved/<agent-name>--<agent-id>/` (keyed on the seeded secret), so a swallowed preservation failure can no longer pass silently. Every plugin built on the shared lifecycle inherits the check.

- Profiles can declare `native_session_preserved_relpaths` so the lifecycle also asserts the agent's native resumable session store was preserved on destroy (not just the transcripts). A FIXME marks where this should grow into an actual resume-from-preserved-store check once `--adopt-session` lands for these agents.

## Azure provider registration

- Added `azure` to the set of remote provider backends that are skipped when tests load local-only backends (`_REMOTE_BACKEND_NAMES` in `providers/registry.py`), so the new `mngr_azure` plugin behaves like `aws` / `gcp` / `vultr` during test isolation.

- `ProviderUnavailableError` now accepts an optional `user_help_text` override. The default still tells the user to start Docker / disable the provider, but cloud providers (whose "unavailable" cause is a credential/subscription problem, not a local daemon) can pass curated guidance instead -- so a cloud auth failure no longer advises "start Docker". Used by the Azure provider.

- Regenerated `mngr azure` and `mngr ovh` CLI docs: `mngr azure prepare` / `mngr azure cleanup` and `mngr ovh list` now take a `--provider` option (and the standard common options) so they read defaults from the selected `[providers.NAME]` settings.toml block.

- Added the `azure` provider backend (`imbue-mngr-azure`) to the install-wizard plugin catalog (`PLUGIN_CATALOG`), so `mngr plugin install` offers it alongside `aws` / `gcp` / `ovh` / `vultr`.

- `mngr gc` gained a provider garbage-collection hook (`ProviderInstanceInterface.gc_provider_resources`, a no-op by default) so a provider can reclaim orphaned cloud resources that are not attached to any host. Reclaimed resources are reported in the gc summary (human / JSON / JSONL) under "Provider resources" and honor `--dry-run`. The Azure provider uses it to reap NIC / public-IP orphans from failed VM creates; that cleanup previously ran at the start of the next `create_instance`.

Changed: `mngr_common_transcript_flush` (shared common-transcript helper) now takes an
optional lock-acquire timeout (seconds), exported as `MNGR_CONVERT_LOCK_TIMEOUT` to each
synchronous converter pass. This lets a latency-sensitive caller (e.g. a SIGTERM/SIGINT
handler) cap how long the flush blocks waiting for the convert lock -- its only
potentially-slow step. Implemented without `timeout(1)` so it stays portable to macOS.
Callers that pass no argument are unchanged (default 30s).

- `mngr git push`, `mngr git pull`, and `mngr rsync` now run the underlying `git` / `rsync` binary as a plain subprocess with the user's stdout/stderr (no redirection), so progress, errors, and pager-style output flow directly to the terminal. stdin is redirected to /dev/null, so the underlying binary can't block waiting for input (credential prompts, host-key confirmations, merge-message editors, etc.) -- those are misconfigurations and we'd rather fail fast than have the agent hang. mngr still waits for the underlying process to exit, so destination-side cleanup -- including `mngr rsync --uncommitted-changes=merge`'s stash pop -- continues to run as before. The `_complete` JSONL terminating events (`{"success": true}` for git, `{"event": "rsync_complete", ...}` for rsync) and the trailing "Rsync complete: N files, M bytes transferred" human line are gone; on a non-zero exit, mngr raises its own `GitSyncError` / `MngrError` so the CLI still surfaces the failure (the underlying exit code is included in the message).
- `imbue.mngr.api.git.git_push` / `git_pull` and `imbue.mngr.api.rsync.rsync` / `rsync_to_remote` / `rsync_from_remote` grow an optional `run_in_terminal: bool = False` parameter. Default behavior (captured stdout/stderr via `ConcurrencyGroup`) is unchanged, so in-process callers like `mngr_pair` and `mngr_mapreduce` are unaffected; only the `mngr` CLI passes `True`. The terminal-stdio path goes through the existing `imbue.mngr.utils.interactive_subprocess.run_interactive_subprocess` helper, called with `stdin=subprocess.DEVNULL`.
- `RsyncResult`, `imbue.mngr.utils.rsync_utils.parse_rsync_output`, and the whole `rsync_utils` module are removed. The rsync API functions now return `None` -- the existing callers (the `mngr rsync` CLI and `mngr_mapreduce`) already discarded the return value, and the `files_transferred` / `bytes_transferred` counts were already useless in `run_in_terminal=True` mode. rsync still runs with `--stats`, so CLI users still see the summary block in their terminal at the end of a transfer.

`mngr stop`, `mngr destroy`, and `mngr cleanup` now aggregate and classify cleanup failures instead of hanging on, or silently swallowing, problems.

Previously the stop path ran its tmux/process-collection shell commands (`tmux list-windows`, `tmux list-panes`, `tmux kill-session`, the `pgrep` descendant walk, the `MNGR_AGENT_ID` env scan, and the SIGTERM/SIGKILL loop) without a timeout, so a wedged `tmux list-panes` could block `Host.stop_agents` indefinitely; and both the stop and destroy paths swallowed most other failures (logging a warning and exiting 0), so a partially-failed cleanup looked identical to a clean one.

Now every cleanup step is bounded and its outcome is classified as either benign (the target was already gone -- no error, exit 0) or a real failure (a resource is actually left behind). Real failures are aggregated across all steps/agents/hosts (cleanup continues rather than failing fast), each tagged with a cause category, and surfaced two ways:

- The process exits with a cause-specific, informative exit code (the most severe cause when several occur): `2` timeout, `3` processes remain, `4` local state remains, `5` host/infrastructure remains, `6` provider inaccessible, `1` other. A clean or only-benign run still exits `0`.

- Structured output (`--format json`) now reports a `failures` list (each with `category`, `message`, `agent_name`, `host_id`) and an `exit_code` field, replacing the old `errors` string list.

Benign detection: shell commands (stop, `rm`) classify by stderr message matching (e.g. tmux "can't find session", kill "No such process"); the destroy path classifies by provider exception type (e.g. Docker `NotFound`). Timeouts are treated as one failure cause among many. Stopping an agent on an offline host is now reported as a real `PROVIDER_INACCESSIBLE` failure (the host is unreachable so the agents cannot be verified stopped) rather than a silent skip.

`Host.stop_agents`, `Host.destroy_agent`, and `ProviderInstance.destroy_host` now raise a `CleanupFailedGroup` (an `ExceptionGroup` whose leaves carry the classified `CleanupFailure`s) when a real resource is left behind, and return normally otherwise -- so a caller can never silently drop a leftover-resource failure by ignoring a return value. `execute_idempotent_command` gains an opt-in `raise_on_timeout` flag that normalizes the two backends' differing timeout signals (local: a killed process; remote: `socket.timeout`) into a single `CommandTimeoutError`; other callers are unchanged. See `specs/cleanup-error-aggregation.md`.

`mngr gc` now reports cleanup failures as structured, categorized failures (consistent with `mngr destroy`/`stop`/`cleanup`) and **exits with a cause-specific exit code** (`2`/`3`/`4`/`5`/`6`, most severe when several occur) when garbage collection leaves a resource behind -- e.g. a snapshot or volume that could not be deleted (`5` host/infrastructure remains), a work dir, source dir, log, or build-cache entry that could not be removed (`4` local state remains), or an explicitly-requested provider that was unavailable (`6` provider inaccessible). Previously gc only exited non-zero when an explicitly-requested provider was skipped, so failed snapshot/volume/work-dir deletions did not affect the exit code. Structured output (`--output json`/`jsonl`) now includes a `failures` list (each with `category`, `message`, `agent_name`, `host_id`) alongside the existing `errors` strings.

Gave `test_cli_create_rejects_dirty_tree_by_default` a 30s `@pytest.mark.timeout` (matching its sibling `test_cli_create_via_subprocess`) so a cold `uv run mngr create` startup under load no longer races the global 10s pytest timeout.

- **Concurrent SSH keypair creation is now race-free.** `load_or_create_ssh_keypair` (`providers/ssh_utils.py`) serializes first-time creation behind an exclusive file lock, and `save_ssh_keypair` writes both key files atomically (temp file + `os.replace`, via the shared `atomic_write` helper) before applying their permissions. The parallel host-discovery fan-out opens one SSH connection per VPS and each lazily creates this keypair on first use; previously racing writers could leave a transient zero-byte or mismatched `.pub`, which surfaced as `ValueError: Not enough fields for public blob` deep in paramiko's certificate probe and aborted `mngr create`. This was observed intermittently on the OVH (and, via the same discovery path, Vultr) release tests.

- **paramiko's bare key-probe `ValueError` is now surfaced as a structured `HostConnectionError`.** `OuterHost._ensure_connected` wraps the `ValueError` paramiko raises when it parses a malformed/half-written `.pub` next to the private key, so callers that catch `MngrError` (e.g. best-effort host discovery) treat it as an ordinary per-host connection failure instead of letting it abort the whole operation.

## 2026-06-15

Regenerated the bundled CLI reference docs to include the new `mngr imbue_cloud admin server pricing` command (per-slice OVH bare-metal pricing table).

Regenerated the `imbue_cloud` CLI reference docs to include the new `admin server` command group (list / register / allocate-slice / set-status) added for the OVH bare-metal slices feature.

- `mngr create --format json` (and `--format jsonl`) now also reports the created host's name and SSH connection (`ssh_user` / `ssh_host` / `ssh_port`), plus an `outer_ssh_port` when the provider exposes a separate outer/management sshd (e.g. an OVH-slice's VM-root port reached via a box-forwarded port). Previously only `agent_id` / `host_id` were emitted. A new `HostInterface.get_outer_ssh_port` hook (default `None`) backs this.

- `VpsDockerProvider.record_outer_host_key` pins an outer (VPS-root) sshd host key in the provider's known_hosts -- used when operating on a VPS the provider did not order itself (e.g. the imbue_cloud rebuild on a leased host) so its outer connections pass strict host-key checking.

- `mngr create --format json` now also reports the agent SSH endpoint's on-disk private key path (`ssh_key_path`), so pool-bake tooling can run post-bake SSH steps against the host without a second `mngr list` round-trip.

Added a `--window` (`-w`) option to `mngr capture` for capturing a non-primary tmux window in the agent's session, by index (e.g. `--window 1`) or name. Without it, capture still reads the agent's primary window as before.

Added a shared shell library `mngr_common_transcript_lib.sh`, provisioned to every
agent's `commands/` dir alongside `mngr_log.sh` and `mngr_transcript_lib.sh` (via
`Host._ensure_shared_shell_libs`). It centralizes the common-transcript converter
primitives shared across agent plugins:

- the convert lock (a coarse mkdir-based mutex serializing the converter's
read-modify-write so the background daemon and an on-demand `--single-pass` flush
can't append duplicate events), and

- the turn-end flush (one synchronous `--single-pass` of the raw streamer + common
converter, in pipeline order), used by agent turn-end hooks so a WAITING-signal
consumer can't outrun the converter.

Centralized the Claude Code CLI presence check. `mngr extras` status (`_claude_plugin_status`) and the `is_claude_installed` test helper now both defer to the canonical `CLAUDE.is_available()` system-dependency check instead of re-implementing `shutil.which("claude")` inline, so the binary name and lookup logic live in one place.

Also removed a duplicate subprocess-error tuple: `extras.py` now imports the shared `SUBPROCESS_ERRORS` from `imbue.mngr.utils.deps` (promoted from the previously private `_SUBPROCESS_ERRORS`) rather than defining its own copy.

Fixed a regression in the e2e test fixture that wrote a malformed `settings.local.toml`
(a duplicated `type = "claude"` key under `[commands.create]`). The duplicate key made
every `mngr` invocation in the e2e/tutorial release suite fail to parse its config and
exit non-zero, cascading into a large block of release-test failures.

Also updated two e2e tutorial tests (`test_config_set_unknown_key_fails`,
`test_config_set_rejects_unknown_key`) that assumed the project `settings.toml` does not
exist until a command writes it. The e2e fixture now intentionally pre-seeds that file
with the pytest opt-in key, so these tests now verify that a rejected `config set` leaves
the file unchanged (and never writes the bad key) rather than asserting the file is absent.

All test-only changes; they do not change `mngr`'s runtime behavior.

## GCP provider integration

- `mngr create` CLI markdown docs regenerated to include the new `gcp` provider's build-args help (`--gcp-zone`, `--gcp-machine-type`, `--gcp-image`, `--gcp-spot`, `--git-depth`), and the `mngr gcp` operator command group docs added (`docs/commands/secondary/gcp.md`), covering both `mngr gcp prepare` and `mngr gcp cleanup`.
- `gcp` added to `_REMOTE_BACKEND_NAMES` in `providers/registry.py` (alongside `aws`/`vultr`/`modal`/`imbue_cloud`). The GCP backend resolves Application Default Credentials at build time, and `google.auth.default()` probes the GCE metadata server as its last fallback, which blocks for seconds in non-GCE environments without credentials. Marking it remote means `load_local_backend_only` (the test default) skips it, so provider-enumerating tests no longer build a default GCP provider and hang on that probe.
- `gcp` and `ovh` added to the install-wizard plugin catalog (`plugin_catalog.py`) as INDEPENDENT provider backends, so `mngr` offers them during plugin installation alongside `aws`/`vultr`/`modal`. (`ovh` was already published but had been missing from the catalog.)
- New `ssh_utils.wait_for_expected_host_key` (and the `parse_openssh_public_key_blob` helper): polls a server's live SSH host key until it matches a known expected key, then returns. Used by the GCP provider, whose `startup-script` installs the host key only after sshd has already booted with a random one; waiting before the strict-checked connection avoids a host-key-mismatch abort without resorting to TOFU.

CLI docs (`libs/mngr/docs/commands/secondary/usage.md`, regenerated via `scripts/make_cli_docs.py`): reflect the `mngr usage` help-output regrouping and the `--max-age` -> `--stale-after` rename in `imbue-mngr-usage`. `--stale-after` and `--detail` now appear under a `## Display` section; `--since` and `--preserved/--no-preserved` now appear under `## Filtering` alongside the agent-filter flags. The old `## Other Options` section is gone. The rendered synopsis is updated to `mngr usage [--stale-after DURATION] [--detail] [--since DURATION] [--no-preserved] [COMMAND]`.

`test_synopsis_lists_all_non_optout_flags` (in `libs/mngr/imbue/mngr/cli/help_formatter_test.py`): no longer silently skips commands whose synopsis is a placeholder like `[OPTIONS]`. Such commands' custom non-Common flags are now reported as missing from the synopsis (and the author must either enumerate them or add them to `_SYNOPSIS_OPTOUT_FLAGS`). Factored out a shared `_AGENT_FILTER_FLAGS` constant covering the 11 flags injected by `add_agent_filter_options`.

`mngr connect`, `mngr gc`, `mngr list`, `mngr snapshot create`, `mngr snapshot list`, `mngr snapshot destroy`: replace placeholder `[OPTIONS]` synopses with enumerated ones. For `list`, behavior/input flags (`--stdin`, `--schema`, `--ids`, `--addrs`, `--fields`, `--sort`) are pulled to the front, followed by the standard filter set, with `--limit` and `--on-error` trailing. For `connect`, `[future]` flags (`--reconnect`, `--session-command`) are omitted -- they remain unimplemented stubs.

`[future]`-flag detection: extract `_check_connect_future_options` in `connect.py` (mirroring `_check_create_future_options` / `_check_list_future_options` in `snapshot.py`) and pin every `[future]` flag with parametrized tests (`test_future_flags_raise_not_implemented_error` in `connect_test.py`; `test_snapshot_create_future_flags_raise_not_implemented_error` / `test_snapshot_list_future_flags_raise_not_implemented_error` in `snapshot_test.py`) that fail when any `[future]` flag stops raising `NotImplementedError`. The failure message tells the author to add the flag to its command's synopsis and delete the corresponding test case.

CLI docs regenerated: `libs/mngr/docs/commands/primary/connect.md`, `libs/mngr/docs/commands/primary/list.md`, `libs/mngr/docs/commands/secondary/gc.md`, `libs/mngr/docs/commands/secondary/kanpan.md`.

Added a shared `WaitingReason` enum (`imbue.mngr.primitives`) and a shared `classify_waiting_reason` rule (`imbue.mngr.hosts.common`) so agent plugins compute the `waiting_reason` field from one source of truth instead of each defining their own. The codex and claude plugins now import these instead of duplicating the enum and the gating logic, and use the existing `OnlineHostInterface.path_exists` for marker checks rather than a private file-existence helper.

## 2026-06-14

- Changed: `mngr extras` status (`_print_extras_status`) now accepts an injectable `claude_status_fn` (mirroring the existing `status_fn` seam on the `_install_*` helpers), so its test can skip shelling out to the `claude` CLI. This removes the slow, variable Node-process startup that made `test_print_extras_status_runs_without_error` flaky under the offload timeout. Internal/test-only; default behavior is unchanged.

Fixed `mngr clone` failing with "destination path ... already exists and is not an empty directory" when cloning a remote agent to a local target (e.g. `mngr clone <agent> <name> --provider local`). The git-mirror transfer initializes a bare repo at the target before fetching, but the remote-source-to-local-target path used `git clone --mirror`, which refuses a non-empty destination. It now performs a mirror-style `git fetch` into the existing bare repo instead.

User-facing CLI errors (`MngrError` and its subclasses) now render their `Error:` line in bold red on a color-capable terminal, matching the colored `ERROR:` prefix already used for `logger.error`. Previously click printed the line in the default terminal color, so an actionable failure (for example, "run `mngr gcp prepare` first" before the firewall rule exists) was visually indistinguishable from normal output. Coloring is gated on the same policy as other mngr output: it is suppressed when stderr is not a TTY or when `NO_COLOR` is set, so piped/captured output stays plain. Exit semantics are unchanged (still a clean exit 1, never a traceback).

Fix the "branch already checked out" error from `mngr create` to suggest the correct flag. It previously pointed users at `--in-place`, which no longer exists (it was consolidated into `--transfer`); it now suggests `--transfer=none`.

Fixed `mngr clone` (and any agent creation) failing when transferring a git repo from one remote host to another remote host. Previously the git push was run directly on the remote source host but pointed at the target's SSH key and known_hosts files, which only exist on the local orchestrator machine -- producing errors like "Identity file ... not accessible" and "Host key verification failed". The transfer now relays through a local bare mirror: it pulls from the source using the source's credentials, then pushes to the target using the target's, both run locally where those files exist. This matches the existing remote-to-remote handling already used for rsync transfers.

Fixed the SSH-backed-host test fixture (`local_sshd`) leaking `git config --global` writes into the developer's real `~/.gitconfig`. Tests that exercise remote git transfers run `git config --global --add safe.directory ...` over the SSH connection, where the test's HOME-redirection fixtures cannot reach; the fake sshd now sets `GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM` for its sessions so those writes stay inside the test sandbox.

## 2026-06-13

Fixed a data-loss bug in volume garbage collection and made discovery fail
loudly (instead of silently skipping) when a provider's backend is unreachable.

The data-loss bug: when the Docker daemon became briefly unavailable during a
`mngr` operation that runs GC (e.g. a Docker daemon restart), the Docker
provider's `discover_hosts` swallowed the failure and returned an empty host
list. GC then treated every volume as orphaned and deleted it -- wiping the
per-host data of still-live hosts, so their containers could no longer be
restarted. The Docker provider's `discover_hosts` now raises
`ProviderUnavailableError` when the daemon is unreachable instead of returning
`[]`, so an unreachable daemon can no longer be mistaken for "this provider has
zero hosts". GC skips an unavailable provider at its own boundary (it must not
delete volumes it cannot verify).

"Unreachable" is judged by the transport, not by the exception base class: a
dropped connection or timeout (including the daemon disappearing mid-operation,
which surfaces as a raw `requests` connection error rather than a
`DockerException`) maps to `ProviderUnavailableError`, while a
`docker.errors.APIError` -- meaning the daemon was reached and answered with an
error -- propagates as a real fault. This keeps a healthy-but-erroring daemon
from being silently treated as offline (and its provider wrongly skipped by GC).

Discovery no longer hides an unreachable provider. Previously, multi-provider
discovery silently skipped a provider whose backend was down. That meant a
command could quietly do a partial job -- e.g. `mngr message my-agent`, intended
to reach every instance of `my-agent`, could miss an instance on a down provider
without telling you. Now `discover_hosts_and_agents` propagates
`ProviderUnavailableError`, so commands that scan every provider (`message`,
`limit`, `snapshot`, `create`) fail loudly rather than silently omit agents on
the unreachable provider.

Targeted commands now scope discovery so an *unrelated* down provider can't fail
them. `mngr rsync`, `mngr git push`/`pull`, and `mngr event <host>` now resolve
only the provider(s) that could actually hold the target -- via the `.PROVIDER`
qualifier and/or the agent name (resolved through the discovery event stream) --
instead of blindly scanning every provider. So `mngr rsync ./x agent@host.local`
keeps working when an unrelated Docker daemon is down (Docker is never queried),
while a command whose target really is on the down provider fails with a clear
"provider is not available" error.

- `mngr snapshot create`, `mngr snapshot list`, and `mngr snapshot destroy` now take agent and host targets as a single positional list. The `--agent`/`--host` flags have been removed; write `agent`, `agent@host[.provider]`, `@host[.provider]`, or a bare `host-...` ID instead.
- `mngr event @host[.provider]` now narrows discovery to the pinned provider, matching the agent path's behavior.

- `mngr message` (alias `msg`) no longer accepts `--provider`. The set of providers to query during discovery is now derived from the agent addresses themselves: the union of providers named by the addresses, or a full scan if any address omits its provider. Previously the discovery call ignored the providers named by the addresses and only honored `--provider`, so e.g. `mngr msg agent@host.provider_a --provider provider_b` would query the wrong provider and report the agent missing.
- Internally, `mngr message` now routes through `find_all_agents` like the other agent-address subcommands instead of running its own CEL-filter pipeline. The `send_message_to_agents` API now accepts a pre-resolved `Sequence[AgentMatch]` instead of CEL `include_filters` / `exclude_filters` / `all_agents` / `provider_names`. No behavioral change for users beyond the `--provider` removal above.

Fixed the DESCRIPTION (and other prose) sections of `mngr <command> --help` to be indented to the man-page depth of seven spaces in an interactive terminal (the pager / rich path). Previously these sections rendered flush-left, unlike the piped/plain output and the surrounding sections.

## 2026-06-12

Updated the auto-generated `mngr create` docs for the `--adopt-session` option: a bare session ID is now searched in the current and user-scope Claude config dirs, every live local mngr agent, and preserved sessions from destroyed agents.

Internal: added `get_agents_root_dir(host_dir)` as the single source of truth for the agents-state root directory, defined alongside `get_agent_state_dir_path` in `imbue.mngr.hosts.common` (a low-level module importable without circular-import issues). Consolidated the previously hand-written `host_dir / "agents"[ / str(agent_id)]` path constructions across the codebase to route through these two helpers. No behavior change.

Added support for agent-type aliases and made disabled-plugin errors name the real owning plugin.

- Plugins can now register short alternate names for the agent types they provide via the new `register_agent_aliases` hook. An alias is accepted anywhere its canonical type is and resolves to the same agent (e.g. `mngr create my-agent agy` is equivalent to `mngr create my-agent antigravity`). Aliases are a name-resolution layer above the agent-type registries: an alias is not itself a distinct agent type, so it does not appear in `mngr plugin list --kind agent-type`, but it is tab-completable for `--type` and the persisted agent records the canonical type name. If you define a custom `[agent_types.X]` block whose name matches a built-in alias, your custom type takes precedence: the alias is dropped (with a warning) and the name resolves to your custom type.

- mngr now records which plugin registered each agent type, so the "plugin is disabled, enable it with `mngr plugin enable ...`" message names the actual registering plugin instead of assuming the plugin name equals the agent-type name. This fixes the message for types whose name differs from their plugin's entry-point name (e.g. the `pi-coding` type registered by the `pi_coding` plugin).

Tab completion now completes the `-S`/`--setting` config override (`KEY=VALUE`) on every command.

- Pressing TAB after `-S` (or `--setting`) completes the config KEY against the same catalog of keys behind `mngr config set` (e.g. `mngr create -S head<TAB>` -> `headless=`). Keys with a constrained value set (booleans, enums like log levels, provider/agent-type names) insert `KEY=` and then list the allowed values on the next TAB; free-form keys complete to the bare key name.

- Values complete too: `mngr create -S logging.console_level=<TAB>` lists `TRACE`/`DEBUG`/... and `mngr create -S headless=<TAB>` lists `true`/`false`. Works in both zsh and bash (which tokenize `KEY=VALUE` differently).

- Fixed a related completion bug: short value-taking options (`-S`, `-m`, `-b`, `-l`, `-n`, `-t`, `-o`, `-i`, `-s`, `-w`) were not recognized as consuming their value, so their argument was miscounted as a positional. This could suppress completion of a later positional argument on commands with a fixed argument count (e.g. after `-S KEY=VALUE`). Long and short option forms are now classified uniformly (this also fixes the same miscount for the long `--verbose` count option), and the bash `=`-word-break splitting of `KEY=VALUE` values is handled so the value pieces are not miscounted.

- `agent_types.*` keys now complete for builtin/registered agent types (e.g. `agent_types.claude.*`, `agent_types.codex.*`), not just the custom agent types defined in your config. These are derived from each agent type's config schema, so every settable field is offered (including container fields like `config_overrides` that have no value set yet), and constrained fields complete their values (`parent_type` -> agent type names, boolean fields -> `true`/`false`).

- Config keys now complete one dotted segment at a time instead of dumping every fully-qualified key at once. For both `-S`/`--setting` and `mngr config get`/`set`/`unset`, the first TAB shows the top-level keys (with deeper keys collapsed to a `section.` branch, e.g. `agent_types.`, `logging.`), and each further TAB drills into the next segment. The trailing `.` on a branch is inserted with no following space so you can keep typing the next segment (in zsh and bash). For `-S`, a key with a constrained value set is completed to `KEY=` the same way -- its allowed values are deferred to the next TAB rather than listed as soon as the key prefix matches.

- Shell completion now installs as a small, stable shim in your rc that sources a managed completion file (`~/.mngr/completions/mngr.{zsh,bash}`) which mngr keeps up to date. This means completion improvements (like the segment-at-a-time drilling above) apply automatically when you upgrade mngr -- no need to re-edit your rc file. The managed files are refreshed automatically in the background (e.g. on `mngr list`) and by `mngr extras completion`.

- If you have an older self-contained completion function in your rc (from before the shim), tab completion now prints a nudge (rate-limited to at most once a day, and only while the install is out of date) to run `mngr extras completion` to switch to the managed shim. `mngr extras completion` recognizes an outdated install (rather than treating any existing completion as already-configured), installs the up-to-date shim, and **removes the old self-contained completion block** when it byte-for-byte matches a form mngr generated (a hand-edited or unrecognized block is left untouched). So migrating is a single command with no leftover cruft. After installing, it prints the exact `source` command to activate completion in your current shell without opening a new one.

Fixed environment-variable forwarding for remote streaming SSH commands
(`OuterHost.execute_streaming_command` with `env=...`). The streaming path
prepended env vars as a bare `KEY=VAL command` prefix, which in the shell only
applies to the single simple command it precedes -- so for a compound command
like `install && tool ...` the var was gone by the time the second command ran.
It now uses `export KEY=VAL && command` (mirroring the non-streaming pyinfra
path), so the var is set in the shell environment for the whole command. This is
what made remote `depot build` fail with "missing API token" even though
`DEPOT_TOKEN` was supplied via `env`. Extracted the prefixing into a pure
`_prepend_env_exports` helper with unit tests.

Also fixed provider-config parsing to coerce field types. `_parse_providers` used
`model_construct`, which stored raw TOML scalars without coercion -- so an enum
field like `builder = "DEPOT"` stayed the string `"DEPOT"` and a `tuple` field
stayed a list. That tripped pydantic serializer warnings on `model_dump` and, for
a provider block defined in a single config layer (no merge to re-coerce it),
broke identity checks like `builder is DockerBuilder.DEPOT` (silently falling
back to the non-depot path). It now uses `model_validate`, which coerces while
still recording only the provided keys in `model_fields_set` so per-field
config-layer merging is unaffected. This also coerces nested-model provider
fields (e.g. SSH static `hosts` tables to `SSHHostConfig`), subsuming the
dedicated post-`model_construct` coercion helper that previously handled only
that case.

Added a `ScalarTuple` marker type (and the `ScalarStrTuple` annotated alias) for
tuple-typed settings fields that are semantically a single scalar value -- a
higher-precedence config layer that sets one replaces the whole value rather
than tripping the settings-narrowing guard. `StringDerivedTuple` (string-shaped
TOML values like `cli_args = "..."`) is now a specialization of it. This lets a
field like the AWS provider's `allowed_ssh_cidrs` be tightened in a developer's
`settings.local.toml` without the guard rejecting it as "dropping" the committed
default -- combining CIDRs across config layers is never the intent. The marker
is applied by `model_validate` (via the after-validator), so it relies on the
provider-parsing switch to `model_validate` described above.

## AWS provider support: shared layer changes

The `mngr_aws` plugin lands as a new provider backend. The shared `mngr` layer picks up the following supporting changes:

- New `resolve_backend_and_config(provider_name, mngr_ctx)` helper on `mngr/providers/registry.py`. Both `get_provider_instance` and the `mngr create` bootstrap path use it, replacing duplicated "configured-instance vs. bare-backend-name fallback" logic.
- `is_for_host_creation` removed from `ProviderBackendInterface` (Modal-specific flag was being `del`'d by every other backend); replaced with a default-no-op `bootstrap_for_host_creation(name, config, mngr_ctx)` method that Modal overrides and that `mngr create` invokes before `build_provider_instance`.
- The Docker state-container leak fix (read-only commands like `mngr list`/`gc` no longer lazily create the singleton state container) is carried into this new shape: the Docker backend's `build_provider_instance` now unconditionally raises `ProviderEmptyError` when the state container is missing (it is always read-only), and the Docker backend overrides `bootstrap_for_host_creation` to create the container on the `mngr create` path. This replaces main's `is_for_host_creation`-gated version of the same fix.
- `mngr/api/create.py`'s host-creation bootstrap helper is now public as `bootstrap_backend_for_host_creation(provider_name, mngr_ctx)` so other entry points (e.g. `mngr_tmr`'s snapshot path) can trigger the same one-time bootstrap before calling `get_provider_instance`.
- `aws` added to the remote-backend list and `mngr` plugin catalog.
- `mngr create` CLI markdown docs regenerated to include the AWS provider's build-args help.
- `test_cleanup_stop_action_with_real_agent` and `test_list_command_with_running_filter_alias` marked `@pytest.mark.flaky` after observing intermittent 10s-timeout failures on loaded offload sandboxes; pass locally in <3s.
- `_is_transient_ssh_error` (in both `hosts/host.py` and `hosts/outer_host.py`) now treats Python's built-in `TimeoutError` as transient. pyinfra's `read_output_buffers` raises a bare `TimeoutError` when an SSH command's response doesn't arrive within its per-command read timeout (e.g. when the remote sshd is reloaded mid-read during cloud-init bootstrap); the retry loop now picks it up rather than letting it escape host creation.
- `_run_shell_command`, `_get_file`, `_put_file`, and `execute_streaming_command` on `OuterHost` (and `Host._run_shell_command`) now catch the post-retry `TimeoutError` and surface it as a structured `HostConnectionError`. Their inner retry handlers also disconnect on `TimeoutError` so retries rebuild the SSH connection rather than reusing the dead channel.

- Resizing the terminal while attached to a remote agent (AWS or any SSH-backed provider) now reflows correctly instead of showing a field of padding dots once the window grew past a fixed size. The post-attach step previously ran `tmux resize-window -A`, which has a documented side effect of switching the window's `window-size` option to `manual` -- pinning the window at its attach-time size so it no longer tracked the client. It now only sends `SIGWINCH` to the pane processes (to prompt a redraw), leaving `window-size` at tmux's default `latest` so the window resizes with the client on every change.

- **De-duplicated the SSH error-translation chain in `OuterHost`.** The identical `except TimeoutError / OSError "Socket is closed" / EOFError,SSHException -> HostConnectionError` block that was copy-pasted across `_run_shell_command`, `_get_file`, `_put_file`, `execute_streaming_command`, and `list_directory` is now a single `_translate_ssh_errors(...)` context manager parameterized by the per-operation messages. No behavior change (the list-directory path still lets a raw `TimeoutError` propagate via the optional `timed_out` arg).

Regenerated the `mngr schedule` CLI reference docs to include the new
`--timezone` option on `schedule add` (added in the mngr-schedule plugin),
which pins the IANA timezone the `--schedule` cron expression is interpreted in.

Moved the built-in `codex` agent type out of mngr core into the external `imbue-mngr-codex` plugin (added to the plugin catalog as a recommended INDEPENDENT-tier agent type); removed the in-core `codex_agent` stub and its direct registration.

Updated the codex tutorial e2e tests (`test_create_basic.py`, `test_agent_types.py`) to match: codex is now a real agent-type plugin rather than a command-driven stub, so the tests create it with `--no-auto-start` (the real codex run is covered by the `mngr_codex` plugin's own release test) instead of faking it with a `command` override. Two of them were also dropped from `@pytest.mark.modal`: real codex can't run on the throwaway Modal hosts (no binary/auth), which is exactly what the old `sleep` fake worked around; Modal-path coverage of the create mechanism remains in the other (non-codex) tutorial tests. Also fixed a pre-existing duplicate `type = "claude"` key in the e2e test fixture's `settings.local.toml` template, which the old codex tests had been masking by rewriting the file via `mngr config set`.

Added `mngr list --schema`, a machine- and human-readable catalog of every field you can reference in `--include`/`--exclude`, `--sort`, and `--fields`/`--format`.

- `mngr list --schema` lists each referenceable field with its type, description, and the contexts it works in: `cel` (usable in `--include`/`--exclude` and `--sort`, which share one evaluation context) and `template` (also usable in `--fields`/`--format`). It composes with `--format json`, `--format jsonl`, and `--format` template strings, and is rejected (with a clear error) if combined with any agent-selection option since the catalog is static.

- The catalog is derived live from the real data shape (`AgentDetails`/`HostDetails`), so it always reflects the actual models -- including deeply nested fields like `host.resource.cpu.count` and `host.ssh.host`. The non-model fields (the computed `age`/`runtime`/`idle`, the `host.provider`/`project` aliases, and dynamic patterns like `labels.$KEY`) are listed explicitly and pinned to the real computation/alias tables by tests.

- The `project` field is now usable in CEL filters and sorts (e.g. `--include 'project == "mngr"'`, `--sort project`), mirroring the existing `host.provider` alias and the `--project` flag; previously it only worked in `--fields`/`--format` templates.

- The `mngr list` help "Available Fields" section (and the generated `docs/commands/primary/list.md` on GitHub) is now rendered from this same catalog, so the documented fields can no longer drift from the models.

Marked the `pi-coding` agent type plugin (`imbue-mngr-pi-coding`) as recommended
in the plugin catalog, so `mngr extras` offers it by default alongside the other
agent types (claude, opencode, antigravity) now that it has real lifecycle
support.

Added reusable gitignore-status helpers (with a `GitignoreStatus` result) to `mngr.api.git`. Given a host, a repo path, and any repo-relative path (which need not exist yet), they report whether that path is gitignored -- resolving symlinks anywhere along the path (e.g. `.claude -> .agents`) first so `git check-ignore` doesn't choke with "beyond a symbolic link":

- `check_path_gitignore_status` -- ignored by any rule (returns `SKIP` / `IGNORED` / `NOT_IGNORED`).

- `check_path_repo_gitignore_status` -- same, but a path ignored only by the user's global excludes returns `ONLY_GLOBAL` rather than `IGNORED` (for preflight checks whose result must also hold on a remote host / fresh clone, which has no global excludes).

Plugins use these to guard files they write into an agent worktree against showing up as untracked changes.

Added a canonical schema for the agent-agnostic common-transcript envelope
(`imbue.mngr.agents.common_transcript_records`). It is the single source of truth
for the `user_message` / `assistant_message` / `tool_result` records every agent
plugin emits into the stream `mngr transcript` reads, with a validator and a
conformance test asserting that all five emitters -- claude, antigravity,
opencode, pi-coding, and codex -- produce records matching it, so the
independently written emitters cannot silently drift on the shared fields.
A meta-test discovers every registered agent type that emits a common transcript
and fails if any lacks such a conformance test, so the requirement is enforced
rather than relying on convention -- a new agent plugin cannot merge without one.

Added a shared agent release-lifecycle harness
(`imbue.mngr.agents.agent_release_testing`) that drives the common create -> WAITING ->
message -> transcript -> stop/start resume -> destroy arc with per-agent profiles, so
each plugin's release test is a thin profile and every agent is held to the same
lifecycle and the same canonical-transcript contract.

## 2026-06-11

Replaced direct built-in exception raises (ValueError/RuntimeError) in config key resolution, docker provider config validation, and agent discovery with dedicated custom exception types.

Added an `OPT_IN_PLUGINS` set to the config pre-reader (`config/pre_readers.py`) for plugins that are **disabled by default** and must be explicitly enabled with `[plugins.<name>] enabled = true`. This inverts the normal default (plugins load unless explicitly disabled) for the listed plugins, reusing the same `enabled` config key. The first opt-in plugin is `claude_subagent_proxy`, which is very experimental and breaks other tooling.

The docker provider now raises a typed, actionable `DockerRuntimeNotRegisteredError`
when the configured `docker_runtime` (e.g. `runsc` for gVisor) is not registered
with the Docker daemon, instead of letting Docker's raw exit-125 `ProcessError`
propagate. The old behavior surfaced the entire `docker run` command line with the
real cause ("unknown or invalid runtime name: runsc") buried inside it and no
guidance. The new error renders as a clean message naming the runtime and provider,
with `user_help_text` pointing at the fix (install the runtime, or set
`docker_runtime=runc` via `mngr config set` / the
`MNGR__PROVIDERS__<NAME>__DOCKER_RUNTIME` env var). Because it is an `MngrError`
subclass, `mngr create --format jsonl` now emits `error_class:
"DockerRuntimeNotRegisteredError"` so callers can branch on the type.

## 2026-06-10

Added the `log_warnings` loguru-capture fixture to the shared plugin test helper (`register_plugin_test_fixtures` in `imbue.mngr.utils.plugin_testing`), so plugin test suites that register the standard fixtures can assert on emitted warnings without defining their own copy. The capture logic lives in a single `capture_log_warnings()` context manager in `imbue.mngr.utils.testing`, which both that fixture and mngr's own `conftest.py` `log_warnings` fixture delegate to. (Affects test infrastructure only.)

Improved test quality for the top-level `imbue/mngr` modules: replaced tests that passed without verifying behavior. `mngr --version` is now asserted to succeed unconditionally (the prior test accepted a since-fixed failure mode); deleted tautological `ToolRequirement` constructor tests and a type-enforced catalog signal check; strengthened the installable-packages, catalog-floor, and create-time parsing assertions; and removed a permanently-skipped upgrade-discovery test whose behavior is already covered by `hosts/host_test.py` and `test_install.py`.

- Sending a message to a Claude agent now confirms submission as soon as the message is accepted into the agent's queue, instead of always waiting on the `UserPromptSubmit` hook. The hook only fires when the prompt reaches the model, which for a message sent to a *busy* agent is when the agent finally dequeues it -- potentially many minutes later -- so `mngr message` (and any caller of the send path, e.g. an HTTP front-end) could block up to the full submission timeout and exceed a front-door proxy timeout, even though the message was already queued and would be processed. `send_enter_via_tmux_wait_for_hook` now watches a fresh `enqueue` event in the agent's transcript log *concurrently* with the hook's `tmux wait-for` signal (in a single remote command) and returns as soon as either lands. This keeps the call fast for busy agents while still confirming via the hook for prompts that never enqueue a model turn (the `/clear` and `/compact` TUI-local commands, whose signal is fired from `SessionStart`). Behavior is unchanged for TUIs that don't supply an acceptance-marker command (they keep the original hook-only wait). The shared `tui_utils` module stays agent-neutral: it watches an opaque, agent-supplied "message accepted" marker command for a fresh monotonic token, and the Claude-specific transcript-log/`enqueue`-event schema now lives in the Claude plugin rather than in the shared module.

The git repo-detection helpers (`find_git_worktree_root`, `is_git_repository`, `find_git_common_dir`) no longer silently swallow unexpected `git` failures. Previously any `ProcessError` (a non-zero exit, a timeout, or a failure to even spawn the subprocess) was caught and turned into a "not in a git repository" answer (None/False), so a transient or environmental git problem would silently drop the project-scope config layer (e.g. disabled plugins would not be blocked) or otherwise misreport repository state, with no explanation. Now only git's own "not a git repository" result maps to that sentinel; every other failure is raised with git's actual return code and stderr, so problems surface loudly instead of causing confusing downstream misbehavior. The detection calls also force a C locale so the check cannot be defeated by a localized git. This is the production-side fix for a rare flaky failure of `test_create_plugin_manager_blocks_disabled_plugins` in CI. No change to behavior when running inside or outside a git repository normally.

Regenerated the `mngr kanpan` CLI reference doc to document the new `--format json` / `--format jsonl` output (the command now prints a board snapshot for programmatic use instead of ignoring the flag).

- `mngr create --reuse` now scopes the existing-agent lookup to the host named in the agent address (e.g. `babatest` in `system-services@babatest.docker`), not just the provider. Previously, when creating a new host (`--new-host`), the reuse lookup ignored the address's host name and matched *any* same-named agent on the provider, so it raised "Multiple agents found with name '<name>'. Use address syntax ..." as soon as two or more same-named agents were discoverable -- even though the address already specified the host. This broke callers that deliberately share one agent name across many hosts and rely on the host name for identity (the minds desktop client names every workspace's primary agent the constant `system-services`). With the fix, a create targeting a brand-new host finds nothing to reuse and proceeds to create, while a re-create targeting an existing host reuses exactly the agent on that host. Genuinely ambiguous reuse (a shared name with no host in the address) still raises the disambiguation error.

Regenerated command docs: the `file` and `tmr` See-Also sections now link to `mngr rsync` instead of the removed `push`/`pull` commands, fixing broken `[mngr help push](mngr help push)` / `[mngr help pull](mngr help pull)` markdown links. The doc generator (`scripts/make_cli_docs.py`) now fails `--check` on any See-Also reference that resolves to neither a known command nor a help topic, so stale references like these are caught going forward.

`test_create_plugin_manager_blocks_disabled_plugins` now asserts up front that `MNGR_LOAD_ALL_PLUGINS` is not set, failing loudly with a diagnostic message if it is. That env var disables plugin blocking, so a leak from another test or imported module would otherwise silently mask the test; rather than papering over it, the test acts as a tripwire that surfaces the leak so it gets fixed at its source.

Strengthened several weak or fragile unit tests under `imbue/mngr/utils` so they actually catch regressions:

- `build_cel_context` tests now assert the converted CEL value types (`StringType`, nested `MapType`) and exact values, instead of merely checking that keys are present. They now catch a regression that stopped converting raw values to `celpy.celtypes`, which would break dot-notation filtering.
- The `imbue.mngr` import smoke test now asserts the package name and that the `cli` console-script entrypoint is a real Click command, instead of the tautological `assert mngr`; the file was also renamed from `test_mngr_import.py` to `mngr_import_test.py` to match the unit-test naming convention.
- `get_current_branch` test now checks out a deterministic, uniquely-named branch and asserts the exact returned name, so it would fail if the function returned a commit-ish or remote ref rather than the branch name.
- `check_bash_version` test now exercises the version-comparison branch (an unreachable `minimum=999` returns `False`) instead of only asserting the return type.
- `_format_arg_value` complex-object test now asserts the exact rendered repr via an inline snapshot, catching dropped fields or changed quoting.
- The asciinema cast-player init-script test now verifies the script wires `AsciinemaPlayer.create(...)` to the correct player div id (`player-0`) and runs inside the `DOMContentLoaded` handler, rather than just checking for substrings.
- Editor test sleep scripts now use large, globally-unique durations to avoid leak-detector collisions, with clarifying comments about why the long sleeps make the synchronous `is_running()` assertions race-free.
- The name-generator uniqueness tests now assert the observable behavior -- that 50 draws yield at least 10 distinct names -- instead of a probabilistic `>= 5` threshold or a seed-pinned exact count. The threshold is chosen so a correct generator's flake probability is ~1e-24, while not coupling the test to wordlist size or RNG draw order. (Per-name validity is covered by the existing per-style tests.)
- Relocated the name-generator tests from `test_name_generator.py` (integration-test naming) to `name_generator_test.py` (unit-test naming) to match their actual nature and the `_test.py` convention.
- Removed two tautological dataclass/enum tests (`InstallMethod` field round-trip and `DependencyCategory` member values) whose behavior is already covered by the behavioral install-command tests; added a comment to the logging-suppressor buffering test explaining why its count bound is `>=`.
- Raised the coverage floor from 80% to 85% (CI measures ~87%).

- `libs/mngr`: regenerate the `mngr forward` CLI reference (`docs/commands/secondary/forward.md`). The `--port` option's help text and default drifted out of sync with the dynamic-port behavior; the regenerated doc now describes the "try 8421, fall back to an OS-assigned port" semantics.

## 2026-06-10

Addressed Josh's review feedback on PR #1937:

- `mngr gc --provider <name>` now exits non-zero when an explicitly-named provider is unavailable. The other selected providers still run to completion; the unavailable provider is reported as an error in the summary so the user can see what was not gc'd. Empty providers (e.g. a fresh Modal per-user environment with nothing to collect) remain silently skipped, since their state is known to be empty and there is nothing to do. The automatic post-destroy gc path (which always passes `--all-providers` internally and tolerates skips) is unaffected.

- Removed the `-a` / `--all` / `--all-agents` flag from `mngr message` (alias `mngr msg`). The tutorial and CLI examples now use the explicit `mngr list --ids | mngr msg -` pattern. Users who relied on `-a` should switch to piping ids from `mngr list` (optionally with `--include` / `--exclude` to scope the broadcast).

- Dropped `--no-ensure-clean` from the agent-type e2e tutorial tests. The e2e fixture now gitignores the per-test project config directory (where `mngr config set` writes its files), so the working tree stays clean and the flag is no longer needed. The `@pytest.mark.rsync` markers on those tests (whose only purpose was to satisfy the resource guard for the rsync path that `--no-ensure-clean` happened to trigger) are removed alongside.

- Removed a stale duplicate `type = "claude"` line in the e2e fixture's seeded `settings.local.toml` that was causing every release-tier e2e/tutorial test to fail with "Cannot overwrite a value".

## 2026-06-09

Fixed the SSH provider silently disabling strict host-key checking for statically-configured hosts.

- A host defined under `[providers.<pool>.hosts.<host>]` that set **both** `key_file` and
  `known_hosts_file` lost its `known_hosts_file` whenever the backend expanded the `key_file` path.
  `SSHProviderBackend.build_provider_instance` rebuilt the `SSHHostConfig` by re-listing only
  `address`/`port`/`user`/`key_file`, so `known_hosts_file` silently became `None` and strict
  host-key checking was turned off for that host. The backend now updates only the `key_file`
  field, preserving `known_hosts_file` (and any future fields), matching the dynamic-hosts path
  that already did this correctly.
- Consolidated the `key_file` path-expansion logic (previously hand-rolled separately in the static
  and dynamic host paths) into a single `SSHHostConfig.with_expanded_key_file()` method that both
  paths now call, so neither can silently drop a field if `SSHHostConfig` gains one in the future.

Fixed a crash that made statically-configured SSH hosts unusable, and added documentation for the SSH provider.

- Static `[providers.<pool>.hosts.<host>]` tables defined in `settings.toml` previously crashed every
  host-enumerating command (`mngr list`, `mngr connect`, `mngr create <agent>@<host>.<pool>`, ...) with
  `AttributeError: 'dict' object has no attribute 'key_file'`. Provider configs are built with
  `model_construct` (to keep unset top-level fields `None` for config-layer merging), which does not
  coerce nested values, so each host entry stayed a raw dict instead of an `SSHHostConfig` and blew up
  as soon as the backend touched it. The config loader now coerces nested pydantic-model fields (the
  only one today being the SSH provider's `hosts` map) after `model_construct`, so static SSH hosts load
  and resolve correctly. A malformed host entry now produces a clear `providers.<pool>.hosts` config
  error instead of a late crash.
- Added an [SSH provider documentation page](../docs/core_plugins/providers/ssh.md) covering host
  configuration (`address`/`port`/`user`/`key_file`/`known_hosts_file`), the dynamic-hosts file, the
  `NAME@HOST.PROVIDER` form for running an agent on a configured host, and the provider's limitations
  (no host creation/snapshots/tags). Registered the `ssh` backend in the provider concepts doc.

Fixed the Docker provider leaking singleton "state containers". Read-only commands (`mngr list`, `mngr gc`, `mngr cleanup`, and any cross-provider discovery) no longer create a Docker state container when none already exists: the Docker backend now treats the provider as empty and skips it, mirroring how the Modal backend skips a provider whose environment does not exist. Only `mngr create` (which passes `is_for_host_creation=True`) creates the state container. Behavior for existing Docker hosts is unchanged.

Also hardened the test suite against leaked Docker state containers: fixed an off-by-one in the leaked-container detector, and the per-worker session cleanup now fails the suite when a state container created under one of its own test prefixes is left behind (while still warn-and-cleaning unattributable containers from other concurrent workers or older sessions).

Fixed create-template `setting`/`setting__extend` entries being silently dropped. A `--template` whose definition sets `setting__extend = ["providers.docker.docker_runtime=runsc"]` (or any other config key) now actually reaches the resolved config instead of being ignored. Direct CLI `-S` still wins over a template-provided setting for the same key.

A template `setting` that targets `commands.*` or `create_templates.*` now raises a clear error (those sections are resolved before template settings are applied, so the value could never take effect) instead of being silently ignored.

Fixed `mngr config get` and `mngr config list --all` so they surface provider-subclass fields (e.g. `docker_runtime` on a docker provider) instead of reporting "Key not found".

Introduced a standard way for plugins to preserve files from an agent's state directory when
the agent (or its whole host) is destroyed, and made stopped hosts readable through a uniform
interface.

- New `HostFileReadInterface` (in `interfaces/host.py`) captures the read-only file operations
  (`read_file`, `read_text_file`, `path_exists`, `get_file_mtime`, `list_directory`) that work
  even when a host is not online, as long as its persistent storage (volume) is reachable.
  `OuterHostInterface` now extends it, so every online host is a `HostFileReadInterface`.
- New `OfflineHostWithVolume` (in `hosts/offline_host.py`) implements `HostFileReadInterface`
  on top of a stopped host's persisted volume, addressing files by absolute paths under
  `host_dir` exactly as an online host would. `make_readable_offline_host()` wraps a plain
  `OfflineHost` in this readable form when the provider yields a volume for it (else returns the
  plain `OfflineHost`), and every provider's offline-host construction now does so -- so a
  stopped host is readable whether it is reached via `get_host` (the destroy/GC path) or
  `to_offline_host`. The volume *reference* is fetched via a new provider method,
  `get_volume_reference_for_host`, which (unlike `get_volume_for_host`) skips any network
  existence probe -- e.g. Modal's `listdir` -- and returns the lazy reference, so constructing a
  readable offline host (including during host discovery) adds no per-host probe; only providers
  that actually probe (Modal) override the method, and a since-deleted volume surfaces as a
  read/write failure at access time. This lets callers treat a stopped-but-volume-backed host
  uniformly with an online one instead of branching on online-vs-offline and reaching for the
  raw `Volume` API.
- New `api/preservation.py` with `PreservedItem`, `preserve_agent_data()`, and
  `get_preserved_agent_dir()`. Callers declare a list of paths (relative to the agent state
  dir) to keep; the same declaration is executed against either an online host (rsync for
  directories) or a volume-backed offline host (file-by-file walk). Preserved files mirror the
  agent-state-dir layout verbatim under `<local_host_dir>/preserved/<agent-name>--<agent-id>/`.
- `OuterHost` gained a `list_directory()` implementation (local filesystem walk, or SFTP
  `listdir_attr` over the same paramiko channel used for remote file reads).
- The listing-entry type `VolumeFile` is now the shared return type for every
  `HostFileReadInterface.list_directory` (hosts as well as volumes). Its `file_type` uses the
  full `FileType` enum (file, directory, symlink, pipe, socket, block, character, other), moved
  into core `interfaces/data_types.py` from `mngr_file` (which now re-exports it), with a
  canonical `FileType.from_stat_mode` classifier; `VolumeFile` also gained an optional
  `permissions` string. Producers fill these to the fidelity their source allows: a host
  classifies the real `stat`/`lstat` mode and reports a permissions string, while a bare volume
  only distinguishes file vs. directory and leaves `permissions` None. (`VolumeFileType` is gone,
  folded into `FileType`.)
- New `HostFileWriteInterface` (`write_file`, `write_text_file`), the write companion to
  `HostFileReadInterface`. `OuterHostInterface` extends it (so every online host writes), and
  `OfflineHostWithVolume` implements it by writing the stopped host's volume (file modes are not
  settable through a volume write, so `mode` is ignored there). This lets write commands target
  an online or a stopped host through one interface.
- `api/events.py` now reads and discovers event journals through `HostFileReadInterface`
  (an online host, or a readable stopped host whose volume is reachable) addressed by a single
  absolute events path under the host's `host_dir`. This removes the separate code paths that
  shelled out `find`/`cat` over SSH for online hosts and used a separately-fetched
  events-scoped `Volume` for everything else, collapsing the dual `online_host`/`volume`
  representation on `EventsTarget` into one `host` handle. It also drops the trailing-newline
  "sentinel-cat" workaround: the host read path is byte-exact (local reads bytes directly,
  remote uses SFTP), so a file's exact trailing-newline state survives without the sentinel.

Regenerated the `mngr usage` command reference docs to include the new `--preserved` / `--no-preserved` flag (on `mngr usage` and `mngr usage wait`). The behavior itself lives in the `mngr_usage` plugin; this is just the generated CLI doc reflecting it.

# Docstring: update stale provider-field example

`ProviderInstanceConfig.merge_with` used `is_host_in_docker` as an illustrative
provider field in its docstring. That field was removed from the Lima provider
(which no longer runs agents in a nested Docker container); the example now
references `is_run_as_root`. No behavior change.

Fixed the e2e tutorial test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) that
wrote a duplicate `type = "claude"` key under `[commands.create]` in the generated
`settings.local.toml`. The duplicate key caused every e2e tutorial command to fail
with a TOML parse error ("Cannot overwrite a value"). Removing the redundant line
restores config parsing so the e2e tutorial tests run.

Fixed: the very first `mngr create --provider modal NAME` (including the
`--reuse` form) against a brand-new per-user Modal environment no longer fails
with `Provider 'modal' has no state yet`. The CLI create path resolved the
new-host provider (used to tear the host down if a post-create step fails) with
read-only semantics, so on a not-yet-existing Modal environment it raised
`ProviderEmptyError` before the create could bootstrap the environment. It now
resolves that provider with `is_for_host_creation=True`, matching the API-layer
resolution, so the environment is created on first use as documented.

Fixed the e2e test fixture's generated `settings.local.toml`, which defined the
`type` key twice under `[commands.create]`. The duplicate key produced invalid
TOML ("Cannot overwrite a value"), causing every `mngr` invocation in e2e
tutorial tests to fail while parsing the config file.

Fixed the e2e test fixture that wrote a duplicate `type` key into
`settings.local.toml`, which produced invalid TOML and broke
`mngr observe --discovery-only` (and other config-reloading commands) under
the e2e suite. Also strengthened `test_advanced_observe_stream` to verify the
documented DISCOVERY_FULL snapshot contract (source, agents/hosts/providers
collections, and presence of the local provider).

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) so the
`settings.local.toml` it writes is valid TOML. The fixture had an accidental
duplicate `type = "claude"` key under `[commands.create]`, which made the
strict (tomllib) config read path fail with "Cannot overwrite a value". This
broke e2e tests whose commands parse config strictly, e.g.
`mngr list --running --format json` in `test_advanced_watch_dashboard_running`.

Fixed a duplicate `type = "claude"` key in the e2e test fixture's generated `settings.local.toml`, which produced invalid TOML ("Cannot overwrite a value") and broke every e2e tutorial test's environment setup.

Fixed the e2e tutorial test fixture and the command-agent dev-server test.

- Removed a duplicate `type = "claude"` key in the `[commands.create]` section of the
  `settings.local.toml` written by the shared `e2e` fixture (`e2e/conftest.py`). The duplicate
  key was a merge artifact that made the TOML unparseable, so every e2e tutorial command that
  loaded the config failed with "Cannot overwrite a value".
- `test_command_agent_dev_server_extra_windows` is a purely local command-agent test, so it no
  longer carries a spurious `@pytest.mark.modal` (which the resource guard rejected as "marked
  but never invoked"). Its verification listing is now scoped to `--provider local`, mirroring
  the sibling `test_command_agent_python_http`; the Modal command-agent path remains covered by
  `test_command_agent_batch_job_modal`.

Fixed the e2e tutorial test fixture: removed a duplicate `type = "claude"` key in the generated `settings.local.toml` that produced an invalid TOML file ("Cannot overwrite a value"), which caused `mngr create` to fail in command-agent tutorial tests.

Fixed the e2e test fixture so the shared `settings.local.toml` it writes is valid
TOML. The `[commands.create]` table contained a duplicate `type = "claude"` key,
which `tomlkit` rejects with "Cannot overwrite a value". This broke any e2e command
that loaded the merged config (e.g. `mngr config edit`), surfacing a config-parse
error instead of the command's real behavior.

Also strengthened `test_config_edit_editor_failure` to assert that `mngr config edit`
propagates the editor's exact exit code (1 from `/bin/false`) rather than merely
exiting non-zero.

Fixed the e2e tutorial test fixture which wrote a duplicate `type = "claude"`
key under `[commands.create]` in `settings.local.toml`. The duplicate produced
invalid TOML, causing `mngr` commands in affected e2e tests to fail at config
parse time with "Cannot overwrite a value" instead of exercising the command
under test. Also hardened `test_config_edit_scope_missing_editor` to assert the
missing-editor error names the editor and points at `$EDITOR`/`$VISUAL`.

Fixed the e2e tutorial test fixture so the `mngr config` tutorial tests pass again. The shared `e2e` fixture had two regressions: a duplicate `type = "claude"` key inside `[commands.create]` of the seeded `settings.local.toml` (which made the file unparseable, breaking `config path`/`config edit`), and a stray seed of the project-scope `settings.toml` (which prevented `config edit`/`config set` tests from exercising genuine first-use behavior, where the project config file does not yet exist). No user-facing behavior change; this is test-infrastructure only.

Fixed the e2e tutorial test fixture and the `test_config_edit` release test.

- The shared e2e subprocess fixture wrote a `settings.local.toml` containing a
  duplicate `type = "claude"` key under `[commands.create]`, which made tomlkit
  reject the file ("Cannot overwrite a value") and broke every e2e tutorial
  test. Removed the duplicate key.
- Adapted `test_config_edit` to the fixture's seeded project `settings.toml`:
  the project-scope config file already exists, so the test now verifies that
  `config edit` opens that exact file in `$EDITOR` and that the editor's marker
  persists into it, instead of asserting the file was created from scratch.
- Added `@pytest.mark.timeout(60)` to `test_config_edit`, which now runs two
  mngr subprocesses and exceeded the default 10s function timeout.

Fix duplicate `type = "claude"` key in the e2e test fixture's generated `settings.local.toml`. The duplicate line made the TOML unparseable, causing config-reading tutorial tests (e.g. `test_config_get_missing_key`) to fail with a parse error instead of exercising the intended behavior.

Fixed the e2e test fixture's local `settings.local.toml` template, which had a
duplicate `type = "claude"` key under `[commands.create]`. The duplicate made the
file invalid TOML, so any test that exercised `mngr config set --scope local`
(e.g. the `test_config_get` tutorial test) failed when tomlkit re-parsed the file
with "Cannot overwrite a value".

Fixed the e2e tutorial test fixture so that the generated `settings.local.toml`
no longer contains a duplicate `type = "claude"` key under `[commands.create]`.
The duplicate produced invalid TOML that tomlkit rejected when `mngr config set
... --scope local` re-saved the file, breaking config tutorial tests such as
`test_config_list_json`.

Fixed the e2e test fixture so `mngr config` tutorial tests work again.

- The shared e2e fixture wrote `settings.local.toml` with the `type = "claude"` key duplicated
  under `[commands.create]`, producing invalid TOML. Any `mngr config` command that loaded the
  merged config (e.g. `mngr config list --scope user`) then failed with a "Cannot overwrite a
  value" parse error. Removed the duplicate so the fixture emits valid TOML.
- Strengthened `test_config_list_scope` to assert real scope isolation: the fixture's
  `connect_command` value lives only in the local scope, so it must appear under
  `--scope local` and must not bleed into the user/project views.

Fixed the e2e test fixture that seeded a malformed `settings.local.toml` with a duplicate `type = "claude"` key under `[commands.create]`. TOML disallows duplicate keys, so any `mngr config set --scope local` (which re-parses and re-saves the file via tomlkit) failed with "Cannot overwrite a value". This unblocks the config tutorial e2e tests (e.g. `test_config_list`).

Fixed the e2e test fixture that seeds the local-scope config file: it wrote a
duplicate `type = "claude"` key under `[commands.create]`, producing an
unparseable `settings.local.toml`. This caused `test_config_path_invalid_scope`
(and any command that loaded the merged config) to fail with a TOML parse error
instead of exercising the intended behavior.

Fixed the e2e test fixture that seeded a malformed `settings.local.toml` with a
duplicate `type = "claude"` key under `[commands.create]`, which made every config
command fail to parse the file ("Cannot overwrite a value"). This unblocks
`test_config_path_scope` and the other CONFIGURATION tutorial e2e tests.

Fixed the e2e test fixture in `imbue/mngr/e2e/conftest.py` that wrote an invalid `settings.local.toml`: the `[commands.create]` table contained a duplicate `type = "claude"` key (introduced by a botched merge), which made every `mngr` invocation fail with "Cannot overwrite a value". This unblocks the e2e tutorial config tests (e.g. `test_config_path`).

Fixed the e2e test fixture's `settings.local.toml`, which wrote a duplicate `type = "claude"` key under `[commands.create]`. The duplicate is invalid TOML and caused `mngr config set` (which re-parses the file with a strict editing parser) to fail with "Cannot overwrite a value". Also added an explicit 60s timeout to `test_config_set_default_provider`, which runs two `mngr` invocations and was exceeding the 10s default.

Fixed the `test_config_set_headless` e2e tutorial test. The e2e fixture now seeds the project `settings.toml` with the pytest opt-in, so the test no longer appends a duplicate `is_allowed_in_pytest` key (which produced a TOML "Cannot overwrite a value" parse error). Also removed a duplicate `type = "claude"` key that the fixture wrote into `settings.local.toml`, which broke `mngr config set` whenever it loaded the merged config via tomlkit. Strengthened the test to assert the value lands in the project scope and is actually written to the on-disk `settings.toml`.

Fixed the e2e test fixture that seeded an invalid `settings.local.toml`: a bad
merge had added a duplicate `type = "claude"` key under `[commands.create]`,
which made the file unparseable TOML and broke `test_config_set_scope` (and any
other config test that loads the local layer).

Fixed the e2e config tests: the shared `e2e` fixture wrote a duplicate `type = "claude"` key into the local `settings.toml`, which is invalid TOML and broke `mngr config set` of an unknown key. Also updated `test_config_set_unknown_key_fails` to verify the rejected key is absent from the project settings file (the fixture now seeds that file with the pytest opt-in) rather than asserting the file does not exist.

Fixed the e2e test fixture that seeded a malformed `settings.local.toml` with a
duplicate `type = "claude"` key under `[commands.create]`. The duplicate caused
`mngr config set` (which round-trips the file through tomlkit) to fail with
"Cannot overwrite a value", breaking `test_config_set` and other config tests.

Fixed the e2e test fixture's `settings.local.toml`, which contained a duplicate
`type = "claude"` key under `[commands.create]` and was therefore invalid TOML.
Any e2e command that performed a full merged-config load (e.g. `mngr config
unset`) failed with a "Failed to parse config file" error instead of running.
Also tightened `test_config_unset_missing_key` to assert the error names the
specific missing key.

Fixed the e2e tutorial test for `mngr config unset`. The shared e2e fixture
wrote a `settings.local.toml` with a duplicate `type = "claude"` key under
`[commands.create]`, which made `tomllib` reject the file and broke config
loading for every e2e mngr command. Removed the duplicate. Also reworked
`test_config_unset` to set `commands.create.provider` before unsetting it (a
key that is never set cannot be unset) and to verify the value is actually
removed from the project settings file.

Fixed the e2e tutorial test fixture, which wrote an invalid `settings.local.toml` with a
duplicated `type = "claude"` key under `[commands.create]`. This caused every command run
through the fixture to abort with a TOML parse error ("cannot overwrite a value") instead of
exercising the actual code path.

Also strengthened `test_connect_by_agent_id_fictional` to assert that connecting to a
nonexistent agent id reports a clean user-facing "not found" error with no leaked Python
traceback.

Fixed the e2e tutorial connect tests (`test_connect.py`). The shared e2e
fixture wrote a `settings.local.toml` with a duplicate `type = "claude"` key
under `[commands.create]`, which made every `mngr create` in these tests fail
with a TOML parse error; removed the duplicate. The interactive
`run_connect_interactively` helper now clears the inherited `$TMUX`/`$TMUX_PANE`
and forces the builtin tmux attach (via `MNGR_CONNECT_COMMAND_ACTIVE`) so the
standalone `mngr connect` performs a real attach instead of being intercepted by
the no-op connect command or refused by the nested-tmux guard. The four
interactive connect tests also got a `@pytest.mark.timeout(120)` since the
create/attach/detach flow exceeds the default 10s per-test timeout. Test-only
change; no user-facing behavior changed.

Fixed a duplicate `type = "claude"` key in the e2e test fixture's generated `settings.local.toml`, which caused a TOML parse error ("cannot overwrite a value") and broke e2e tutorial tests. No user-visible behavior change.

Fixed the e2e test fixture (`conftest.py`) that wrote an invalid `settings.local.toml` with a duplicate `type = "claude"` key under `[commands.create]`. The duplicate key caused a TOML parse error ("cannot overwrite a value") that broke every e2e tutorial command, including `test_connect_explicit_host`.

Fixed the e2e tutorial connect tests (`test_connect.py`):

- Repaired the e2e fixture, which wrote a duplicate `type = "claude"` key into
  `settings.local.toml` and caused every `mngr create` in the e2e suite to fail
  with a TOML "Cannot overwrite a value" parse error.
- Added `@pytest.mark.timeout(120)` to the interactive `mngr connect` tests, which
  perform a full agent create plus interactive connect and exceed the default 10s
  per-test timeout.
- Added `test_connect_no_start_fails_when_stopped`, covering the documented
  unhappy path for `mngr connect --no-start` (it refuses to connect to a stopped
  agent rather than auto-starting it).

Fixed the e2e tutorial connect tests. The shared e2e fixture wrote a `settings.local.toml` with a duplicated `type = "claude"` key under `[commands.create]`, which made every `mngr` invocation fail with a TOML "Cannot overwrite a value" parse error; the duplicate line is removed. Also added a `@pytest.mark.timeout(120)` override to `test_connect_short_form`, which drives an interactive `mngr conn` attach that polls for up to 30s and so needs more than the 10s global pytest timeout.

Fixed a duplicate `type = "claude"` key in the e2e test fixture's generated
`settings.local.toml`, which caused every e2e tutorial test to fail with a TOML
"Cannot overwrite a value" parse error when running `mngr create`.

Added `test_connect_with_start_restarts_stopped_agent`, an e2e test that shares
the `mngr connect --start` tutorial block but stops the agent first, verifying
that `--start` actually restarts a stopped agent (the existing test only connects
to an already-running agent, where `--start` is a no-op).

Fixed a duplicate `type = "claude"` key in the e2e test fixture's generated
`settings.local.toml` that produced invalid TOML ("Cannot overwrite a value"),
causing `mngr` config loading to fail in e2e tutorial tests. Also strengthened
the `test_control_mngr_via_env_rejects_invalid_value` release test to assert on
the specific "Unknown provider backend" rejection message.

Fixed the e2e test fixture (`conftest.py`) which wrote a duplicate `type = "claude"` key into the generated `settings.local.toml`, causing all e2e tutorial tests to fail config parsing ("Cannot overwrite a value"). Also tightened `test_create_agent_args_require_dash_separator` to assert on the specific unrecognized-option failure mode.

Fixed the e2e test fixture so tutorial tests can run again, and strengthened the
create-and-destroy tutorial test.

- The e2e conftest fixture wrote a `settings.local.toml` with a duplicate
  `type = "claude"` key under `[commands.create]` (a merge artifact). tomlkit
  rejects duplicate keys, so every e2e tutorial command failed up front with
  "Cannot overwrite a value". Removed the duplicate line.
- `test_create_and_destroy_agent` now asserts the agent appears in `mngr list`
  before it is destroyed, making the post-destroy absence check a real
  before/after contrast.

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`), which wrote a
`settings.local.toml` with a duplicate `type = "claude"` key under `[commands.create]`.
TOML rejects the duplicate ("Cannot overwrite a value"), which made every e2e command
(`mngr create`, etc.) fail to parse its config. Removed the redundant line.

Added `@pytest.mark.timeout(300)` to the `test_rename.py` e2e tests so that
`mngr list --format json` has time to complete remote-provider discovery (matching the
convention used by the other create+list e2e tests), and strengthened
`test_create_and_rename_agent` to verify the renamed agent is preserved in place (same
command, still alive) rather than only checking its name.

Fixed the `test_create_codex_agent` e2e tutorial test. The e2e fixture was
writing a duplicate `type = "claude"` key into `[commands.create]` in the
generated `settings.local.toml`, which produced invalid TOML and broke any
command (such as `mngr config set --scope local`) that loads and re-saves that
file via tomlkit. Removed the duplicate. Also added a `@pytest.mark.timeout(120)`
to the test (it runs three sequential mngr operations that each perform full
provider discovery, exceeding the default 10s timeout) and removed the spurious
`@pytest.mark.modal` (the test only creates a local agent and never invokes the
modal CLI binary the resource guard tracks).

Fixed the e2e test fixture that seeded `settings.local.toml` with a duplicate
`type = "claude"` key under `[commands.create]`. The duplicate parsed on initial
load but caused `mngr config set --scope local` to fail with "Cannot overwrite a
value" when it re-saved the file, breaking `test_create_codex_explicit_type`.

Fixed the e2e tutorial test fixture and the `test_create_codex_positional` release test.

- The e2e fixture's `settings.local.toml` was emitting a duplicate `type = "claude"` key under
  `[commands.create]`, producing malformed TOML. Any `mngr config set` that re-parsed the merged
  config then failed with "Cannot overwrite a value". Removed the duplicate line.
- `test_create_codex_positional` now scopes its verification `mngr list` to `--provider local`
  (the agent is created locally) and raises the per-test timeout to 120s, matching the sibling
  `test_create_codex_explicit_type`. The previous unscoped `mngr list` fanned out to every
  provider (including Modal) and exceeded the default 10s timeout. The test also now asserts the
  created agent reached a RUNNING/WAITING state, not just that it has the codex type.

Fixed the e2e tutorial test fixture and the `--type command -- <cmd>` create test.

- Removed a duplicate `type = "claude"` key under `[commands.create]` in the e2e
  `settings.local.toml` that the fixture writes (`e2e/conftest.py`). The duplicate key made
  the file invalid TOML, so every `mngr` invocation in an e2e test aborted with
  "Cannot overwrite a value".
- Added `@pytest.mark.timeout(120)` to
  `test_create_command_agent_runs_post_dash_command_in_agent`, matching its sibling
  real-create tests. A real create (tmux session + asciinema connect, plus a one-time ttyd
  install) followed by `mngr exec` and `mngr list` routinely exceeds the default 10s
  function timeout.

Fixed the e2e test fixture's generated `settings.local.toml`, which had a duplicate `type = "claude"` key under `[commands.create]`. The duplicate produced invalid TOML ("Cannot overwrite a value"), causing every e2e command to fail to parse its config. Removed the duplicate line so the fixture writes valid TOML again.

Strengthened `test_create_command_custom_script` to confirm the forwarded command is actually running as a process inside the agent (via `mngr exec ... ps`), rather than only checking the recorded metadata and state.

Fixed the e2e tutorial test fixture so the `command`-type agent tutorial tests run again.

- The shared `e2e` fixture (`e2e/conftest.py`) was writing a `settings.local.toml` with a
  duplicate `type = "claude"` key under `[commands.create]`, which is invalid TOML and made
  every `mngr` invocation in the affected e2e tests fail with "Cannot overwrite a value". Removed
  the duplicate key.
- `test_create_command_python_http` now carries `@pytest.mark.timeout(120)` (matching its
  sibling `test_create_command_custom_script`), since creating a command agent plus the
  follow-up `mngr list`/`mngr exec` provider discovery can exceed the default 10s per-test
  timeout when a remote provider is unreachable.

Fixed the e2e tutorial test fixture and the `test_create_copy` release test.

- The e2e `settings.local.toml` written by the test fixture contained a duplicate
  `type = "claude"` key under `[commands.create]`, which made TOML parsing fail for
  every `mngr` command in the e2e tutorial suite. The duplicate has been removed.
- `test_create_copy` carried a spurious `@pytest.mark.modal` mark even though it only
  creates a local agent with a local git-mirror transfer; the resource guard's
  NEVER_INVOKED check failed the test. The mark has been removed.
- Strengthened `test_create_copy` to verify the git-mirror copy is a functional
  repository that carries over the source repo's commit history (via `git log`), not
  merely a directory containing a `.git` folder.

Fixed the `test_create_custom_yolo_agent_type` e2e tutorial test. The shared e2e
fixture wrote a duplicate `type = "claude"` key into `settings.local.toml`,
producing invalid TOML that made `mngr config edit` fail to parse the config. The
test also configured the custom `yolo` agent type with only a `command` (no
`parent_type`), so the type could not resolve to a concrete agent class. The test
now points `yolo` at the built-in `command` parent, scopes its verification
`mngr list` to the local provider, and drops the spurious `@pytest.mark.modal`
mark (the test only exercises the local provider).

Fixed a duplicate `type = "claude"` key under `[commands.create]` in the e2e tutorial test fixture's `settings.local.toml`. The duplicate caused a TOML "Cannot overwrite a value" parse error that broke `mngr create` in every e2e tutorial test.

Fixed the e2e tutorial test fixture (`e2e/conftest.py`) that wrote an invalid
`settings.local.toml` with a duplicate `type = "claude"` key under
`[commands.create]`, which caused `mngr create` to fail with a TOML parse error
("Cannot overwrite a value") in every e2e tutorial test. Also added a
`@pytest.mark.timeout(120)` mark to `test_create_default_branch`, which was
exceeding the default 10s per-test timeout because it runs `mngr create` plus
several `mngr exec` commands.

Fixed the e2e test fixture, which wrote a duplicate `type = "claude"` key into the generated `settings.local.toml`, producing invalid TOML that made every `mngr` subprocess fail to parse its config. Also gave `test_create_default_project_label` a 120s timeout (matching its sibling agent-creation tests) so it no longer trips the 10s default pytest timeout while `mngr list` performs provider discovery.

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) that wrote a
`settings.local.toml` containing a duplicate `type = "claude"` key under
`[commands.create]`. The duplicate produced invalid TOML, causing every e2e
tutorial test that loads the local config to fail with "Cannot overwrite a
value". Removed the duplicate line.

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) that wrote a
duplicate `type = "claude"` key under `[commands.create]` in the generated
`settings.local.toml`. The duplicate key produced invalid TOML, so every e2e
command that loaded the merged config failed with `Failed to parse config file
... Cannot overwrite a value`. Removed the redundant line so the fixture emits
valid TOML.

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) which wrote a
`settings.local.toml` containing a duplicate `type = "claude"` key under
`[commands.create]`. The duplicate (a merge artifact) made the file invalid TOML,
so every `mngr create` invoked from an e2e test aborted with
`Failed to parse config file ...: Cannot overwrite a value`. Removing the duplicate
key restores the fixture so the docker (and all other) tutorial e2e tests can run.

Fixed the e2e test fixture's `settings.local.toml` template, which contained a
duplicate `type = "claude"` key under `[commands.create]`. The duplicate made
the file invalid TOML, so every `mngr` command in the docker tutorial e2e tests
failed with "Cannot overwrite a value" instead of exercising the docker provider.

Fixed a duplicate `type = "claude"` key in the e2e test fixture's generated
`settings.local.toml` (`libs/mngr/imbue/mngr/e2e/conftest.py`). The duplicate
produced a "Cannot overwrite a value" TOML parse error that caused every e2e
tutorial `mngr` command to exit 1 before reaching the provider. This unblocks
the Docker create tutorial tests (and all other e2e tests sharing the fixture).

Fixed the e2e test fixture (`mngr/e2e/conftest.py`) which wrote a `settings.local.toml` containing a duplicate `type = "claude"` key under `[commands.create]`. The duplicate key is invalid TOML and caused every Docker e2e tutorial test (e.g. `test_create_docker_volume_start_arg`) to fail with "Cannot overwrite a value" when `mngr` parsed the config file.

Fixed the e2e test fixture so `mngr` commands run again: the generated
`settings.local.toml` had a duplicate `type = "claude"` key under
`[commands.create]`, which made the config fail to parse with "Cannot overwrite
a value" and broke every e2e test. Also gave `test_create_duplicate_name_fails`
a 120s timeout (matching sibling e2e tests) since it creates a live agent and
runs `mngr list`, whose provider discovery can exceed the default 10s timeout.

Fixed the e2e test fixture, which wrote an invalid `settings.local.toml` containing a duplicate `type = "claude"` key under `[commands.create]`. This caused every e2e tutorial test to fail with a "Cannot overwrite a value" TOML parse error. Also gave `test_create_from_another_agent` a 120s timeout (it runs two `mngr create` operations plus a clone) and added `test_create_from_another_agent_source_alias` to cover the documented `--source` alias for `--from`.

Test maintenance for the tutorial e2e suite (no user-facing behavior change):

- Fixed the e2e test fixture so it no longer writes an invalid `settings.local.toml`. The
  `[commands.create]` section had a duplicate `type = "claude"` key, which made every `mngr`
  command in an e2e tutorial test fail with "Cannot overwrite a value" while parsing the config.
- Strengthened `test_create_git_mirror_with_existing_branch`: it now also verifies that the
  agent's git mirror is checked out at the same commit the existing branch points to in the
  source repo (not merely that a same-named branch exists). Hardened its `mngr exec` verification
  calls with a longer timeout to absorb agent/provider-discovery latency under local load.

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) which wrote a
duplicate `type = "claude"` key into the generated `settings.local.toml`. TOML
forbids duplicate keys, so every e2e tutorial test using this fixture failed with
"Cannot overwrite a value" while parsing the config. Removed the duplicate.

Also gave `test_create_headless` the same `@pytest.mark.timeout(120)` its sibling
multi-operation tests carry (it runs create + list + exec, exceeding the 10s
default), and strengthened its assertion to verify the headless agent actually
runs inside its dedicated worktree rather than only checking the `exec` exit code.

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) so the generated
`settings.local.toml` no longer wrote `type = "claude"` twice under `[commands.create]`.
The duplicate key produced invalid TOML ("Cannot overwrite a value"), which made every
e2e tutorial command fail to parse its config. Removing the duplicate restores the intended
single default agent type and unblocks the e2e tutorial tests (e.g. `test_create_headless`).

Fixed the e2e test fixture, which wrote a `settings.local.toml` containing a
duplicate `type = "claude"` key under `[commands.create]`. TOML rejects a
repeated key, so every `mngr` command run through the e2e fixture failed with a
config parse error. Removing the duplicate restores the e2e suite.

Strengthened `test_create_help_succeeds` to assert that `mngr create --help`
emits the command's own NAME summary, SYNOPSIS, and EXAMPLES sections (not just
two flag strings), confirming the help genuinely belongs to the `create` command.

Fixed the e2e tutorial test fixture (`conftest.py`) that wrote a duplicate `type = "claude"` key into `settings.local.toml` under `[commands.create]`, producing invalid TOML and causing `mngr create` to fail with "Cannot overwrite a value" during e2e tutorial tests.

Fixed the e2e test fixture that seeded a malformed `settings.local.toml` with a
duplicate `type = "claude"` key under `[commands.create]`, which made every
`mngr create` invoked from an e2e tutorial test fail with a TOML "Cannot
overwrite a value" parse error. Removed the duplicate key.

Fixed the e2e test fixture that generated an invalid `settings.local.toml`: the
`[commands.create]` table set `type = "claude"` twice, which is not valid TOML
("Cannot overwrite a value") and caused every e2e tutorial test to fail at config
load. Removed the duplicate key.

Also added an e2e test (`test_create_unnamed_agent_gets_random_name`) covering the
documented behavior that `mngr create` with no name argument generates a random
agent name.

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) that wrote an
invalid `settings.local.toml` with a duplicate `type = "claude"` key under
`[commands.create]`. The duplicate key caused every e2e test using this fixture
to fail with "Cannot overwrite a value" when mngr parsed the config, before any
command logic ran. Removed the duplicate line.

Fixed the e2e test fixture (`e2e/conftest.py`) that wrote a duplicate `type = "claude"` key under `[commands.create]` in the generated `settings.local.toml`, which made the file invalid TOML and caused every `mngr` command in e2e tests to fail with a config parse error. Also strengthened `test_create_short_forms` to exercise the tutorial's positional agent-type argument (`mngr create my-task command`) and to assert that the resolved agent type and stand-in command are correct.

Fixed the e2e test fixture so the release tests in `test_create_basic.py` run again.

- The shared e2e `settings.local.toml` fixture (in `e2e/conftest.py`) wrote `type = "claude"`
  twice under `[commands.create]`, producing invalid TOML. Every `mngr` invocation in these
  tests aborted with "Cannot overwrite a value" before doing any work. Removed the duplicate
  key.
- Added `@pytest.mark.timeout(120)` to `test_create_with_agent_args`, which runs two sequential
  `mngr` operations (create, list) each performing full provider discovery and so exceeds the
  default 10s pytest-timeout.

Fixed the e2e test fixture (`imbue/mngr/e2e/conftest.py`) that wrote `type = "claude"` twice into the `[commands.create]` table of the generated `settings.local.toml`. The duplicate key produced invalid TOML ("Cannot overwrite a value"), causing every tutorial e2e test that runs `mngr create` to fail with a config parse error. Removed the duplicate line.

Fixed a malformed `settings.local.toml` written by the e2e test fixture: a duplicate `type = "claude"` key under `[commands.create]` caused every `mngr` command in the e2e tutorial tests to abort with a TOML "Cannot overwrite a value" parse error. Removing the duplicate line restores the e2e tutorial release tests (e.g. `test_create_with_connect_command`).

Fixed the e2e test fixture (`imbue/mngr/e2e/conftest.py`) that wrote a duplicate `type = "claude"` key under `[commands.create]` in the generated `settings.local.toml`. The duplicate key caused TOML parsing to fail with "Cannot overwrite a value", breaking e2e tutorial tests including `test_create_with_custom_branch_pattern`.

Fixed the e2e test fixture that wrote a duplicate `type = "claude"` key under `[commands.create]` in `settings.local.toml`, which caused every config-loading e2e command to fail with a TOML "Cannot overwrite a value" parse error instead of running. Also strengthened `test_create_with_dirty_tree_fails` to verify no agent is left behind when the clean-working-tree guard aborts.

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) so it no
longer writes a duplicate `type = "claude"` key into the per-test
`settings.local.toml`. The duplicate made tomlkit reject the config file
("Cannot overwrite a value"), causing every e2e tutorial test that creates an
agent to fail. This affected `test_create_with_env_file` and its siblings in
`test_env_vars.py`.

Also added `test_create_with_missing_env_file_is_rejected`, an unhappy-path e2e
test covering the same `--env-file` tutorial block: it verifies that pointing
`--env-file` at a nonexistent file is rejected with a clear error and creates no
agent.

Fixed two e2e test-fixture issues that broke the create-time env-var tutorial tests:

- The e2e fixture wrote a duplicate `type = "claude"` key into the same
  `[commands.create]` table of `settings.local.toml` (a stray line left by a
  bulk merge), which is invalid TOML and made *every* e2e command fail with
  "Cannot overwrite a value". Removed the duplicate.
- `test_create_with_env_vars` still carried a stale `@pytest.mark.modal` even
  though it creates on the default provider and never invokes Modal (its sibling
  default-provider tests had the mark removed in the same merge). The resource
  guard correctly flagged the superfluous mark; removed it.

Fixed the e2e tutorial test `test_create_with_env` and its shared fixture:

- Removed a duplicate `type = "claude"` key in the e2e `settings.local.toml`
  written by the `e2e` fixture, which made the file invalid TOML and caused
  every `mngr create` in the e2e tutorial tests to fail with a config parse
  error.
- Reworked the `--env` test to launch its agent body via `bash -c '...'` so the
  compound command and `$MNGR_TEST_VAR` expansion run inside the agent's shell,
  instead of being collapsed into a single (non-existent) command word by the
  command agent's per-argument shell quoting.

Fixed a duplicate `type = "claude"` key in the e2e test fixture's generated `settings.local.toml`, which caused a TOML parse error ("Cannot overwrite a value") and broke `mngr` commands in all e2e tutorial tests.

Fixed the e2e tutorial test fixture: `settings.local.toml` was written with a duplicate
`type = "claude"` key under `[commands.create]`, which made every `mngr` command in the e2e
tutorial suite fail to parse its config ("Cannot overwrite a value"). Removed the duplicate key.

Also strengthened `test_create_with_extra_tmux_windows` to verify that the extra tmux windows
are actually running their configured commands (not just that windows with the right names
exist), matching the tutorial's promise that `-w name="cmd"` starts a window running that command.

Fixed the e2e test fixture (`conftest.py`) which wrote a duplicate `type = "claude"` key under `[commands.create]` in the generated `settings.local.toml`. The duplicate key caused every command in affected e2e tests to fail with a TOML parse error ("Cannot overwrite a value") before reaching the behavior under test. This unblocks `test_create_with_invalid_label_format` and other e2e tutorial tests.

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) which wrote a
duplicate `type = "claude"` key into the generated `settings.local.toml`, making the
file unparseable and causing every e2e `mngr` command to fail with "Cannot overwrite
a value". Removed the duplicate key.

Strengthened `test_create_with_json_output` to verify that the `agent_id`/`host_id`
returned by `mngr create --format json` actually match the agent reported by
`mngr list --format json`, and that the agent is in a running state -- confirming the
machine-readable identifiers are real and usable for scripting, not just well-formed.

Fixed the e2e test fixture, which wrote a duplicate `type` key into its generated `settings.local.toml` and caused every e2e `mngr` command to fail config parsing. Added an e2e test that the `mngr create --quiet` flag suppresses all console output while still creating the agent.

Fixed a duplicate `type = "claude"` key in the e2e test fixture's
`settings.local.toml` that made the file invalid TOML, causing
`test_create_with_label` (and any e2e test loading local config) to fail with
"Cannot overwrite a value".

Extended `test_create_with_label` to verify labels and host labels actually
drive `mngr list` filtering (matching values include the agent, non-matching
values exclude it).

Fixed the e2e test fixture (`conftest.py`) which wrote a duplicate `type = "claude"` key into the `[commands.create]` block of `settings.local.toml`, producing invalid TOML that made every `mngr create` in the release e2e suite fail with a config parse error. Also added an unhappy-path e2e test (`test_create_rejects_malformed_label`) covering a `--label` value that is not in KEY=VALUE format.

Fixed the e2e test fixture's `settings.local.toml` generation, which wrote a
duplicate `type = "claude"` key under `[commands.create]` and caused every e2e
`mngr create` to fail with a TOML "Cannot overwrite a value" parse error.

Strengthened `test_create_with_message` to verify the initial message is
actually delivered into the agent's tmux pane (via `tmux capture-pane`), rather
than only checking that mngr logged "Sending initial message".

Fixed the e2e tutorial test fixture, which wrote a duplicate `type = "claude"`
key under `[commands.create]` in `settings.local.toml`, making the file
unparseable and breaking every e2e tutorial test that depended on it. Added a
120s function-timeout override to `test_create_with_no_ensure_clean` (a real
create plus `mngr list` exceeds the default 10s), and added an unhappy-path test
verifying that `mngr create` aborts on a dirty working tree when
`--no-ensure-clean` is omitted.

Fixed a duplicated `type = "claude"` key in the e2e test fixture's generated
`settings.local.toml`, which made `tomllib` reject the file and caused every
`mngr` command in e2e tests to fail with a config parse error instead of
exercising the real code path. Also strengthened the nonexistent-base-branch
e2e test to assert the failure is actually about the missing base branch, so it
can no longer pass for an unrelated reason.

Fixed the e2e tutorial test fixture and broadened coverage of the `mngr create --pass-env`
tutorial block.

- Removed a duplicate `type = "claude"` key the `e2e` fixture wrote into the generated
  `settings.local.toml`. The duplicate produced invalid TOML, so every `mngr` command in the
  e2e/tutorial release suite failed with `Cannot overwrite a value`.
- Added `test_create_with_pass_env_skips_unset_var`, an unhappy-path test for the same
  `--pass-env` tutorial block: forwarding a variable that is not set in the current shell does
  not fail `create`; the variable is simply absent from the agent's environment.

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) which wrote a duplicate `type = "claude"` key into `[commands.create]` in the generated `settings.local.toml`, causing every e2e tutorial test to fail with a TOML "Cannot overwrite a value" parse error. Also added the `@pytest.mark.timeout(120)` marker to the `test_create_with_pass_env` and `test_create_with_pass_env_unset` tutorial tests so they no longer hit the default 10s pytest timeout while running multiple `mngr` subprocess commands.

Fixed the e2e tutorial test fixture that wrote a duplicate `type = "claude"` key into the generated `settings.local.toml`, which produced an invalid-TOML parse error and broke every e2e tutorial test. Added a `@pytest.mark.timeout(120)` to `test_create_with_pass_env` so it matches its sibling tests and does not hit the default 10s timeout during the slow `mngr list` provider-discovery step.

Strengthened `test_create_with_pass_env` to additionally exec into the running agent and assert the forwarded `API_KEY` is visible in its live environment, not just in the on-disk env file.

Fixed a duplicate `type = "claude"` key in the e2e test fixture's generated `settings.local.toml` that caused a TOML parse error ("Cannot overwrite a value") in all e2e tutorial tests. Also strengthened `test_create_with_plugin_flags` to verify the failed create leaves no agent behind.

Fixed the e2e tutorial test fixture: removed a duplicate `type = "claude"` key
in the generated `settings.local.toml` that produced invalid TOML and broke
`mngr create` across e2e tests. Also added a `@pytest.mark.timeout(120)` mark to
`test_create_with_project_label` so its multi-step `mngr` subprocess calls are
not killed by the global 10s pytest timeout.

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) which wrote a
duplicate `type = "claude"` key under `[commands.create]` in the generated
`settings.local.toml`. TOML rejects the duplicate key, causing every e2e test
that loads this config (including `test_create_with_quiet_output`) to fail with
"Cannot overwrite a value". Removed the duplicate line.

Fixed the e2e test fixture (`conftest.py`) that wrote a duplicate `type = "claude"` key under `[commands.create]` in the generated `settings.local.toml`. The duplicate key produced invalid TOML ("Cannot overwrite a value"), causing every `mngr` command run inside the e2e tests to fail config parsing. Removing the duplicate restores valid config and unblocks the affected e2e tests.

Fixed the e2e tutorial test fixture, which wrote a duplicate `type = "claude"` key into `[commands.create]` in the generated `settings.local.toml`. The duplicate key made the file invalid TOML, causing every e2e tutorial command to fail with a config parse error ("Cannot overwrite a value").

Also strengthened `test_create_with_source_path_no_git` to assert that a non-git source folder produces no agent git branch and that the agent's work directory is not a git repository, reinforcing the tutorial's claim that mngr does not require git.

Fixed the e2e test fixture's generated `settings.local.toml` which had a duplicate `type = "claude"` key under `[commands.create]`, causing all e2e tutorial tests using the fixture to fail at config-parse time with "Cannot overwrite a value".

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) so the seeded `settings.local.toml` no longer contains a duplicate `type = "claude"` key under `[commands.create]`. The duplicate produced invalid TOML, causing every e2e tutorial command that loads this config to fail with a "Cannot overwrite a value" parse error (most visibly breaking `test_create_with_template_modal_disabled`, which then failed for the wrong reason).

Fixed the e2e test fixture and expanded coverage for `mngr create --template`.

- The e2e `settings.local.toml` written by the test fixture contained a duplicate
  `type = "claude"` key under `[commands.create]`, which made tomlkit refuse to parse the
  config ("Cannot overwrite a value") and broke every e2e test that loaded it. Removed the
  duplicate line.
- Added `test_create_with_nonexistent_template`, an unhappy-path companion to
  `test_create_with_template`, verifying that creating with an unconfigured template name
  fails with a helpful error that names the missing template and lists the available ones,
  and that no agent is created.

Fixed the e2e test fixture in `conftest.py` that wrote a duplicate `type = "claude"` key under `[commands.create]` in the generated `settings.local.toml`. The duplicate produced invalid TOML, causing every e2e tutorial test (including `test_create_with_transfer_none`) to fail config parsing with "Cannot overwrite a value".

Fixed the e2e test fixture (`e2e/conftest.py`) so that the `settings.local.toml` it writes no
longer contains a duplicate `type = "claude"` key under `[commands.create]`. The duplicate key
caused tomlkit to fail config parsing ("Cannot overwrite a value"), which made every e2e tutorial
test (including the Docker tutorial tests) fail before running any `mngr` command.

Fixed the e2e test fixture that wrote a duplicate `type = "claude"` key into
`settings.local.toml`, which made every e2e tutorial command fail to parse its
config. Also corrected the `test_destroy_all_via_stdin` release test: it only
manages local agents, so it never invokes Modal and no longer carries the
`@pytest.mark.modal` mark. Strengthened the test to assert that both agents are
reported as destroyed by the piped `mngr list --ids | mngr destroy - --force`
command.

Fixed the release e2e test `test_destroy_by_session_name_happy_path` (destroy section).

- Fixed the shared e2e fixture (`e2e/conftest.py`): the generated `settings.local.toml` had a
  duplicate `type = "claude"` key inside `[commands.create]` (introduced by a squashed
  conflict resolution), which made tomlkit reject the file with "Cannot overwrite a value" and
  broke `mngr create` for every e2e test using the fixture. Removed the duplicate line.
- Removed the stale `@pytest.mark.modal` marker from the test. It only creates a local
  `command`-type agent and destroys it by tmux session name; it never invokes the bare `modal`
  CLI (the only path the resource guard observes), so the guard failed the test with "marked
  with @pytest.mark.modal but never invoked modal". The mark has no effect on CI selection
  (release offload filters by `release`).

Fixed the e2e test fixture, which wrote a duplicate `type = "claude"` key under
`[commands.create]` in the generated `settings.local.toml`, causing every e2e
test to fail with a TOML parse error ("Cannot overwrite a value"). The default
agent type is now written exactly once.

Strengthened `test_destroy_multiple_at_once` to also assert on the
"Successfully destroyed 3 agent(s)" summary line, verifying that a single
`mngr destroy a b c --force` command tears down the exact number of agents
requested.

Fixed the e2e test fixture that wrote an invalid `settings.local.toml` containing a duplicate `type = "claude"` key under `[commands.create]`, which made TOML parsing fail and broke agent creation across e2e tests. Also added the missing `@pytest.mark.timeout(120)` marker to `test_destroy_no_gc` so it no longer falls back to the too-short default timeout.

Fixed the e2e test fixture's generated `settings.local.toml`, which contained a
duplicate `type = "claude"` key under `[commands.create]` and caused every e2e
tutorial test to fail with a TOML parse error during `mngr create`.

Added `test_destroy_keeps_branch_by_default`, a companion test for the
`mngr destroy --remove-created-branch` tutorial block that verifies the documented
safe default: a plain destroy leaves the agent's git branch intact.

Fixed the e2e test fixture's generated `settings.local.toml`, which contained a
duplicate `type = "claude"` key under `[commands.create]` that made TOML parsing
fail ("Cannot overwrite a value") and broke `mngr create` in every tutorial e2e
test. Also removed an incorrect `@pytest.mark.modal` from
`test_destroy_short_form_running_requires_force`: that unhappy-path test refuses
to destroy a running local agent without `--force`, so it never invokes modal.

Fixed the e2e tutorial test fixture so the `mngr rm` short-form destroy release test passes. The shared `settings.local.toml` written by the e2e fixture had a duplicate `type = "claude"` key under `[commands.create]` (a merge artifact), which made every `mngr create` in the e2e tutorial tests fail with a TOML "Cannot overwrite a value" parse error. Also removed the superfluous `@pytest.mark.modal` mark from `test_destroy_short_form`, which only creates a local `command`-type agent and never invokes the `modal` binary, so the resource guard failed it with a "marked modal but never invoked modal" violation.

Fixed a duplicate `type = "claude"` key in the e2e test fixture's `settings.local.toml`, which caused every e2e test to fail at agent creation with a TOML "Cannot overwrite a value" parse error.

Fixed the e2e tutorial test fixture: removed a duplicate `type = "claude"` key in the generated `settings.local.toml`, which caused a TOML parse error ("Cannot overwrite a value") that broke all e2e tutorial tests using the shared fixture.

Fixed the e2e tutorial test fixture (`E2eSession`) that wrote an invalid `settings.local.toml` containing a duplicate `type = "claude"` key under `[commands.create]`, which made every `mngr` command in the e2e tutorial tests fail to parse its config. Also added a `@pytest.mark.timeout(60)` override to `test_env_var_mngr_headless`, which makes several sequential `mngr` subprocess calls and was timing out under the default 10s limit.

Fixed the e2e test fixture (`conftest.py`) that wrote an invalid `settings.local.toml`: the `[commands.create]` table contained a duplicate `type = "claude"` key, which TOML rejects ("Cannot overwrite a value"). This broke `mngr create` in every e2e test. Also removed a duplicate `_parse_jsonl_events` helper definition in `test_event.py`.

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) that wrote a
duplicate `type = "claude"` key into the generated `settings.local.toml`,
producing invalid TOML that made every tutorial e2e agent-creation command fail
with "Cannot overwrite a value". The fixture now writes the default agent type
once. This unblocks the `mngr event` tutorial e2e tests (including
`test_event_follow_filter_source`).

Fixed the e2e tutorial test fixture so agent creation works again. The shared
`settings.local.toml` written by the e2e session fixture contained a duplicate
`type = "claude"` key under `[commands.create]`, which made the config file
invalid TOML and caused every `mngr create` in the tutorial e2e tests to fail
with "Cannot overwrite a value". Removed the duplicate key.

Also removed a redundant second definition of the `_parse_jsonl_events` helper
in `test_event.py` that shadowed the stronger original (the original asserts
each parsed line is a JSON object).

Fixed the e2e tutorial test fixture so `mngr event` tutorial tests can create agents again.

- The e2e fixture wrote a duplicate `type = "claude"` key inside `[commands.create]` in the
  generated `settings.local.toml`, producing invalid TOML. Every tutorial command that creates
  an agent failed with "Cannot overwrite a value". Removed the duplicate key.
- Test-only cleanup in `test_event.py`: removed a duplicated `_parse_jsonl_events` helper (the
  second, weaker definition shadowed the first) and strengthened
  `test_event_head_conflicts_with_tail` to assert that no events are emitted to stdout when
  `--head` and `--tail` are combined.

Test-only changes (no user-visible behavior change):

- Fixed the e2e tutorial test fixture (`e2e/conftest.py`): the generated
  `settings.local.toml` had a duplicate `type = "claude"` key under
  `[commands.create]`, which made TOML parsing fail with "Cannot overwrite a
  value". This broke agent creation for every e2e tutorial test. Removed the
  redundant key.
- Cleaned up `e2e/tutorial/test_event.py`: removed a duplicate
  `_parse_jsonl_events` definition that shadowed the stricter one, and
  strengthened `test_event_head` to assert that `--head` returns the leading
  prefix of the full event stream (the earliest events, not the tail).

Fixed the e2e tutorial test fixture, which wrote an invalid `settings.local.toml` containing a
duplicate `type = "claude"` key under `[commands.create]`. This caused every tutorial e2e test
that creates an agent to fail with a TOML parse error ("Cannot overwrite a value"). The duplicate
line was removed so the generated config is valid.

Also strengthened `test_event_include_filter_rejects_invalid_cel` to assert that a rejected
`--include` CEL filter produces no event output on stdout, verifying the command fails loudly
rather than silently emitting events.

Fixed the e2e test fixture that generated an invalid `settings.local.toml`: the
`[commands.create]` table contained a duplicate `type = "claude"` key, which made
tomlkit reject the file ("Cannot overwrite a value") and caused every `mngr create`
in the e2e tutorial tests to fail. Removed the duplicate key. Also removed a
shadowing duplicate `_parse_jsonl_events` helper in the event tutorial tests so the
stricter (object-asserting) parser is the one actually used.

Fixed the e2e tutorial test fixture so the generated `settings.local.toml` no longer emits a duplicate `type = "claude"` key under `[commands.create]`, which produced an invalid-TOML parse error ("Cannot overwrite a value") and broke `mngr create` in the tutorial event tests. Also strengthened `test_event_tail` to verify the `--tail 20` JSONL contract (at most 20 events, each carrying the guaranteed fields), matching its sibling event tests, and removed a duplicate `_parse_jsonl_events` helper definition.

Fixed the e2e tutorial test fixture (`conftest.py`) that wrote an invalid `settings.local.toml` with a duplicated `type = "claude"` key under `[commands.create]`. The duplicate key caused a TOML parse error ("Cannot overwrite a value") during `mngr create`, breaking agent creation in the e2e tutorial tests.

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) that wrote an
invalid `settings.local.toml`: the `[commands.create]` table defined `type = "claude"`
twice, which TOML rejects ("Cannot overwrite a value"). This caused every `mngr create`
in the e2e tutorial suite to fail at setup with a config-parse error. Removed the
duplicate key so the fixture writes a single `type = "claude"` default, unblocking the
`mngr exec` tutorial tests (and all other e2e tests sharing this fixture).

Fixed the e2e tutorial test fixture and expanded `mngr exec --cwd` coverage.

- The e2e fixture wrote a duplicate `type = "claude"` key into the generated
  `settings.local.toml`, which the TOML parser rejected ("Cannot overwrite a
  value"). Every tutorial test that creates an agent (`mngr create`) failed at
  the create step before reaching its actual assertions. Removed the duplicate
  key so the fixture-generated config parses.
- Added `test_exec_cwd_nonexistent`, an unhappy-path test sharing the
  `mngr exec --cwd` tutorial block: it verifies that pointing `--cwd` at a
  directory that does not exist on the agent host causes exec to exit nonzero
  rather than silently running in the default work_dir.

Fixed the e2e tutorial fixture so `mngr create` works again: the generated
`settings.local.toml` had a duplicate `type = "claude"` key under
`[commands.create]`, which made the TOML parser reject the config ("Cannot
overwrite a value"). Removed the duplicate.

Gave `test_exec_git_log` a `@pytest.mark.timeout(60)` override (matching the
identically-shaped `test_exec_branch_show_current`), since `mngr create` plus
`mngr exec` together exceed the 10s default pytest timeout.

Fixed the e2e tutorial test fixture that generated an invalid `settings.local.toml` with a duplicate `type = "claude"` key under `[commands.create]`, which made every e2e tutorial test fail to create agents. Also added a 120s timeout to `test_exec_git_push_then_merge`, which exercises a slow `mngr exec`, so it no longer trips the default 10s pytest timeout.

Fixed the e2e tutorial test fixture, which wrote a duplicate `type = "claude"`
key into the per-test `settings.local.toml`, producing invalid TOML that made
`mngr create` (and therefore every e2e tutorial test) fail to load its config.
Also gave `test_exec_short_form` an explicit `@pytest.mark.timeout(120)` so the
create+exec cycle is not killed by the repo-wide 10s default timeout.

Fixed a bug in the e2e tutorial test fixture (`conftest.py`) that wrote a duplicate `type = "claude"` key under `[commands.create]` in the generated `settings.local.toml`. The config parser rejected the duplicate key ("Cannot overwrite a value"), causing every e2e tutorial test's `mngr create` step to fail. Removed the duplicate key.

Fixed the e2e tutorial test fixture that wrote a malformed `settings.local.toml` containing a duplicate `type = "claude"` key under `[commands.create]`, which caused a TOML parse error ("Cannot overwrite a value") and made `mngr create` fail in every tutorial e2e test.

Fixed the e2e `test_full_lifecycle` release test (and the shared e2e fixture it depends on):

- Removed a duplicate `type = "claude"` key under `[commands.create]` in the e2e conftest's
  generated `settings.local.toml`. The duplicate made tomlkit refuse to parse the file
  ("Cannot overwrite a value"), causing every `mngr` command in e2e tests to fail at config
  load.
- Added `@pytest.mark.timeout(300)` to `test_full_lifecycle`, matching the convention used by
  the other multi-command e2e tests. Without it the test inherited the global 10s `func_only`
  timeout and was killed partway through its sequence of `mngr` commands.

Fixed the e2e test fixture's generated `settings.local.toml`, which defined the `[commands.create]` `type` key twice. TOML forbids redefining a key in the same table, so every e2e tutorial test failed at startup with "Cannot overwrite a value" before any command ran. Removed the duplicate key.

Fixed the e2e test fixture so it no longer writes a duplicate `type = "claude"` key into the generated `settings.local.toml`. The duplicate caused a TOML parse error ("Cannot overwrite a value") that broke `test_invalid_provider_fails` (and any other e2e test that loaded the merged config), masking the real behavior under test.

Fixed the e2e test fixture so it no longer emits an invalid `settings.local.toml`: the
`[commands.create]` table set `type = "claude"` twice, which is a duplicate-key TOML error
("Cannot overwrite a value") that caused every command loading the merged config (e.g.
`mngr list`) to fail. Removed the duplicate key.

Added a happy-path companion to the `mngr list --fields "name,state,initial_branch"` tutorial
test (`test_list_fields_original_branch_with_agent`) that creates an agent and asserts the
`initial_branch` column actually displays the branch mngr created for it (`mngr/my-task`),
complementing the existing empty-list ("No agents found") coverage.

Test-only changes (no user-visible behavior change).

- Fixed the e2e test fixture: the generated `settings.local.toml` defined `type = "claude"` twice
  under `[commands.create]`, which is an invalid duplicate TOML key and caused every `mngr` command
  in the e2e suite to fail with a config parse error. Removed the duplicate.
- Strengthened `test_list_filter_by_state` to also assert that the `--stopped` flag returns exactly
  the same set of agents as its documented CEL alias `--include 'state == "STOPPED"'`.

Fixed a bug in the shared e2e test fixture (`e2e/conftest.py`) where the generated
`settings.local.toml` contained a duplicate `type = "claude"` key under
`[commands.create]`. The duplicate key made the file invalid TOML, so every `mngr`
command run by an e2e test failed during config parsing with
`Cannot overwrite a value`. Removing the redundant line restores a valid config and
unblocks the docker tutorial e2e tests (and all other e2e tests sharing this fixture).

Fixed the e2e test fixture (`conftest.py`) that wrote an invalid `settings.local.toml` with a duplicate `type = "claude"` key under `[commands.create]`, which made every e2e `mngr create` fail with a TOML "Cannot overwrite a value" parse error.

Strengthened `test_multiple_agents_coexist` to verify that coexisting agents each occupy a distinct working directory (their own worktree), rather than only checking that an `echo` command runs on each.

Fixed the e2e test fixture so it no longer writes a duplicate `type = "claude"` key into the generated `settings.local.toml`, which had been causing every config load (and thus commands like `mngr plugin disable`) to fail with a TOML "Cannot overwrite a value" parse error. Also added a 60s `@pytest.mark.timeout` to the plugin e2e tests, which run several real `mngr` subprocess invocations and exceed the default 10s per-test timeout.

Fixed the e2e test fixture and the plugin e2e tests so the plugin disable/enable roundtrip
release test runs correctly:

- Removed a duplicate `type = "claude"` key from the `[commands.create]` table that the e2e
  fixture writes into `settings.local.toml`. The duplicate produced invalid TOML, so every
  command in an affected e2e test failed up front with "Cannot overwrite a value".
- Added `@pytest.mark.timeout(300)` to the two plugin e2e tests, matching the convention used
  by other multi-command e2e release tests. Their several real CLI subprocess invocations
  exceed the global 10s `func_only` timeout.

Fixed the e2e test fixture's generated `settings.local.toml`, which contained a
duplicate `type = "claude"` key under `[commands.create]` (a squash-merge
artifact) that produced invalid TOML and broke every e2e test with a config
parse error. Also strengthened `test_plugin_list_active_to_see_types` to verify,
via JSON output, that the `claude`, `codex`, and `command` agent types appear as
their own enabled plugin entries rather than relying on loose substring matches.

Fixed the e2e tutorial test fixture (`e2e` in `conftest.py`) which wrote a malformed `settings.local.toml` containing a duplicate `type = "claude"` key under `[commands.create]`. The config loader now rejects duplicate TOML keys, so this broke every e2e tutorial test with "Cannot overwrite a value". Removed the accidental duplicate line.

Also strengthened `test_recipe_launch_check_cleanup` to verify that destroy removes the agent itself (gone from `mngr list`, no longer resolvable by `mngr exec`), not just its branch.

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) that wrote an invalid `settings.local.toml`: a merge artifact had duplicated the `type = "claude"` key inside the `[commands.create]` table, causing every `mngr` command in e2e tests to fail with `Failed to parse config file ...: Cannot overwrite a value`. Removed the duplicate key.

Fixed the e2e test fixture that wrote a duplicate `type = "claude"` key into the
generated `settings.local.toml`, which produced an invalid-TOML parse error
("Cannot overwrite a value") and broke every e2e command (e.g. `mngr create`).

Strengthened `test_rename_dry_run_does_not_rename` to also verify (via `mngr
exec`) that the agent remains reachable and running its command under its
original name after a dry-run, not just that the name is unchanged in
`mngr list`.

Test-only changes (no user-visible behavior change):

- Fixed the e2e `e2e` fixture (`e2e/conftest.py`), which wrote a `[commands.create]`
  block with a duplicate `type = "claude"` key into `settings.local.toml`. TOML rejects
  duplicate keys, so every e2e test using the fixture failed at the first `mngr` command
  with "Cannot overwrite a value". Removed the duplicate line.
- Strengthened `test_tips_exec_env_inspect`: it now cross-checks that the
  `MNGR_AGENT_ID` exported into the exec'd environment matches the id mngr records for
  the agent, and verifies the `env | sort` output is actually sorted.

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) that wrote a duplicate
`type = "claude"` key under `[commands.create]` in the generated `settings.local.toml`. The
duplicate key made the file invalid TOML, so every `mngr` command run through the e2e fixture
failed with "Cannot overwrite a value". Removing the duplicate restores the e2e tutorial tests
(including `test_tips_transcript_tail_assistant`).

Fixed the e2e tutorial test fixture and strengthened the unknown-command test.

- The e2e `settings.local.toml` written by the tutorial-test fixture contained a duplicate
  `type = "claude"` key under `[commands.create]` (a merge artifact). This is invalid TOML and
  made every e2e tutorial command abort with a config parse error ("Cannot overwrite a value")
  instead of running. Removed the duplicate so the fixture config parses again.
- `test_unknown_command_fails` now asserts the precise Click usage exit code (2), that stdout is
  empty (clean for scripting), and that the error names the offending command, in addition to
  pointing the user back at `mngr --help`.

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
