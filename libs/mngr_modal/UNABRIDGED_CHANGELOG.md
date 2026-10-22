# Unabridged Changelog - mngr_modal

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_modal/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-10-20

Marked eight Modal acceptance tests `@pytest.mark.flaky`, so offload retries them instead of turning CI red on each occurrence. The 2026-10-19 flake sweep found each one recovering on a retry, on `main` or on more than one branch that does not touch the code it exercises, and none carried the marker:

- `test_snapshot_and_shutdown_missing_host_id`, `test_snapshot_and_shutdown_nonexistent_sandbox` and `test_snapshot_and_shutdown_nonexistent_host_record`: the Modal HTTP frontend answers the deployed endpoint's request itself (`404 modal-http: invalid function call`, 500, read timeouts), tracked as MIND-219.

- `test_get_and_set_host_tags`, `test_create_host_creates_sandbox_with_ssh`, `test_delete_nonexistent_snapshot_raises_error` and `test_get_host_resources`: a Modal volume or sandbox-terminate call with no deadline of its own stalls past the test's timeout (MIND-290), or a storage 503 outlasts the retry budget (MIND-477).

- `test_modal_still_reports_a_failed_build_terminating`: Modal's logs API rate-limits the build-log fetch, which does not retry it (MIND-480).

The markers are triage while those tickets are open, not fixes.

Configuring a host's sshd now logs, at info, the fingerprints of the keys that are about to replace the host's `authorized_keys` (the client key and any appended entries), along with the host id, provider and profile. A later key rejection can then be matched against the keys the host was last configured with.

## 2026-10-18

A Modal provider instance can now name a profile of this machine's `~/.modal.toml` instead of carrying a token: set `modal_profile = "<name>"` on the `[providers.<instance>]` block (mutually exclusive with `token_id` / `token_secret`). The profile's token pair is read from the file (`MODAL_CONFIG_PATH`, else `~/.modal.toml`) every time the provider is built rather than through the Modal SDK, which loads the file once at import, so a long-lived `mngr observe` sees a profile created by `modal token new --profile <name>` on its next check.

A profile missing on this machine (no file, no such section, or no token pair in it) raises the new `ProviderCredentialSourceMissingError` with that `modal token new` command as the fix, and discovery keeps re-checking the provider until the profile exists. The app-context registry keys on the token the profile supplied, so two instances on different profiles never share one Modal app context.

`imbue.mngr_modal.config` now exposes `modal_config_path()` and `read_modal_profiles()`, the location and reader of the Modal profiles file, so other tools (the Imbue Studio add-connection modal) list its profiles through the one reader that the backend's own lookup uses.

The Modal provider reports its create steps through mngr's structured progress stream (`--format jsonl`): `modal.build` and `modal.wait_ssh` under the create's `creating_host` phase. The image build advances through its Dockerfile instructions by watching the `=> Step N:` lines Modal's builder streams, counted against the Dockerfile's instruction count when the build was driven by one. Each instruction is also reported as a step of its own under the build (named by its text, as the docker provider names Dockerfile stages), as is the image save at the end, so a consumer can learn how long each one takes.

Testing integration branch combining three in-flight changes (each branch's own entry sits beside this one in the projects it touches): Imbue Cloud machine resizing from the workspace settings page (`mngr-allow-imbue-resizing`), shared-workspace cleanup (`mngr-cleanup-shares`), and cloud connections backed by a local login instead of pasted keys (`mngr-allow-provider-profile-vars`).

For this project: a Modal provider instance can name a profile of this machine's `~/.modal.toml` (`modal_profile`) instead of carrying a token, read fresh on every provider build, and `imbue.mngr_modal.config` exposes the profiles reader.

## 2026-10-17

The Modal provider's SSH key material now lives per provider instance, at `<profile>/providers/modal/<instance>/keys/` (per-host material under `keys/host_keys/<host uuid hex>/`, the pins in `keys/known_hosts`), the same shape as the vps providers' key dir. Sandboxes created under the old `providers/modal/` root are no longer reachable from that machine (their material is not read any more); Modal sandboxes cycle within about a day, so recreate them.

A `DIRECT` Modal provider instance can now carry its own credentials via `token_id` / `token_secret` in its provider block (or `MNGR__PROVIDERS__<NAME>__TOKEN_ID` / `..._TOKEN_SECRET`), independent of `MODAL_TOKEN_*` and `~/.modal.toml`, so several instances can target different Modal accounts. Both fields must be set together. App contexts are registered per (app name, token id), so same-named apps under different tokens never share a context, and the not-authorized help text mentions the new fields. A release test proves an instance authenticates with its configured token and nothing else. The Modal release Trip 4 now expects the curated not-authorized help to name `modal token new` (the documented command the help text has always used) instead of `uvx modal token set`, which it never mentioned.

Part of the account-owned remote providers program (`specs/account-owned-providers/spec.md`); the entries `mngr-per-client-host-keys.md` and `mngr-account-owned-providers-part-4.md` in this directory belong to the same PR.

- The README's SSH key material section names the per-instance `keys/known_hosts` store the code uses (it previously called it provider-wide).

- A provider instance's `token_id` and `token_secret` may come from different config layers (for example the id in `settings.toml` and the secret from the environment): the pairing is checked on the merged config when the instance is built, not per layer, which had rejected that split at startup.

- The client key that opens a sandbox resolves per host first, then to the instance's shared `keys/modal_ssh_key` when one is installed, and is never minted on a read path (`resolve_modal_client_keypair`): a sandbox this machine holds neither key for fails with `ModalHostKeyMissingError`, which names both paths and says the sandbox was created on another device or before per-host keys existed, so it must be reached from the device that created it or have its record synced here. Only `create_host` mints the per-host pair, so a connect no longer leaves behind a key the sandbox never authorized (one that would then have won over a shared key installed later); discovery lists such a sandbox from its record, at info level, instead of failing. The shared key is never created by mngr; it is the operator-installed key the TMR CI and mngr-seer debug flows have their sandboxes authorize, which the per-host-only resolver had stopped presenting.

- The per-host key layout is published as `MODAL_HOST_ACCESS_MANIFEST` (`constants.py`, with the key-file names `MODAL_SSH_KEY_NAME`, `MODAL_HOST_KEY_NAME` and `MODAL_KNOWN_HOSTS_NAME`), which the provider's own key dir and key paths resolve through; the deploy-time list of profile files kept out of images is built from the same names.

- The README's SSH key material section says that neither key is minted on a read path and that sandboxes created before the per-instance key dir existed are not reachable after upgrading: destroy and recreate them.

## 2026-10-16

The modal provider reports that it cannot re-read one host on its own (`supports_incremental_host_discovery` is false), since it reads every host in one batched pass, so the streaming discovery poller's new between-poll host re-reads skip it.

## 2026-10-15

Deploying a Modal route endpoint no longer races other deploys to create the app it publishes into.

Creating a host makes sure its Modal app publishes mngr's `snapshot_and_shutdown` endpoint, deploying it when the app does not already carry the current version. On an app that does not exist yet, concurrent creates all deploy at once, and Modal's app creation inside `modal deploy` is not atomic: the deploys mint rival apps and then fight over the one name. That refusal is neither of the two concurrent-modification shapes the deploy-lock retry rides through, so the losers simply fail, and the name can end up owned by an app that a given deploy never published into.

The deploy now takes the app through Modal's atomic get-or-create before publishing into it, so a deploy is never what brings an app into existence. Measured against real Modal, concurrent first deploys of one app went from 5 failures in 6 to 50 successes in 50.

Fixed: a `snapshot_and_shutdown` deploy that was the first thing to touch its app's state volume created it at Modal's default version, and every host create in that app then failed to open it -- `get_volume_for_app` asks for version 2 and Modal refuses a mismatch. The route script now requests the version the backend opens, so whichever side creates the volume first, the other can use it. The backend had always created it first until the endpoint was published ahead of the creates, which is when this surfaced.

## 2026-10-14

Marked four Modal acceptance tests `@pytest.mark.flaky`, so offload retries them instead of turning CI red on each occurrence: `test_list_snapshots_returns_initial_snapshot`, `test_start_host_restores_from_snapshot`, `test_start_host_on_running_host` and `test_start_host_clears_stop_reason_on_resume`. All four flake in CI and none carried the marker. MIND-479's fix retired one failure mode from each of them, but each has two to five more under open causes -- Modal answering 503 for longer than the retry budget (MIND-477), a resumed sandbox dropping its first SSH connection, and host-record creation stranding -- so the marker is triage until those are fixed, not a verdict on the tests.

Removed a duplicated `@pytest.mark.flaky` on `test_mngr_create_transfers_git_repo_with_untracked_files`. Two flake sweeps had each added the marker independently, leaving one bare copy above the comment that explains it and a second one below. Test selection and offload's flake retries are unchanged -- pytest already treated the pair as one mark.

## 2026-10-13

A `mngr` command that stalls in a Modal acceptance test now says which step it was on when it was killed.

Modal bring-up has several steps that can each outlast the test running them -- the SSH tunnel wait is handed the whole sandbox timeout, and the Modal calls that create the sandbox and write the host record have no deadline of their own. When one of them stalled, the test lost the evidence twice over: `subprocess.run` handed its partial capture to a `TimeoutExpired` that reported only how long it waited, and pytest-timeout's signal abandoned the call mid-wait. Seven CI branches hit the resulting 300s `mngr create` hang over three flake sweeps without the cause ever being named (MIND-487).

Two changes. Each Modal call a create can stall on now announces itself before it blocks, so the step is on the record by the time anything kills the command: the wait for a new sandbox to run its first command, the wait for its SSH tunnel, and the host-record write to and read from the state volume. And every `mngr` command a Modal acceptance test runs goes through one helper that, whenever the command has to be killed, reports the step timeline the command recorded in its own event log -- which survives both kinds of kill, because it is on disk and flushed per line. A command that overruns its own timeout reports by raising, so the steps land in the failure's message: that is what reaches junit, and so the flake sweeps and the GitHub annotation, neither of which reads a test's captured output. The message keeps the timeout's own wording as its first line, so the sweeps' display header is unchanged.

Nothing about `mngr create` itself changed, and no budget moved.

The group headers in `test_ratchets.py` change from the banner form `# --- Code safety ---` to `# ratchet_group("Code safety")`, a marker the ratchet sync script reads. The script now also keeps every test under the group its check has in `standard_ratchet_checks.py`, and the groups in that file's order. Here that moved the `Modal images` group up to its place after `Process management`. No ratchet, recorded count, or test name changes.

A running agent on a Modal host no longer reads as STOPPED for a moment while it is being renamed: the host listing now finds each agent's tmux session by the agent id the session carries, not only by its name.

Fixed `mngr create --new-host` failing on Modal for any Dockerfile that consumes a build arg, which includes mngr's own shipped `resources/Dockerfile`.

The Modal provider applied each Dockerfile instruction as its own `dockerfile_commands` call, so that a failed build could reuse the layers before it. Modal builds each such call as a separate stage (`FROM <previous layer>` plus the instruction), and Docker scopes an `ARG` to the stage that declares it, so a build arg expanded to empty in the `RUN` that used it. `ARG NODE_VERSION="24.15.0"` followed by a Node download therefore requested `https://nodejs.org/dist/v//node-v--linux-x64.tar.gz` and failed the build on a 404, and `ARG CLAUDE_CODE_VERSION` failed silently: the install fell back to the latest release instead of the pin, and `ENV CLAUDE_CODE_VERSION=${CLAUDE_CODE_VERSION}` baked an empty value into the image. `-b docker-build-arg=KEY=VALUE` was inert for the same reason. `ENV` was never affected, since it is image config that the next stage inherits.

Instructions are now grouped into one batch per cached layer, each ending at the `RUN`, `COPY` or `ADD` that makes the layer worth keeping, with every `ARG` repeated at the head of each later batch so it stays in scope for the rest of the build. mngr's default Dockerfile goes from 23 build calls to 15.

`-b docker-build-arg=KEY=VALUE` is now supplied to Modal's builder as a real build arg, rather than by rewriting the `ARG KEY=<default>` line in the Dockerfile text before the build. An override still applies to any `ARG` the Dockerfile declares, with or without a default; what changes is that the value is no longer spliced into the Dockerfile text between double quotes, so a value containing a `"`, a `$` or a backslash no longer corrupts the instruction it lands in. Naming an arg the Dockerfile does not declare is still an error. An override for an `ARG` that the `FROM` line interpolates still selects the base image, since the build args now also reach the Dockerfile parser that resolves it.

Changing a `-b docker-build-arg` value now rebuilds every layer rather than only those from the declaring `ARG` onward, because the build args form part of the cache key of each batch. Separately, the regrouping into batches invalidates the existing Modal layer cache once, so the first build on this version re-runs from the `ARG`-carrying layers down.

## 2026-10-12

Fixed: `mngr create` on Modal no longer fails outright when Modal fails the `snapshot_and_shutdown` endpoint's deploy for a reason of its own -- an image build losing the worker it was running on, or a deploy that hangs past its 180s attempt (MIND-452).

Before this, `ensure_function_deployed` retried only Modal's per-app deploy lock, so during a Modal incident on 2026-10-05 every create that needed the endpoint failed on its first attempt, across three CI runs. It now retries every failure Modal produced without judging the script, under the same elapsed-time budget, re-checking between attempts whether some other deploy has already published this source.

- The retry's constants and `ensure_function_deployed`'s budget parameter drop `lock` from their names (`DEPLOY_RETRY_BUDGET_SECONDS`, `DEPLOY_RETRY_MIN_BACKOFF_SECONDS`, `DEPLOY_RETRY_MAX_BACKOFF_SECONDS`, `retry_budget_seconds`): the lock is now one of several reasons they cover. `DEPLOY_MAX_DURATION_SECONDS` is unchanged.

- The error raised when the budget runs out names the failure that was actually repeating, instead of always reporting a locked app.

- The `test_snapshot_and_shutdown` acceptance tests deploy through `ensure_function_deployed`, the entry point a create uses, so they survive the same Modal-side failures a create does.

Modal discovery now reports each host's recorded size (`DiscoveredHost.size`: the sandbox's CPUs and memory, as `mngr list` reports `host.resource`) from the host records it already reads, so the `mngr observe` stream carries it; a running sandbox whose record has not propagated yet carries none.

## 2026-10-07

Fixed: A `mngr create ... @.modal` that stalls now says what it is waiting for, and concurrent creates against a cold Modal app no longer queue behind each other's identical deploys (MIND-337).

Creating a Modal host waits for the app's `snapshot_and_shutdown` endpoint to be deployed, for up to 495 seconds. That endpoint belongs to the Modal app rather than to the host being created, so on an app that carries no deploy yet every concurrent create finds the source marker missing, every one of them deploys, and they queue behind Modal's per-app deploy lock.

That wait used to be logged below the default level, so the last thing a stalled create said was `Waiting for sshd to be ready...` -- the step before it, which had already finished. Every report of a Modal create hanging therefore read as a bring-up readiness problem, including this ticket and MIND-314 before it. Measured against real Modal, `_wait_for_sshd` takes about 3 seconds of its 60-second budget.

- `ensure_function_deployed` now re-checks the source marker on every attempt, not just the first. A create whose deploy is refused the app lock adopts the endpoint that the winning deploy published, rather than taking the lock again to publish the same source and making every create still queued behind it wait for that too. It also owns the lock retry, which `imbue.modal_proxy.direct.deploy` used to perform internally.

- A create that does not get the endpoint URL within 60 seconds now says `Waiting for app <app>'s Modal snapshot endpoint to finish deploying ...`, and reports how long it waited once the wait completes.

- Spending the whole budget now raises an error naming the app and the budget, instead of `Future.result`'s empty `TimeoutError`.

- Nothing is logged when the endpoint is already deployed, which is every create after the first against a given app.

Fixed: creating or restarting a Modal host no longer fails outright when Modal ends the sandbox while mngr is still bringing it up.

mngr already probed a freshly created sandbox with a shell no-op and, if it did not run, discarded the sandbox and created another. But it recognized only one of the two ways Modal answers for a sandbox it has lost: while the container is dead but still addressable, the probe is accepted and comes back SIGKILLed, which mngr handled. Once Modal's control plane had dropped the task, the probe call was refused instead -- `Modal Sandbox with container ID ... not found. This means this Sandbox has already shut down`, or `Modal Sandbox is shutting down` -- and that answer escaped the probe rather than being reported by it, so it skipped the replacement and surfaced as `Failed to create Modal host: ...`.

Both of Modal's answers now mean the same thing to the probe, so both reach the replacement that was already there. Nothing waits on a sandbox that is gone: each attempt creates a fresh one.

Added: `test_modal_refuses_a_command_for_a_sandbox_it_has_ended`, an acceptance test that holds Modal to the wording mngr reads as "this sandbox is gone". Only the wording tells that refusal from a resource that genuinely never existed, so if Modal rewords it this test fails and names the pattern to update.

Fixed: a host bring-up command that Modal refuses to run -- because it has ended the sandbox since mngr created it -- is now reported as the sandbox being gone rather than as that command failing. Bring-up already attributed a SIGKILLed command to the sandbox rather than the command; a refused one never got far enough to be attributed at all.

## 2026-10-05

Threads this plugin starts on a `ConcurrencyGroup` now go through `start_mngr_thread`, which destroys the thread's gevent Hub when it finishes. A thread that ran a host command used to leave its Hub, and the pipe pair it holds, open for the rest of the process's life. A new `test_prevent_raw_start_new_thread` ratchet keeps raw `start_new_thread` calls out.

## 2026-10-01

Added `register_modal_test_fixtures`, which registers this package's Modal test fixtures into a consuming package's conftest namespace -- the same shape as mngr's `register_plugin_test_fixtures`.

It replaces `pytest_plugins = ["imbue.mngr_modal.conftest"]`, which pytest 9.1 refuses to honour outside the rootdir conftest. mngr_claude was the only consumer.

## 2026-09-30

Removed the 11 comment lines that had accumulated inside `libs/mngr_modal/imbue/mngr_modal/test_ratchets.py` explaining why individual ratchet counts had been raised.

A ratchet file records counts, not history. The reason a count moved now goes in the commit message and the changelog entry, the reason a particular code site is allowed goes in that module's docstring, and the set of exempt files goes in the `excluded_patterns=(...)` argument -- all places that stay correct as the code changes, which the comments did not.

## 2026-09-29

A Modal control-plane blip no longer takes a whole `mngr` command down with it.

Modal answers some failures with an error that means "Modal failed your request" rather than "here is your answer" -- a cancelled call, a deadline exceeded, the service unavailable. mngr did not recognize those as transient, so a blip on Modal's side surfaced three ways it should not have: a state-volume read got no retry, the resulting error slipped past the guards that exist to degrade gracefully and crashed the caller instead, and a Modal that was merely erroring at startup was treated as a hard failure -- which failed every *other* provider the user had, on a command that may not have been about Modal at all.

Such a failure is now treated the same as a Modal that could not be reached: the operation is retried, and if it keeps failing the Modal provider is reported as unavailable while the rest of the command carries on. The message says Modal did not answer, rather than blaming the network specifically.

`test_get_host_by_name` is marked flaky so CI retries it while MIND-274 is open (MIND-274).

Every Modal sandbox boot, fresh or restored from a snapshot, gives `/tmp` its sticky bit back (the conventional `1777`). Modal drops the bit when an image build step writes under `/tmp` and again when `snapshot_filesystem()` captures a changed `/tmp`, which left `/tmp` world-writable without it; software that checks its socket directory, such as codex 0.157's app-server, refuses to use such a `/tmp`.

## 2026-09-27

Modal's snapshot-and-shutdown endpoint now waits for the sandbox to actually stop before
reporting success. Modal's terminate call is fire-and-forget, so the endpoint used to answer
"success" the instant it asked for a shutdown -- leaving a sandbox that was still running, and
still billing, while mngr believed it was gone. The response now also carries the sandbox's
exit code, which is the proof that the shutdown was observed rather than merely requested.

## 2026-09-25

A transient Modal failure during discovery no longer fails the command (MIND-312).

`mngr list`, `mngr create` and anything else that discovers Modal hosts read the Modal control plane on every run -- listing sandboxes, reading each sandbox's tags, looking up the app. None of those reads were retried, so a single transient status from Modal produced `Error: Discovery failed for provider 'modal': please contact support@modal.com (Error code: ...)`. Those reads now wait out a blip for up to 60 seconds; see the modal-proxy changelog for the details.

The trade is latency for survival: while Modal is erroring, a command that used to fail in a second can now take up to a minute instead. Each retry logs a warning naming the error and the elapsed budget, so a slow run says why it is slow.

## 2026-09-24

The Modal listing tests now use the shared listing script's new output format, where activity times and tmux panes are listed for all agents at once. Modal listings behave as before.

## 2026-09-22

Removed a way `mngr create` on Modal could fail outright: `Error: SSH server not ready after 60.0s at <host>:<port>` (MIND-234).

Every `mngr create` takes an "initial" snapshot of the new host at the end. Recording that snapshot used to resolve a host of its own and open a second SSH connection to write the snapshot record -- a fresh handshake, right after a filesystem snapshot that can leave the sandbox's tunnel unable to complete one. The code waited for the tunnel with a 60-second sshd readiness probe, and when the tunnel did not answer within that, the create failed and the new host was torn down.

Recording a snapshot now uses the host the caller already holds, connected before the snapshot runs. A connection opened beforehand keeps working straight through a snapshot, so there is nothing to wait for: the readiness probe is gone, and so is the second SSH connection every create used to open (about six seconds per create).

`mngr snapshot create` and the pre-termination snapshot in `mngr stop` take the same path and get the same fix.

## 2026-09-21

Fixed `mngr create` on Modal intermittently failing with `Failed to deploy snapshot_and_shutdown function: ... (concurrent modification)` or `Lookup failed for Function 'snapshot_and_shutdown'` when several agents are created at once (MIND-262).

Creating a host used to re-publish the `snapshot_and_shutdown` web endpoint every single time. That endpoint belongs to the Modal *app*, not to the host, and its URL is the same for every host in the app, so the redeploy achieved nothing except taking Modal's per-app deploy lock. Concurrent creates then knocked each other over: the losers failed their deploy outright, and a create whose lookup landed during someone else's publish found no endpoint at all. In CI, where a whole acceptance run shares one app, this was the largest single cause of red Modal runs.

Each deployed route script now also publishes a beacon function whose name is a digest of the script it was deployed from. Because a deploy replaces an app's function set as a unit, that name resolves if and only if the app's current deployment came from exactly that source. `mngr create` looks for it and deploys only when the app is not already carrying this exact endpoint, so in steady state the deploy lock is never taken. The check fails closed -- a missing beacon, a beacon for other source, or an endpoint that does not resolve all deploy -- so an upgraded mngr still republishes, and no endpoint URL is used that a lookup has not just confirmed.

Agent listings from this provider now report the branch an agent's work_dir is
actually on.

mngr broadened `AgentDetails.initial_branch`: it used to name only a branch mngr
created, and is now the branch the work_dir was placed on either way, including
one that already existed and was merely checked out. This provider builds
`AgentDetails` by hand from the agent's `data.json` in its bulk-listing path
rather than going through the shared builders, so without this change its online
listing would have kept reporting the old narrower value while its offline path
reported the new one -- the answer would have changed with host state.

It reads through the shared `read_checked_out_branch` helper, so the fallback for
records written before the underlying field existed is applied identically
everywhere rather than restated per provider.

Parallel fan-outs in this plugin now run on `mngr_executor` instead of a raw `ConcurrencyGroupExecutor`, so each worker thread's gevent Hub is destroyed when its task finishes. Before, a worker that ran a host command (pyinfra reads command output with gevent) left its Hub behind, holding a pipe pair and the task's objects for the rest of the process's life. A new `test_prevent_raw_concurrency_group_executor` ratchet keeps new raw executors out.

## 2026-09-18

The Modal provider's test suite no longer touches the machine it runs on. `test_create_host_raises_on_ssh_setup_failure` drove the real `create_host` bring-up through the local-executing `FakeSandbox` and relied on it failing "as a non-root user"; run as root (the vendored copy inside a minds workspace container, during a workspace update) it succeeded instead, replacing the container's sshd host key and root `authorized_keys` and apt-installing packages, which left the workspace permanently "unresponsive" for its owner. The test now uses a sandbox fake that fails the SSH configuration step without executing anything, and new tests pin down that every production bring-up command (package install, SSH configuration, sshd start) is refused by the fake before it runs, and that `create_host` through the plain fake stops at the first bring-up command.

## 2026-09-16

`mngr create` now shows you why your Dockerfile build failed, even when Modal tells it nothing.

Modal streams a build's output as it happens, and that stream was the only thing `mngr create` had to report when a build failed. The stream is best-effort: when Modal's build-failure result is ready before it opens the stream, nothing is streamed at all. `mngr create` would then fail with only Modal's bare "Image build for im-xxxx failed. View the build logs: modal image logs im-xxxx" -- no sign of which line of your Dockerfile broke, which is the one thing you needed.

In that case `mngr create` now asks Modal for the failing layer's output and reports it, both in the error it prints and in the failed host record that `mngr list` reads. A build whose output did stream is left alone, so it is not printed back to you a second time.

## 2026-09-15

Fixed: Stop blaming package installation when Modal kills the sandbox during host bring-up (MIND-235).

- `mngr create`/`mngr start` on Modal now check that a newly created sandbox actually runs a command before provisioning it, and replace it (up to three tries) when it does not. Modal kills a small fraction of sandboxes on the way up, and every command queued against a dead sandbox comes back SIGKILLed (exit 137), so host creation used to fail outright on an infrastructure hiccup.

- A bring-up step that fails because the sandbox itself is gone now raises `ModalSandboxDiedMngrError` naming the sandbox and its exit code, instead of "Failed to install required packages (exit code 137)" -- a message that was misleading, since the default image already ships those packages and apt never ran.

## 2026-09-12

Integration branch for the imbue_cloud slice-fleet generation 2 program (raw qemu slices with routed-tap networking on Debian 13 boxes, replacing lima), merged together with `main` and the three pre-cutover fix PRs: imbue-ai/mngr-internal#857 (SSH certificates from Vault, #850), #855 (DHCP placement, #849) and #856 (the upstream artifact mirror, #851). The per-change details live in this directory's constituent entries, listed below.

Persistent Modal host creation waits for the snapshot-function deploy for as long as `modal deploy` can legitimately block behind Modal's app lock.

Constituent entries: `mngr-mirror-upstream-artifacts.md`

- Persistent Modal host creation now waits for the snapshot-function deploy for as long as `modal deploy` can legitimately block behind Modal's app lock, instead of a fixed two minutes. Creates fanned out concurrently against one shared app name no longer fail with a bare timeout while their deploy is still queued behind the lock.

## 2026-09-11

Fixed the Modal provider reporting "this host has no volume" when Modal was merely busy (MIND-244). `get_volume_for_host` decided whether a host's volume existed by listing its files, which goes through Modal's `VolumeListFiles` API. That API has a per-workspace rate limit shared by every concurrent client, so unrelated load elsewhere in the workspace made an existing volume look absent -- and callers that read agent state off the volume (`mngr file`, the events reader, mapreduce output pulling) silently degraded to "no volume available".

Existence is now settled by resolving the volume's id, which is the operation that actually answers the question, is unaffected by the listing rate limit, and is strongly consistent.

The two host-volume acceptance tests no longer poll for the volume to appear. Creating a sandbox resolves every volume it mounts, so the volume exists by the time `create_host` returns and the tests assert that directly. They also now check that the in-sandbox write they depend on actually succeeded, so a failed write no longer masquerades as a volume-propagation timeout. Both remain marked flaky for an unrelated reason: fresh-sandbox bring-up (MIND-234).

## 2026-09-10

mngr_modal: the Modal test-environment reaper now lives in this plugin instead of base mngr.

`imbue.mngr.utils.modal_cli` and the "Modal test environment cleanup utilities" section of `imbue.mngr.utils.testing` were Modal-specific code sitting in the provider-agnostic core, which ships to PyPI without this plugin. They are now `imbue.mngr_modal.modal_cli` and `imbue.mngr_modal.cleanup`, the latter alongside `imbue.mngr_vultr.cleanup`, which is the same age-based test-resource reaper for Vultr. `ModalCliOutputError` moved with them, from `imbue.mngr.errors` into this plugin's own `ModalMngrError` hierarchy.

The reaper's six `modal` CLI invocations now go through `ConcurrencyGroup.run_process_to_completion` instead of `subprocess.run`, so its child processes are tracked and cleaned up like every other spawned process in the codebase. They are unchecked, because each caller reads the return code itself; a timeout now arrives as data on the result rather than as an exception, which is what lets these functions keep their "warn and carry on, never raise" contract. A failure to launch `uv` still raises, now as `ProcessSetupError`.

Otherwise moved, not rewritten: `list_modal_test_environments`, `find_old_test_environments`, `delete_modal_apps_in_environment`, `delete_modal_volumes_in_environment`, `delete_modal_environment`, `sweep_old_modal_test_environment` and `cleanup_old_modal_test_environments` behave as before. The leak-tracking registries they use (`ModalCleanupOutcome`, `register_modal_test_*`, `TEST_ENV_PATTERN`) stay in core, which the plugin may import: the dependency now points one way, from plugin to core.

One real bug came out of the move: `delete_modal_apps_in_environment` ran `modal app stop <id> --yes` without `--env`. Unscoped, that resolves the id in your *default* Modal environment, where a test environment's apps are not visible, so every stop would have failed with "No App with ID ... found". It went unnoticed because the wrong-JSON-key bug meant the loop body never ran at all. The stop is now scoped with `--env`, matching the equivalent calls in `mngr_schedule` and `scripts/modal_nuke.py`.

## 2026-09-09

Deflaked the Modal CIDR-allowlist acceptance test `test_cidr_allowlist_allows_traffic_within_range`, which intermittently failed with a bare `CommandResult(stdout='000', ...)` on 21 commits across 18 branches over two weeks (MIND-245).

The test asserted that outbound traffic is permitted by curling `https://example.com` from a sandbox created with `--cidr-allowlist=0.0.0.0/0`. That made its verdict depend on DNS resolution and on a third-party CDN, neither of which a CIDR allowlist governs. Modal's in-sandbox resolver retries on a 10-second timer, exactly the probe's own `--max-time 10` budget, so a single lost DNS packet could not be recovered from inside the deadline. `curl -s` then discarded curl's explanation, leaving `000` -- which Modal also produces for genuinely blocked traffic, since it blackholes rather than refuses -- as the only evidence.

The probe now addresses a literal IP over plain HTTP, so neither DNS nor TLS can affect the verdict, and uses `-S` so curl's own account of a failure reaches stderr and gets printed by the assertion. The allowlist is pinned to a single `/32` containing that IP and the same sandbox also probes an out-of-range address, which must stay blocked: the test now verifies that the allowlist is enforced *and* admits its in-range target, where `0.0.0.0/0` alone would have passed even had enforcement been off entirely.

mngr_modal: fix the session-end Modal app-leak detector, which could never report a leak.

`_get_leaked_modal_apps` (in `conftest.py`) read `"App ID"` / `"Description"` / `"State"` from `modal app list --json`, but the Modal CLI emits `app_id` / `description` / `state`. Every row therefore compared as empty and the detector always returned `[]`, so the `pytest_sessionfinish` check that is meant to fail a test session that leaked Modal apps never fired.

Both `_get_leaked_modal_apps` and `_get_leaked_modal_volumes` now parse through mngr's `utils/modal_cli.py`. If the CLI's output ever stops carrying the keys we read, `pytest_sessionfinish` reports that as a session failure rather than as "nothing leaked" (raising out of that hook would be silently dropped by pytest).

## 2026-09-03

No behaviour change: `build_ssh_connect_command` moved from `imbue.mngr.primitives` to `imbue.mngr.utils.ssh`, and the import here follows it.

The function renders an ssh command rather than defining shared vocabulary, so it sits with the ssh option quoting it uses instead of alongside the enums and id types in `primitives`.

Marked all four `test_exec_*_on_modal` acceptance tests flaky again, so offload
retries them.

Each one creates a Modal agent, which deploys the `snapshot_and_shutdown`
function into the shared Modal app and so races concurrent deploys from the rest
of the suite. The deploy's own bounded retry rides out the usual case but can be
exhausted under load, surfacing as
`ModalProxyAppLockedError: ... Function fu-<id> not found`. The create and
snapshot tests that go through the same path were already marked.

This partially reverts MIND-202, which dropped the marker from these four after
fixing the fresh-sandbox SSH banner race at the source. That fix stands; the
race retried here is a different one -- the shared-app deploy lock -- and it is
a property of the agent-creation helper all four tests share, so the marker goes
back on all four rather than only the two seen failing.

## 2026-09-01

The real-Modal acceptance-test fixtures now ride out transient exhaustion of the shared CI workspace's 100-ephemeral-app cap: provider creation retries on the "reached limit" error at a 15s cadence for up to 4 minutes (the colliding concurrent CI runs' apps drain as their tests finish), instead of failing the whole run. Any other initialization failure remains immediately fatal.

## 2026-08-30

Bumped the pinned `modal` SDK from `1.4.3` to `1.5.4`. Modal-backed agents now run on Modal's V2 Sandbox backend (enabled at the modal-proxy boundary), which provides higher sandbox creation rates and concurrency. No change is required from users, and GPU sandboxes automatically continue to use the V1 backend.

`start_host` now confirms a discovered sandbox is actually alive (via the new `poll()` probe) before treating the host as running, instead of trusting its presence in `Sandbox.list`. Under V2 a just-terminated sandbox can linger in the listing, which otherwise made a stopped or hard-killed host look like it was still running (so a resume left a stale `stop_reason`, and a restart after a hard kill failed to report the missing snapshot).

## 2026-08-25

Documented how `ModalVolume.write_files` satisfies the strengthened per-file atomic-visibility contract on `Volume.write_files` (introduced by the robustness fixes from the 2026-07-14 partial workspace-destroy incident): modal's batched upload commits files as a unit, so concurrent readers never observe a torn (empty / partial) record. No behavior change.

## 2026-08-24

The `test_exec_*_on_modal` acceptance tests no longer flake on the fresh-sandbox SSH banner race (MIND-202); the underlying connect-retry fix lives in `mngr`. With the race removed at the source, `@pytest.mark.flaky` was dropped from all four tests, restoring them as hard oracles (a regression now turns CI red instead of being retried away). This was validated by a pressure test: five concurrent full-load CI runs with the marker removed produced 0 exec-test flakes across 38 executions. The removal also cleared an accidental duplicate marker on `test_exec_echo_on_modal`.

A Modal sandbox's sshd is now started from the same shared `SSHD_START_OPTIONS` as every other container sshd mngr launches, instead of its own copy of the options. It therefore picks up the `ClientAliveInterval 30` / `ClientAliveCountMax 4` hardening from the `mngr start` slowness investigation (`specs/mngr-start-slowness-investigation.md`): a client whose network path died silently is reaped in about two minutes rather than after the kernel TCP keepalive's ~2h11m, so a dead cooperative-lock holder no longer blocks every subsequent operation on the host for hours.

## 2026-08-22

Give the Modal `mngr exec` acceptance tests more headroom on a slow SSH-banner window: the shared `_exec_on_agent` helper's inner subprocess timeout is raised from 60s to 120s, so a valid-but-slow first exec against a fresh Modal sandbox is no longer killed mid-flight. The per-test 300s pytest timeout remains the hang backstop and the assertions are unchanged.

## 2026-08-21

Fixed Modal host provisioning failing en masse with "accepted connections but could not open sessions after 60.0s" (MIND-204). The sshd session-open readiness probe added in MIND-178 authenticated as the local OS user (paramiko's default) rather than the sandbox's `root` user, so every Modal host timed out at provisioning on machines where those differ (e.g. CI runners) -- turning the daily TMR and Release Tests lanes red from 08-20 onward.

`_wait_for_sshd` now passes the SSH user explicitly, via a shared `DEFAULT_SSH_USER` constant that also backs `_start_sshd_in_sandbox`'s default so the authorized-key user and the probe user cannot drift apart.

An unreachable Modal control plane no longer aborts every `mngr` command. When the Modal server cannot be connected to -- most often a dropped network -- the modal provider now raises `ProviderUnavailableError` instead of a fatal `MngrError`, so commands that query multiple providers skip modal and carry on with the providers they *can* reach. Previously a laptop that lost its wifi would fail a `mngr start` for a *docker* workspace on Modal, before ever getting to Docker.

Commands that genuinely target Modal still fail loudly: `mngr create @.modal` surfaces the outage directly (with guidance pointing at your network connection and https://status.modal.com rather than the generic "start Docker" advice), and an agent lookup that matches nothing reports the Modal outage rather than claiming the agent does not exist.

Only the connectivity case is reclassified. Every other Modal failure -- anything Modal actually answered with -- remains fatal, so a genuinely broken Modal integration still surfaces as an error rather than a silently short listing.

Marked `test_upload_deploy_files_handles_large_set_on_modal` `@pytest.mark.flaky`, so offload retries
it instead of failing the run.

The fresh Modal sandbox it creates can accept TCP before sshd answers the handshake, so the
connection fails ("No existing session") before the bulk upload the test actually guards ever
starts -- the same fresh-sandbox sshd boot race its already-marked neighbours were marked for.
Observed on a CI run whose changes were confined to `apps/minds_evals`, passing on the retry.

The marker only buys a retry; the underlying race is unchanged and still worth fixing at the source.

## 2026-08-19

Fixed: Harden Modal host bring-up against cold-start and provider-discovery latency (MIND-178).

- Added tenacity retry to `_get_ssh_info_from_sandbox` to absorb transient `ModalProxyError` during sandbox tunnel setup.

- Added `wait_for_sshd_with_retry` helper to verify the SSH server can actually open sessions (not just accept connections), preventing "No existing session" errors from cold-started sandboxes.

mngr_modal: register the Modal provider backend lazily so that starting `mngr` no longer imports the heavy `modal` SDK. The plugin entry point is now a thin `imbue.mngr_modal.plugin` module (SDK-free) that registers a `LazyProviderBackend`; the backend implementation (and the `modal` SDK it pulls in via `imbue.modal_proxy`) is imported only when a Modal provider is actually used. This lowers cold-start latency for provider-independent commands like `mngr config`, `mngr list`, and `mngr --help`. No user-facing behavior change: the modal build/start help text, deploy-file collection, and post-create sandbox snapshotting are all preserved.

Marked two more Modal acceptance tests `@pytest.mark.flaky`, so offload retries them instead of failing the run.

`test_exec_echo_on_modal` and `test_mngr_create_transfers_git_repo_with_untracked_files` intermittently lose the fresh-sandbox sshd boot race -- paramiko reporting `Error reading SSH protocol banner`, then an `EOFError` during the initial-snapshot write -- which outlasts mngr's deliberately bounded banner retry. This is the same failure mode their already-marked neighbours were marked for; these two were simply missed, and each was observed failing on one CI run and passing on the next with no change to the code under test.

The marker only buys a retry. The underlying race is unchanged and still worth fixing at the source.

## 2026-08-18

Fixed race condition in `list_snapshots` that caused it to return an empty list when called immediately after snapshot creation.

The `list_snapshots` method in `ModalProviderInstance` was reading the host record from the cache (`use_cache=True`), which could return stale data if the cache hadn't been updated after a snapshot was created during `stop_host`. This caused the flaky test `test_restart_after_graceful_stop_without_initial_snapshot` to fail intermittently with `assert len(snapshots) == 1` failing because `list_snapshots` returned an empty list.

Changed `list_snapshots` to bypass the cache and read directly from the volume (`use_cache=False`) to ensure fresh data is always returned.

Retry the post-deploy Modal function lookup on transient not-found. Modal's control plane is not immediately read-consistent after `modal deploy` returns (and concurrent re-deploys of the same app widen the window), so `deploy_function` could fail with "Function 'snapshot_and_shutdown' not found" right after a successful deploy -- seen as flaky `mngr create` failures on modal in acceptance CI. The freshly-deployed lookup now retries briefly on `ModalProxyNotFoundError` (bounded exponential backoff); bare `get_function_url` lookups stay fail-fast.

## 2026-08-17

Add a positive regression guard to the modal `mngr create` acceptance test: it now asserts that recursive provisioning actually staged the deployer's mngr config files to the remote host (a non-empty "Uploaded N mngr config files" upload), rather than merely that the CLI exited 0. This catches the class of silent provisioning skips behind GH-392, where deploy-file collection failed, was downgraded to a warning, and the acceptance suite passed while never provisioning anything.

(GitHub issue #392)

mngr_modal: mark `test_snapshot_and_shutdown_success` flaky so offload retries it instead of letting a transient Modal cleanup failure turn CI red (MIND-180).

Harden the snapshot-test teardown so a flaky Modal cleanup no longer fails the test. `_stop_app_and_delete_volume` now retries transient `modal app stop` / `modal volume delete` failures, treats an already-gone app/volume as success, and -- when a failure survives the retries -- logs it instead of raising `CleanupError`. Leaked resources are still reaped by the session-end cleanup and the `cleanup_old_modal_test_environments.py` CI job, which are the authoritative safety nets. Also give the post-snapshot "sandbox terminated" poll a 30s window (was 10s), since Modal can lag in reflecting termination, and correct a stale "hourly CI safety net" docstring reference.

## 2026-08-16

Fixed a chronic flake where operations right after a Modal filesystem snapshot failed with "Error reading SSH protocol banner": the snapshot transiently breaks new connections through the sandbox's tunnels (the tunnel edge accepts TCP and then closes it without a banner), and the post-snapshot host-record write opens a fresh SSH connection into exactly that window. `_record_snapshot` now re-verifies the tunnel with a full SSH handshake probe (the same `wait_for_sshd` used after boot, bounded by `ssh_connect_timeout`) before writing, so `mngr create` (initial snapshot), `mngr snapshot create`, and commands issued right after them no longer race the tunnel recovery.

- Marked `test_exec_echo_on_modal` with `@pytest.mark.flaky` (like its sibling
  Modal exec tests): fresh Modal sandboxes transiently accept TCP before sshd
  answers the SSH handshake, so a slow Modal window can outlast mngr's bounded
  banner retry. Offload now retries the whole test.

Modal host SSH info in live listings (`mngr list --format json`) now carries the explicit `known_hosts_path` that `SSHInfo` gained in this release, and the human-facing `ssh` command includes `-o UserKnownHostsFile=<path> -o StrictHostKeyChecking=yes` when the pin file is known, matching the shared construction paths.

## 2026-08-15

Phase 2 of the user-controlled-keys plan: per-host SSH keys for Modal sandboxes.

New sandboxes get their own client keypair (`modal_ssh_key`) and sshd host keypair instead of the provider-wide shared pair; the host key is re-injected at every sandbox boot (snapshot restores included), and a clone (`create_host --snapshot` under a fresh host_id) mints fresh keypairs rather than inheriting the source host's. Connections to sandboxes created before this change fall back to the legacy shared pair, which is CLEANUP-marked for removal once those sandboxes cycle out.

All known_hosts pins now carry the host_id and are written through mngr core's new host-key pin store, and `delete_host` forgets the host's pins and per-host keypairs. The unused `get_ssh_public_key` accessor is removed.

Marked `test_mngr_create_transfers_git_repo_with_untracked_files` as flaky (matching its siblings in the same file), so offload auto-retries it through transient Modal-service failures: function-deploy API errors and SSH transport EOFs during the initial sandbox snapshot.

## 2026-08-13

No behavior change. This provider still reports an agent blocked on a tool-approval dialog (or a question put to the user) as `RUNNING` rather than `WAITING`, where local, docker, ssh and lima agents now report `WAITING`.

Its listing builds agent state from a batched script that stats marker files by fixed name and has no agent object to ask, so it cannot see a per-agent-type block signal. It now says so explicitly at the call site rather than by omission.

## 2026-08-12

Marked `test_exec_cwd_override_on_modal` and `test_mngr_create_echo_command_on_modal` as `@pytest.mark.flaky`: fresh Modal sandboxes transiently accept TCP before sshd answers the SSH handshake, and a slow Modal window can outlast mngr's deliberately bounded banner-read connect retry, so offload now retries the whole test instead of failing the run on the infrastructure fluke.

Marked five Modal create/exec acceptance tests `@pytest.mark.flaky` (`test_mngr_create_with_build_args_on_modal`, `test_mngr_create_with_dockerfile_on_modal`, `test_mngr_create_transfers_git_repo_with_new_branch`, `test_exec_failure_propagates_exit_code_on_modal`, `test_exec_json_output_on_modal`) so offload retries them. They intermittently hit the fresh-Modal-sandbox sshd boot race (paramiko "Error reading SSH protocol banner"), which outlasts mngr's deliberately-bounded banner retry; every one of them was observed passing on retry in the same run. No product behavior change; joins the two Modal tests already marked for the same infra flake.

Merge the web-only work (`mngr/hopefully-last-web-details`) into main. Marked several Modal create/exec acceptance tests `@pytest.mark.flaky` (e.g. `test_mngr_create_with_build_args_on_modal`, `test_mngr_create_with_dockerfile_on_modal`, `test_exec_cwd_override_on_modal`, `test_exec_json_output_on_modal`) so offload retries them: fresh Modal sandboxes transiently accept TCP before sshd answers the SSH handshake, which outlasts mngr's deliberately-bounded banner-read retry. No product behavior change; every marked test was observed passing on retry.

## 2026-08-05

Fixed Modal hosts created without an explicit image or Dockerfile becoming permanently unreachable over SSH shortly after creation, surfacing as `Error reading SSH protocol banner` on a host that is otherwise healthy and still running.

That fallback path built the host from `modal.Image.debian_slim()`, which Modal hardcodes to bookworm (Debian 12) for every image builder version. default-workspace-template pins its apt sources to the trixie (Debian 13) suites and installs `openssh-server` during workspace setup, so the host's first `apt-get install` resolved into a full cross-release upgrade, replacing `libc6`, `ld-linux-x86-64.so.2` and `openssh-server` (9.2p1 to 10.0p1) underneath the sshd that had already been started. sshd re-execs the on-disk binary once per incoming connection, and the surviving 9.2 listener passes 9.2's `-R` re-exec flag, which the 10.0 binary rejects; the child therefore died before logging was initialised and the connection closed with no banner and no entry in the sshd log. Only the single connection opened before the upgrade kept working, which is why `mngr create` often got most of the way through and then failed on the first operation that needed to reconnect.

The fallback now builds from an explicitly named `python:3.12-slim-trixie`, matching the template, so no cross-release upgrade happens and sshd's binary and libraries stay intact for the life of the host.

The accompanying warning has also been corrected: it claimed the host was "building from mngr default Dockerfile", but this path never reads `libs/mngr/imbue/mngr/resources/Dockerfile`. It now names the base image it actually uses.

Marked `test_start_host_on_stopped_host_uses_latest_resumable_snapshot` as flaky: it failed in CI with an SSH connect timeout to a freshly-resumed sandbox, the same resource-contention family under the acceptance fan-out (~50 concurrent sandboxes) as the `VolumeListFiles` flakiness already marked flaky elsewhere in this file, just hitting a different Modal-side resource. Offload will retry it automatically.

## 2026-08-03

Deflake two Modal acceptance tests.

The discover-hosts test polls discovery for up to 150s after host creation instead of asserting on a single snapshot: Modal's sandbox listing is eventually consistent, and CI observed it stay stale for over a minute after a successful create.

The graceful-stop restart test is marked flaky (CI observed the restarted sandbox's fresh SSH tunnel transiently refuse connections, an infrastructure failure the test cannot control), and its accidentally duplicated acceptance/timeout decorator stack is collapsed to one.

Merge main into the mithril-refactor stack (relay-based sharing, iframe desktop shell, Mithril SPA). The changes to this project arrive verbatim from main and are described by their original PRs' changelog entries; no project-specific conflict resolution was needed here.

Deflake the Modal discover-hosts and restart acceptance tests: Modal's sandbox listing is eventually consistent, so the tests now poll discovery instead of asserting on a single snapshot.

## 2026-08-02

Added the repo-wide `test_prevent_unpinned_modal_pip_install` ratchet. mngr_modal starts at count 2: the built-in helper-app images in `routes/example.py` and `routes/snapshot_and_shutdown.py` still build with an unpinned `uv_pip_install("fastapi[standard]")`. Pinning the mngr_modal built-in images (including the default host image's apt installs) is a known follow-up; do not add new violations.

## 2026-07-29

mngr_modal: correct docstrings in `conftest.py` that described the Modal test-resource cleanup script as the "CI hourly cleanup". There is no cron trigger; the `cleanup_old_modal_test_environments.py` CI job runs on every push to main and every pull request. The docstrings now say so. No behavior change.

Mark `test_host_volume_is_symlinked_and_persists_data` flaky (it failed in CI on a transient Modal `VolumeListFiles` rate limit) and harden the host-volume visibility probes in `test_modal_instance.py`: they now treat `ModalMngrError` as "not yet visible" instead of propagating, since `wait_for` lets probe exceptions abort the poll immediately rather than retrying until the timeout.

Marked the two host-volume acceptance tests (`test_host_volume_is_symlinked_and_persists_data`, `test_host_volume_data_readable_via_volume_interface`) as flaky: Modal's per-workspace VolumeListFiles rate limit can stay exceeded longer than the volume layer's in-process retry budget under the parallel acceptance fan-out, so offload now retries them automatically.

Mark the two host-volume acceptance tests (`test_host_volume_is_symlinked_and_persists_data`, `test_host_volume_data_readable_via_volume_interface`) as `@pytest.mark.flaky`: both intermittently fail with Modal's "VolumeListFiles rate limit exceeded" when CI's parallel acceptance sandboxes burst-query the volume API (observed on main and on PR branches alike). The underlying rate-limit backoff is improved separately in `modal_proxy`.

Combined-fixes integration PR: marks the two host-volume acceptance tests (`test_host_volume_is_symlinked_and_persists_data`, `test_host_volume_data_readable_via_volume_interface`) as `@pytest.mark.flaky` -- Modal's per-workspace VolumeListFiles rate limit can outlast the in-process retry budget under CI's parallel acceptance fan-out. See `mngr-fix-remote-workspace.md` and `mngr-break-apart-scripts.md` in this directory for details.

## 2026-07-26

Integration branch combining the user-data-layout trains (`mngr/fix-data-layout`, `mngr/declutter-template`) with `mngr/fix-apt-mirror`; the full per-train details live in this project's sibling entries for those branches.

For this project: the modal provider honors the new `host_log_dir` config for its plain-text service logs (activity watcher, volume sync, shutdown script). Default unchanged.

The modal provider now honors the new `host_log_dir` config: the activity watcher, volume sync, and generated shutdown-script plain-text logs are written there when it is set. Default unchanged (`<host_dir>/logs`).

## 2026-07-14

Agent listings from this provider now populate `AgentDetails.pid` (the agent's main process PID in the remote host's PID namespace), extracted from the same already-collected tmux/ps probe data.

Test-only: mark the Modal snapshot acceptance test (`test_snapshot_create_then_list_on_modal`) as known-flaky. It sporadically fails while waiting for sshd on a freshly-booted Modal host ("Error reading SSH protocol banner"), which is Modal-side boot timing rather than a defect in the snapshot code.

## 2026-07-08

`discover_hosts_and_agents` now reads a **running** host's agents **live** off the sandbox instead of only from the state volume. Agents created *inside* the sandbox (for example the user's workspace agent) are never written to the outer state volume -- only host-side agents like `system-services` are -- so resolving from the volume alone left `mngr destroy` / `mngr start` unable to find a live in-sandbox agent (`Agent not found`), and minds' teardown spun on "Destroying..." forever. Offline hosts still resolve from the volume, and if the live read fails the host falls back to the volume so it still lists. This mirrors the VPS (aws/vultr) providers, which already read running hosts live.

`stop_host` no longer silently swallows a failed `sandbox.terminate()`. A failed terminate means the Modal sandbox is still running (a billing orphan), so it now raises a `ModalMngrError`; `destroy_host` records it as a cleanup failure so `mngr destroy` reports failure (and exits non-zero) instead of falsely succeeding while the sandbox keeps running.

`start_host` now clears `stop_reason` on the persisted host record when it resumes a host from a snapshot. Previously a host that was resumed (from PAUSED/STOPPED) and then later hard-killed was mis-derived as PAUSED/STOPPED instead of CRASHED, so it was never garbage-collected and wrongly showed a Resume control. This mirrors the lima provider's behavior on restart.

`start_host` auto-restart (no explicit `snapshot_id`) selects the most recent snapshot. The "initial" snapshot is taken in `on_agent_created` (after the agent is created and its initial message delivered), so it carries the agent and is a valid restore point; a graceful stop/pause leaves a newer snapshot that is preferred. When the host has no snapshots at all it raises `NoSnapshotsModalMngrError` (recreate the workspace rather than restart).

## 2026-07-07

Added an `is_vm_runtime_enabled` setting to the Modal provider config that controls whether sandboxes run on Modal's VM runtime (https://modal.com/docs/guide/vm-sandboxes) instead of the default gVisor runtime. It defaults to false (gVisor); set `is_vm_runtime_enabled = true` under `[providers.modal]` to create sandboxes with `experimental_options = {"vm_runtime": True}`, which provides stronger isolation and broader syscall compatibility (e.g. Docker-in-sandbox state surviving a filesystem snapshot).

Modal streaming discovery now reports each running host's SSH endpoint (via the shared `collect_cached_host_ssh_infos` helper) so the discovery poller re-emits it as a `HOST_SSH_INFO` event. Previously only a full `mngr list` surfaced SSH info, so a consumer that tunnels to Modal hosts through the streaming discovery path (e.g. the minds system_interface forward) could be left unable to reach them.

## 2026-07-06

Updated the per-host-bounded discovery override to accept the new cross-poll read registry parameter (unused by this batch provider, which reads all hosts in one bounded pass). No behavioral change for this provider.

Updated the Modal provider for mngr's new per-provider discovery: it now implements the bounded `discover_hosts_and_agents_within_timeouts` discovery entry point. Because Modal reads all host and agent state from the state volume in one batched pass, individual host reads are not separately bounded -- the provider is still bounded by the provider-level discovery error timeout, and no host is marked UNKNOWN by this path.

Fixed a regression where Modal authentication failures during `mngr list` surfaced as a raw proxy auth error instead of the friendly Modal auth error (with the provider name and disable hint): the `discover_hosts_and_agents` batch path is decorated with the auth-error handler again.

## 2026-07-01

Added a new async/await ratchet (`test_prevent_async_await`) that freezes the current amount of `async def` / `await` usage in this project and fails if new async code is added. We strongly prefer synchronous code: it is far easier to debug, and our software is intentionally low-scale, so async provides no benefit. Existing usage is grandfathered in at its current count; the count can only decrease.

## 2026-06-26

Added scope docstrings to this package's release tests so the TMR (test
map-reduce) harness can anchor each test's intended scope on its docstring
rather than on a tutorial block. Docstring-only; no test logic changed.

## 2026-06-22

Report an unauthenticated Modal provider consistently with the other cloud providers.

A missing/invalid Modal token now raises the shared `ProviderNotAuthorizedError` from provider construction, and `ModalAuthError` is now a subclass of `ProviderNotAuthorizedError` (preserving its existing message and remediation). As a result, Modal auth failures are categorized the same way as the other cloud providers in `mngr list` -- one consistent error line and the granular provider-inaccessible exit code -- instead of an ad-hoc plugin error.

Fixed host lock reporting for Modal hosts: a host's lock status is now derived from a real flock held-probe rather than the lock file's presence. The lock file now persists after release, so the previous mtime-based check would have reported every previously-locked host as permanently locked.

## 2026-06-19

Fixed a dead relative link in the README so it resolves on the PyPI project page.

Added `test_provider_release_trip1` to the Modal release suite: a single-boot full-lifecycle trip (create, exec, plain stop, `--stop-host` refusal, start, persistence, snapshot, out-of-band kill, gc, backend-clean) built on the shared provider release harness. Modal cannot stop a host's compute, so the trip asserts `mngr stop --stop-host` is refused with `HostShutdownNotSupportedError`; it is not parametrized over isolation (Modal has no isolation modes). Gated on Modal credentials plus the new `MNGR_MODAL_RELEASE_TESTS=1` opt-in.

Also added `test_provider_release_trip3` (snapshot survives destroy): Modal snapshots are portable, so the trip snapshots a host, destroys it, then asserts a fresh `mngr create --snapshot` restores the captured filesystem.

Also added `test_provider_release_trip4` (error classification): a no-boot CLI trip asserting `mngr create` with unresolvable Modal credentials surfaces the right error. This PR also fixes Modal to raise the contract `ProviderUnavailableError` (it previously raised a plain `MngrError`), with curated help pointing at `uvx modal token set`; the trip asserts the contract error class + that help. The `--vps-*` migration-arg scenario is skipped (Modal has its own build-arg parser).

Also added `test_provider_release_trip2` (idle auto-shutdown contract): it caps the sandbox lifetime via `-b --timeout=120` and asserts the sandbox is terminated by Modal's own timeout. Modal has no resumable stopped state, so the trip asserts the termination only and skips the resume.

## 2026-06-18

Agent lifecycle detection now targets the agent's primary tmux window by name (the configurable `tmux.primary_window_name`, default `agent`) instead of the literal `:0` index, so it works regardless of the user's tmux `base-index` setting.

## 2026-06-16

`destroy_host` now raises a `CleanupFailedGroup` carrying the classified cleanup failures (instead of returning them, or swallowing errors as warnings) when a resource is left behind, and returns normally otherwise. A resource that was already gone is treated as benign (no failure); a resource that exists but could not be destroyed is recorded as a `HOST_RESOURCE_REMAINS` failure (or `OTHER` for a bookkeeping/record write failure), so `mngr destroy`/`cleanup` can surface it and exit with an informative, cause-specific code. See `specs/cleanup-error-aggregation.md`.

## 2026-06-12

## AWS provider support: ProviderBackendInterface refactor

`is_for_host_creation` was removed from `ProviderBackendInterface` (Modal-specific flag was being `del`'d in every other backend). Modal now overrides a default-no-op `bootstrap_for_host_creation(name, config, mngr_ctx)` method on the interface, where the per-user environment registration moves. `mngr create` invokes this hook before `build_provider_instance`. No behavior change for Modal.

## 2026-06-09

Offline hosts produced by this provider are now readable: the offline-host
construction path (used by both `get_host` for stopped hosts and
`to_offline_host`) returns an `OfflineHostWithVolume` (which implements the new
`HostFileReadInterface`) via the shared `make_readable_offline_host` helper.
This makes a stopped host's files readable through the same interface as an
online host -- used by Claude session preservation when a host is destroyed
while offline (the destroy path obtains the host via `get_host`), and available
to other readers of offline host data. The host's volume is resolved lazily on
first read, so this adds no per-host probe to host discovery. When no volume is
available, reads behave as "nothing there".

The new `get_volume_reference_for_host` is wrapped so missing/expired Modal
credentials surface as the user-friendly `ModalAuthError` (consistent with the
other provider methods) rather than a raw proxy error, including when reached
during offline-host construction.

## Remove Modal async-permission-propagation workaround

Modal has fixed the bug on their side where a just-created environment returned
`modal.exception.PermissionDeniedError` for several seconds (async per-user
permission propagation) before the creating user could operate on it.
Read-after-write is now immediate, so the workaround is no longer needed.

Removed from `mngr_modal`:

- The `ModalProxyPermissionDeniedError` retries in
  `_lookup_persistent_app_with_retry` and `_enter_ephemeral_app_context_with_retry`
  (`imbue.mngr_modal.backend`); both decorators once again retry only on
  `ModalProxyNotFoundError`.
- The `_invoke_modal_sdk_delete_with_retry` test-cleanup helper in `conftest.py`;
  `_classify_modal_sdk_delete` now invokes the SDK delete callable directly again.

## 2026-06-08

Standardized mngr_modal's project conftest on `register_plugin_test_fixtures(globals())`
for HOME isolation, the same single mechanism used by every mngr plugin. The
Modal-specific fixtures (including the credential-loading `setup_test_mngr_env`
override) are unchanged. Internal test-infrastructure change only; no user-facing
behavior change.

Creating a Modal host with an invalid argument (e.g. a non-existent
`--snapshot` image id) now fails with a clean single-line error
(`Error: Failed to create Modal host: '<id>' is not a valid Image ID.`) instead
of dumping a raw Python traceback. `create_host` now wraps
`ModalProxyInvalidError` in a user-facing `MngrError`, mirroring how
`ModalProxyRemoteError` was already handled.

## 2026-06-04

Added an acceptance test (`test_upload_deploy_files_handles_large_set_on_modal`) that uploads a large (600-file) deploy set to a real Modal host and verifies every file lands. This is the regression guard for github issue 1825, where `mngr create` on Modal failed during provisioning (`Error reading SSH protocol banner`) because deploy files were uploaded one SFTP channel at a time; the fix transfers them with a single rsync.

Updated references to the renamed `modal_proxy` test doubles: `TestingModalInterface`
is now `FakeModalInterface` (and the rest of the `Testing*` Modal family is now
`Fake*`). Affects the `make_testing_provider`/`testing_modal` test helpers and
fixtures, which now reference `FakeModalInterface`. No behavior change.

## 2026-06-04

- Three modal acceptance tests (`test_get_host_by_id`, `test_discover_hosts_includes_created_host`, `test_destroy_host_stops_sandbox_and_delete_host_removes_record`) had their `pytest.mark.timeout` bumped from 180s to 300s and a `pytest.mark.flaky` mark added. They all hit `sandbox.tunnels()` while waiting for SSH on a fresh Modal sandbox, which intermittently brushes the 180s ceiling under load -- the bump matches the longer-running peers in the same file (e.g. `test_persistent_host_creates_shutdown_script` already used 300s) and `flaky` lets offload retry on the rare residual timeout.

## 2026-06-02

Preserved connection-error handling in the Modal provider's optimized listing now that
`HostConnectionError` is a `MngrError` subclass. `get_host_and_agent_details` now re-raises
`HostConnectionError` from the inner SSH-collection guard so it still reaches the outer
handler that clears the per-host connection cache and falls back to the default listing,
instead of being swallowed by the inner `except MngrError`.

## 2026-06-01

Fixed flakiness in two `mngr_modal` host-volume acceptance tests by polling `get_volume_for_host` with `wait_for` instead of asserting once, to absorb the brief Modal control-plane lag before a freshly-created volume becomes resolvable by name.

# Offline agent field generators

Updated the provider's `get_host_and_agent_details` override to accept and forward the new `offline_field_generators` parameter to the base implementation, so offline plugin fields (see the mngr changelog entry) are populated when a host falls back to offline data.

## 2026-05-28

# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

No user-facing behavior change.

## 2026-05-27

# Ratchet count tightening

- Tightened the violation counts recorded in `test_ratchets.py` to their current exact values (via `uv run pytest --inline-snapshot=trim`), locking in previously-unrecorded reductions. No source-code or behavior change.

## 2026-05-26

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

Adopted the `PREVENT_BARE_TMUX_TARGETS` ratchet rule (added in `imbue_common`) via
`rc.check_bare_tmux_targets(_DIR, snapshot(0))` in this project's `test_ratchets.py`.
This ratchet prevents new occurrences of `tmux <subcmd> -t '<bare-name>'` -- targets
without a leading `=` exact-match prefix, which can silently route commands to a
sibling session whose name shares a prefix with the intended one. No production code
changes in this project; the adopting test starts at a baseline of zero violations.

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

The acceptance test for `mngr_claude_usage`'s statusline-shim provisioning on a real Modal host (`test_provision_statusline_shim_on_modal_host`) is updated to assert against the new host-stable shim path layout (`<host_dir>/commands/claude_statusline.sh`) and the per-agent sidecar (`<state_dir>/commands/user_statusline_cmd`).

## 2026-05-20

## Modal provider no longer auto-creates an environment from non-create commands

`mngr list`, `mngr gc`, and other read flows no longer silently bootstrap a
Modal environment (the `Created Modal environment: ...` log line) just because
the modal provider is enabled. The Modal provider now disables itself (raises
`ProviderUnavailableError`, which higher-level loaders skip) when its per-user
Modal environment doesn't exist yet. Only `mngr create` is allowed to bootstrap
the environment on first use.

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

- `mngr_modal`: Modal backend now raises a new `ProviderEmptyError`
  (distinct from `ProviderUnavailableError`) when its per-user
  environment doesn't exist yet, so `mngr list` can silently skip the
  empty provider instead of aborting. (Counterpart to the new
  `ProviderEmptyError` handling in the listing pipeline.)

Collapse Modal environments across an offload-acceptance / offload-release
run to a single shared env (opt-in via `MNGR_TEST_SHARED_MODAL_ENV_NAME`).
Each fanned-out sandbox used to mint its own Modal environment and delete
it on teardown -- dozens to hundreds per run, driving the
1500-env-per-workspace cap into transient failures. Inside each sandbox,
the modal test fixtures (`real_modal_provider`, `persistent_modal_provider`,
`initial_snapshot_provider`, plus the session-scoped subprocess-env
fixtures) honor the env var: they thread its name through
`MngrConfig.prefix` + `ModalProviderConfig.user_id` so every test lands
in the shared env, and they skip env creation / deletion / leak-tracking
at the fixture layer (apps and volumes are still created and deleted
per-test as before). Local pytest behavior (no env var set) is unchanged.

Fix Modal resource leaks in `test_snapshot_and_shutdown.py`. The teardown's `modal app stop` and `modal volume delete` calls were both silently failing (`check=False`, captured output discarded); the fixture also wasn't passing an `environment_name` to `deploy_function`, so the test app + volume landed in the default `main` env outside any cleanup safety net. Pass the session-scoped Modal env to deploy, sandbox lookup, volume operations, and cleanup; run app-stop and volume-delete in parallel with `check=True` so any future failure surfaces immediately.

## Retry on async Modal permission propagation

Modal recently migrated their permission system so the per-user permission entry for a just-created environment is propagated asynchronously (typically ~3-7 seconds after `modal environment create` returns success). During that window, operations on the new environment raise `modal.exception.PermissionDeniedError` instead of `NotFoundError`. This was breaking every Modal acceptance test at fixture construction time.

- `imbue.mngr_modal.backend`: both `_enter_ephemeral_app_context_with_retry` and `_lookup_persistent_app_with_retry` now retry on `ModalProxyPermissionDeniedError` in addition to `ModalProxyNotFoundError`, matching the existing 5-attempt exponential backoff (1s→10s) used for env-not-found.
- `libs/mngr_modal/imbue/mngr_modal/conftest.py`: the test cleanup helper `_classify_modal_sdk_delete` now retries Modal SDK deletes through the same propagation window so that fast-running test teardowns don't spuriously leak environments/volumes.

No user-visible behavior change beyond fewer transient Modal failures and a small added startup latency (~3-7s on the very first `mngr create` against a brand-new environment, only on first use per profile).

Applied `@fixture_uses_resources` to `deployed_snapshot_function` in `test_snapshot_and_shutdown.py` to fix `test_snapshot_and_shutdown_missing_host_id` and `test_snapshot_and_shutdown_missing_sandbox_id` failing on the modal resource guard.

Bumped pinned `modal` dependency from 1.3.1 to 1.4.3 to stay in sync with the rest of the monorepo.

## 2026-05-14

CI acceptance test speedup — fix the `mngr_modal` session-end leak detector in `libs/mngr_modal/imbue/mngr_modal/conftest.py` (previously the `modal_session_cleanup` autouse fixture; now a `pytest_sessionfinish` hook so it runs after all session-scoped fixture teardowns -- pytest's autouse session-scoped fixtures tear down before non-autouse session-scoped fixtures regardless of declared dependencies, which made the previous fixture poll a still-registered env and fail before the deregister could run). The detector compared the global `modal environment list --json` against tests' tracked env names, but Modal's listing endpoints are eventually consistent w.r.t. deletion -- after a `modal environment delete X` returns "Environment 'X' not found", the env can still appear in the global list for tens of seconds. With one-test-per-batch the assertion almost never landed in the inconsistency window; with several tests per session it became consistent enough to repeatedly fail teardown on whichever test happened to be last. The fix is twofold: (a) the per-test and session-scope cleanup fixtures deregister tracked resources from `worker_modal_*_names` *only* when the cleanup chain confirmed the resource was deleted or already gone (the synchronous response is authoritative); cleanup failures keep the resource tracked and log a `logger.error` so the session-end leak detector still has a chance to surface a real leak. Cleanup return values are typed via a new `ModalCleanupOutcome` enum (`DELETED | NOT_FOUND | FAILED`). (b) the `pytest_sessionfinish` hook runs after all session-scoped fixture teardowns, so any name still in `worker_modal_*_names` at that point corresponds to a resource whose cleanup either FAILED or was never attempted (test crashed mid-fixture) -- i.e. a real leak rather than a listing-staleness false positive.

## 2026-05-12

TMR: when launching modal agents, override the modal provider config to
skip the per-agent "initial" filesystem snapshot. That snapshot adds 60-90s
per agent and runs once per agent (so 4 agents on a pooled host trigger
four snapshots), even though TMR's pooled hosts are ephemeral and the
snapshotter's host is snapshotted explicitly already.

## 2026-05-08

- mngr_modal: drop `ModalMode.TESTING` from production code paths; tests inject `TestingModalInterface` via `make_testing_provider` instead. Production `mngr_modal.backend` no longer imports `modal_proxy.testing` at module top, so the standard `**/testing.py` wheel-exclude rule applies cleanly to `modal_proxy` (no `only-include` workaround needed) and packaged consumers (e.g. minds.app) no longer crash with `ModuleNotFoundError: No module named 'imbue.modal_proxy.testing'`.
- mngr_modal: `ModalMode` retained with values `DIRECT` (default) and `PROXIED`. `PROXIED` is reserved for routing Modal traffic through the imbue_cloud gateway and currently raises `NotImplementedError` at `build_provider_instance`. The `mode` field on `ModalProviderConfig` is preserved.
- mngr_modal: extract pure `ModalProviderBackend._derive_modal_names(name, config, mngr_ctx)` helper so the environment-name / app-name / host-dir derivation can be unit-tested without instantiating any Modal interface.
- mngr_modal: drop unused `is_testing` parameter from `_get_or_create_app` (only ever non-default in the now-removed `TESTING` dispatch arm; the test-fixture path constructs `ModalProviderApp` directly and never went through this function).

- mngr_modal: extract `ModalProviderBackend._construct_modal_provider(name, config, mngr_ctx, modal_interface)` as the shared factory body. `build_provider_instance` matches the parent-class signature exactly, dispatches on `config.mode` (`DIRECT` selects `DirectModalInterface()`, `PROXIED` raises `NotImplementedError`), then delegates to `_construct_modal_provider`. Tests call `_construct_modal_provider` directly with `TestingModalInterface`. The factory has no per-implementation branches.
- mngr_modal: `make_testing_provider` collapses from a 35-line parallel constructor into a wrapper around `ModalProviderBackend._construct_modal_provider`.
- mngr_modal: delete the dead `mngr_modal/log_utils.py` re-export shim (`b66f3cbd5`'s in-tree migration is complete; nothing imports from it).

- mngr_modal: register the session-scoped Modal env created by `modal_subprocess_env` with the leak-detection registry (`register_modal_test_environment`) so that silent failures in the per-session cleanup helpers (`delete_modal_apps_in_environment` / `delete_modal_volumes_in_environment` / `delete_modal_environment`) are now caught by the autouse `modal_session_cleanup` at session end, rather than leaking the env onto the Modal account.

- mngr_modal: restore the per-test reset of `ModalProviderBackend._app_registry`. The
  autouse `_reset_modal_app_registry` fixture was deleted in #1533. After #1522
  reshaped the test factory to dispatch through `_construct_modal_provider`
  (which short-circuits on the class-level `_app_registry`), the reset became
  load-bearing for cross-test isolation: the second test in a worker would
  reuse the first test's cached app and skip `modal_interface.app_create(...)`,
  leaving `testing_modal._apps` empty and breaking helpers like
  `make_sandbox_with_tags`. Restoring the fixture fixes the post-merge CI
  failures on main.
