# Cleanup error aggregation and classification

## Overview

`mngr stop`, `mngr destroy`, and `mngr cleanup` (the `execute_cleanup` API) currently handle failures inconsistently:

- The **stop path** (`Host.stop_agents`) bounds each tmux/pgrep/kill command with a timeout and raises `CommandTimeoutError` on a timeout (fail-fast), but **silently swallows every other failure**: the shell commands carry `2>/dev/null` / `|| true` / `; true`, and the Python code treats a non-zero `CommandResult` as "no PIDs". A real failure (e.g. a process that could not be killed, or a tmux server error) is indistinguishable from the benign "the thing is already gone" case, so it is ignored and the agent is reported "stopped".
- The **destroy path** (`Host.destroy_agent` + each provider's `destroy_host`) mostly catches provider/SDK exceptions and **logs them as warnings, then continues**, swallowing real failures (e.g. a Docker image that exists but cannot be removed, a VPS instance that is still running). Errors that do surface land in `CleanupResult.errors` as bare strings.
- The **CLI** logs `CleanupResult.errors` as warnings and **always exits 0**, so a partially-failed cleanup is indistinguishable from a clean one to any caller or script.

This spec defines a uniform model: cleanup **never silently ignores an error**. Every failure is either classified as **benign** (the resource was already gone, so nothing is left behind -> not an error) or **real** (a resource is actually left un-cleaned-up -> recorded). Real failures are aggregated (cleanup continues rather than failing fast), each tagged with a **cause category**, and the process exits with an **informative, cause-specific exit code**.

**Audience:** developers implementing or reviewing the cleanup paths.

**Related specs:** [detached-destroy-flow](detached-destroy-flow/spec.md), [docker-cleanup-state-and-images](docker-cleanup-state-and-images/spec.md), [discovery-provider-error-resilience](discovery-provider-error-resilience.md).

## Goals

- A failure that leaves a resource behind is **always surfaced** (recorded + non-zero exit), never silently swallowed.
- A "failure" that only occurred because the **target was already gone** produces **no error and exit 0**.
- **Timeouts are treated as one error cause among many**, not special-cased.
- Failures are **aggregated**: cleanup attempts every step / agent / host and collects all failures, rather than aborting on the first (subject to `ErrorBehavior`).
- The process **exit code identifies the cause** of the most severe failure; structured output enumerates every failure.
- Applies to **both stop and destroy** paths, across all providers.

## Non-goals

- Changing *what* cleanup does (which commands run, which resources are removed). This is purely about error detection, classification, aggregation, and reporting.
- Retrying failed operations. A real failure is recorded, not retried.
- Changing `ErrorBehavior` semantics (`ABORT` still stops early; `CONTINUE` still proceeds). Aggregation happens *within* a single host/agent operation regardless; `ErrorBehavior` governs whether we proceed to the *next* host/agent.

## Core concepts

### Benign vs. real failure

A command or operation "fails" (non-zero exit, or raises) for one of two reasons:

- **Benign** -- the resource it targeted was **already absent** (session/window/pane already gone, process already dead, container/VM/volume/droplet/dir/key already removed). Nothing is left behind. This is a **success** for cleanup purposes: no error recorded, contributes nothing to the exit code.
- **Real** -- the resource **exists but could not be cleaned up**, or the operation could not complete (timeout, permission denied, provider unreachable, API error). Something is (or may be) left behind. This is recorded as a failure with a cause category.

Detection mechanism differs by layer (see [Classification rules](#classification-rules)):

- **Shell commands** (stop path, `rm`): classify by **exit code + stderr substring matching**. This requires *removing* the `2>/dev/null` / `|| true` / `; true` guards that currently pre-swallow errors, so the Python layer can see the real exit code and stderr.
- **Provider operations** (destroy path): classify by **exception type** -- providers already raise typed "not found" exceptions (`docker.errors.NotFound`, `ModalProxyNotFoundError`, `HostNotFoundError`, etc.).

### Failure cause categories and exit codes

Each real failure is tagged with a `CleanupFailureCategory`. Categories map to process exit codes, extending the existing `libs/mngr/imbue/mngr/cli/exit_codes.py` (`SUCCESS=0`, `ERROR=1`, `TIMEOUT=2`):

| Category | Meaning (what is left behind) | Exit code |
|---|---|---|
| `TIMEOUT` | A cleanup command timed out; the resource's state is unknown / likely incomplete. | `2` (existing) |
| `PROCESSES_REMAIN` | Agent PIDs were collected but could not be killed (e.g. permission denied), or could not be enumerated; processes may still be running. | `3` (new) |
| `LOCAL_STATE_REMAINS` | A tmux session or the agent's on-host state directory could not be removed though present. | `4` (new) |
| `HOST_RESOURCE_REMAINS` | An infrastructure resource (container, VM, droplet, volume, disk, SSH key) could not be destroyed though present. May incur ongoing cost. | `5` (new) |
| `PROVIDER_INACCESSIBLE` | Cleanup could not even be attempted: host record missing, provider unreachable, host not destroyable / unsupported. | `6` (new) |
| `OTHER` | Uncategorized real failure (e.g. a plugin `on_destroy` hook raised, an unexpected exception). | `1` (existing `ERROR`) |

A run may hit several causes, but a process has one exit code. Resolution:

- **Structured output (JSON) enumerates every failure** with its category and message.
- **The process exit code is the code of the most severe cause** that occurred.

**Severity order (most severe first):** `HOST_RESOURCE_REMAINS` > `PROCESSES_REMAIN` > `LOCAL_STATE_REMAINS` > `TIMEOUT` > `PROVIDER_INACCESSIBLE` > `OTHER`.

Rationale: leaked paid infrastructure is worst; live processes next; local state and unknown-due-to-timeout below that; "couldn't attempt" and "uncategorized" last. (Severity order is independent of the numeric code values; it is an explicit ranking.)

**Note:** if no real failures occur, exit code is `SUCCESS` (0), even if some commands "failed" benignly.

## Data model

`CleanupFailureCategory` and `CleanupFailure` live in `libs/mngr/imbue/mngr/interfaces/data_types.py` (alongside `CommandResult`), so the `hosts` and `providers` layers can produce them without importing `api`. `CleanupResult` stays in `libs/mngr/imbue/mngr/api/data_types.py`. `CleanupFailureCategory` is an `UpperCaseStrEnum` (member values `TIMEOUT`, `PROCESSES_REMAIN`, ...). The low-level methods **raise** these (wrapped in a `CleanupFailedGroup`; see the **Propagation** paragraph below) rather than returning them, so a forgotten return value can never silently drop a real failure.

```python
class CleanupFailureCategory(StrEnum):
    TIMEOUT = "timeout"
    PROCESSES_REMAIN = "processes_remain"
    LOCAL_STATE_REMAINS = "local_state_remains"
    HOST_RESOURCE_REMAINS = "host_resource_remains"
    PROVIDER_INACCESSIBLE = "provider_inaccessible"
    OTHER = "other"

class CleanupFailure(FrozenModel):
    category: CleanupFailureCategory
    message: str                       # human-readable description
    agent_name: AgentName | None = None
    host_id: HostId | None = None
```

`CleanupResult` gains a structured failures list. To avoid a disruptive change to every reader of `errors`, we **replace** `errors: list[str]` with `failures: list[CleanupFailure]` and expose a derived `errors` property (formatted strings) for the human output path and any string consumers:

```python
class CleanupResult(MutableModel):
    destroyed_agents: list[AgentName] = Field(default_factory=list)
    stopped_agents: list[AgentName] = Field(default_factory=list)
    failures: list[CleanupFailure] = Field(default_factory=list)

    @property
    def errors(self) -> list[str]:
        return [f"[{f.category}] {f.message}" for f in self.failures]
```

(If a `MutableModel` property collides with field-update mechanics, use an explicit `formatted_errors()` method instead; the implementer should pick whichever the model framework supports cleanly.)

**Exit code constants** (`cli/exit_codes.py`): add `EXIT_CODE_PROCESSES_REMAIN = 3`, `EXIT_CODE_LOCAL_STATE_REMAINS = 4`, `EXIT_CODE_HOST_RESOURCE_REMAINS = 5`, `EXIT_CODE_PROVIDER_INACCESSIBLE = 6`. Add a pure function mapping a list of `CleanupFailure` to an exit code via the severity order:

```python
def exit_code_for_failures(failures: Sequence[CleanupFailure]) -> int:
    # returns EXIT_CODE_SUCCESS if empty, else the code of the most-severe category present
```

**Propagation: raise an exception group.** Because the model is aggregate-and-continue, the low-level cleanup methods attempt every step, collect every real failure, and **raise them once at the end** wrapped in a `CleanupFailedGroup`. `Host.stop_agents`, `Host.destroy_agent`, and each provider's `destroy_host` keep their `None` return type: returning normally means fully clean (or only benign "already gone" outcomes), and a non-empty set of real failures is raised. This is an interface change on `HostInterface`/`OnlineHostInterface` (`stop_agents`, `destroy_agent`) and `ProviderInstanceInterface` (`destroy_host`).

Raising (rather than returning a `list[CleanupFailure]`) is what makes "a real failure is never silently swallowed" a property of the *type system* rather than of caller discipline: a returned list can be discarded with no diagnostic, whereas an unhandled `CleanupFailedGroup` propagates loudly. The carrier types live in `libs/mngr/imbue/mngr/interfaces/cleanup_failures.py`:

- `CleanupFailedError(MngrError)` -- one leaf, wrapping a single structured `CleanupFailure` (recoverable via `.failure`).
- `CleanupFailedGroup(ExceptionGroup[CleanupFailedError])` -- the group actually raised; `.failures` returns the tuple of structured `CleanupFailure`s, and `.from_failures(seq)` builds it. This mirrors the existing `ConcurrencyExceptionGroup` precedent.
- `collecting_cleanup_failures()` -- a context manager yielding a list the operation appends to; on exit it raises `CleanupFailedGroup.from_failures(...)` if the list is non-empty, else returns normally. Each cleanup method wraps its body in this. A sub-operation that itself raises a group (e.g. `destroy_agent` calling `stop_agents`) is absorbed into the enclosing aggregate via `collect_cleanup_failures(sink, group)` so the remaining steps still run.

Exception-group leaves are `CleanupFailedError`, an `MngrError` subclass; the *group* is **not** an `MngrError`. This keeps the two failure modes type-distinct at the **orchestration boundary**: a `CleanupFailedGroup` means "we acted, but left resources behind" (collect its `.failures`, still record the agent/host as acted-on), whereas a bare `MngrError` from `get_provider_instance` / `get_host` means we could not act at all (`execute_cleanup` converts it to a `PROVIDER_INACCESSIBLE`/`OTHER` `CleanupFailure`; see [Orchestration](#orchestration)). A `CommandTimeoutError` is **not** raised out of `stop_agents`; it is caught internally and aggregated as a `TIMEOUT` failure.

## Classification rules

### Stop-path shell commands

The six stop commands are rewritten to **not** pre-swallow errors (drop `2>/dev/null` and `|| true`/`; true` where they hide exit codes/stderr), so a classifier can inspect the result. A shared helper runs each command bounded by `_STOP_AGENT_COMMAND_TIMEOUT_SECONDS` and returns one of: `Ok(output)`, `Benign`, or `Failure(category, message)`.

Detection: a `CommandTimeoutError` (from `raise_on_timeout`) -> `Failure(TIMEOUT)`. Exit 0 -> `Ok`. Non-zero -> `Benign` if exit code / stderr matches the command's benign patterns, else `Failure(category)` with the per-command category below.

| Command | Benign (already gone) signature | Real-failure category |
|---|---|---|
| `tmux list-windows -t =<session>` | stderr matches `can't find session`, `no server running` | `PROCESSES_REMAIN` (could not enumerate -> processes may remain) |
| `tmux list-panes -t =<session>:<win>` | stderr matches `can't find session`, `can't find window`, `can't find pane`, `no server running` | `PROCESSES_REMAIN` |
| `pgrep -P <pid>` | exit 1 (no matching children) | `PROCESSES_REMAIN` (exit >= 2 = pgrep error) |
| `/proc` env scan | exit 0 always (loop emits matches; scan errors per-pid are local). A timeout -> `TIMEOUT`. | `PROCESSES_REMAIN` only if the scan command itself errors |
| `kill -TERM`/`-KILL` loop | per-pid stderr `No such process` (ESRCH) -> already dead | `PROCESSES_REMAIN` if any stderr line is non-ESRCH (e.g. `Operation not permitted`) |
| `tmux kill-session -t =<session>` | stderr matches `can't find session`, `no server running` | `LOCAL_STATE_REMAINS` (session exists but could not be killed) |

**Kill-loop nuance:** the loop is a single batched shell command. Run it without `2>/dev/null`, capture combined stderr, and classify: if every non-empty stderr line matches the ESRCH "No such process" pattern, it is benign (the pids died between collection and kill -- expected); any other line (notably "Operation not permitted") yields `PROCESSES_REMAIN`. The benign-pattern list is a module constant so it is easy to audit and extend.

**TOCTOU note:** message-matching (the chosen approach) is robust to the session vanishing between collection and a later command -- a `list-panes`/`kill-session` that then fails with `can't find session` is correctly classified benign, with no separate existence-guard needed.

### Destroy-path provider operations

Providers already raise typed exceptions. Each `destroy_host` (and `destroy_agent`'s non-shell steps) is updated to **stop swallowing** and instead classify. Benign = a typed "not found / already gone" exception; real = anything else, mapped to a category.

| Operation (provider) | Benign exception (already gone) | Real-failure category |
|---|---|---|
| `destroy_agent` `rm -rf <state_dir>` (host.py) | shell already idempotent (missing dir -> exit 0); non-zero with `No such file` is benign | `LOCAL_STATE_REMAINS` (e.g. permission denied) |
| `remove_persisted_agent_data` (provider) | provider no-op when absent | `LOCAL_STATE_REMAINS` |
| Docker `container.remove(force=True)` | `docker.errors.NotFound` / container `None` | `HOST_RESOURCE_REMAINS` |
| Docker `images.remove(tag)` | image-tag-absent (list check empty) | `HOST_RESOURCE_REMAINS` |
| Modal `volume_delete` | `ModalProxyNotFoundError` | `HOST_RESOURCE_REMAINS` |
| VPS `remove_container` / `remove_volume` / `delete_btrfs_subvolume` (container realizer only) | already-gone (helpers `tolerate_missing` / `-f` no-op on absent, so a raised `MngrError` means the resource is present but could not be removed) | `HOST_RESOURCE_REMAINS` |
| VPS `vps_client.destroy_instance` | instance-already-gone (provider 404/410) | `HOST_RESOURCE_REMAINS` (a leaked paid instance is the worst case) |
| VPS `vps_client.delete_ssh_key` | key-already-deleted | `HOST_RESOURCE_REMAINS` (leaked credential) |
| Lima `limactl_delete` / `limactl_disk_delete` | VM/disk-already-gone (`LimaCommandError` with not-found text) | `HOST_RESOURCE_REMAINS` |
| `remove_host_from_known_hosts` | `FileNotFoundError` | benign always (cosmetic local file) |
| `_host_store.write_host_record` (mark DESTROYED) | -- | `OTHER` (record not updated; state inconsistency) |
| `LocalProviderInstance.destroy_host` | -- | `PROVIDER_INACCESSIBLE` (`LocalHostNotDestroyableError`: local host is intentionally not destroyable) |
| `SSHProviderInstance.destroy_host` | -- | `PROVIDER_INACCESSIBLE` (`NotImplementedError`) |
| `provider.get_host` / host record lookup | -- | `PROVIDER_INACCESSIBLE` (`HostNotFoundError`, `ProviderUnavailableError`) |
| `on_before_agent_destroy` / `on_destroy` hooks | -- | `OTHER` (plugin failure) |

**Provider "absent" signals.** For the VPS family the container-specific teardown steps (`remove_container`/`remove_volume`/`delete_btrfs_subvolume`) live behind the realizer in `DockerRealizer.teardown_placement` (`libs/mngr_vps/imbue/mngr_vps/docker_realizer.py`); a BARE (`isolation=NONE`) host has no container/volume/btrfs to tear down, so `BareRealizer.teardown_placement` (`libs/mngr_vps/imbue/mngr_vps/bare_realizer.py`) is a no-op and these rows apply to the container realizer only. The "absent" signal is made distinguishable by the underlying helper rather than by message matching: `remove_container(..., tolerate_missing=True)`, `delete_btrfs_subvolume_on_outer`, and `remove_volume` (`docker volume rm -f`) each no-op on an already-gone resource, so any `MngrError` they raise means the resource is present but could not be removed -> `HOST_RESOURCE_REMAINS`. The provider-level `destroy_instance`/`delete_ssh_key` steps in `VpsProvider.destroy_host` (`libs/mngr_vps/imbue/mngr_vps/instance.py`) distinguish a benign provider 404/410 from a real failure via the module-level `is_vps_resource_already_gone` (`libs/mngr_vps/imbue/mngr_vps/instance.py`), applied through `attempt_cloud_resource_teardown`. The container realizer's `CleanupFailedGroup` is absorbed into the provider's destroy aggregate via `collect_cleanup_failures`.

## Execution model

### Stop path (`Host.stop_agents`)

1. For each agent, run the collection + kill + kill-session steps best-effort, classifying each command via the shared helper. Do **not** abort on a real failure -- record a `CleanupFailure` (tagged with the agent) and continue to the next step / agent.
2. A timed-out step yields a `TIMEOUT` failure for that agent and we continue (the remaining steps are still attempted; if the tmux server is wedged they will likely also time out and add their own failures -- that is fine, they aggregate).
3. Wrap the body in `collecting_cleanup_failures()`: append each `CleanupFailure` to the yielded list; on exit it raises a `CleanupFailedGroup` if any were collected, or returns normally if everything was clean or only-benign.

This replaces the current fail-fast `raise_on_timeout` propagation. `raise_on_timeout` remains the **internal mechanism** the helper uses to *detect* a local timeout (a local timeout otherwise looks like a benign non-zero exit); the helper catches the resulting `CommandTimeoutError` and turns it into a `TIMEOUT` `CleanupFailure` rather than letting it propagate out of `stop_agents`.

### Destroy path

- `Host.destroy_agent`: runs `on_destroy` hook, `stop_agents`, `rm -rf <state_dir>`, `remove_persisted_agent_data`, each best-effort with classification, all inside `collecting_cleanup_failures()`. `stop_agents` now raises its own `CleanupFailedGroup`; `destroy_agent` catches it and merges its `.failures` in via `collect_cleanup_failures` so the remaining steps still run. Raises the combined `CleanupFailedGroup` (or returns normally if clean).
- Each provider `destroy_host`: wrap the body in `collecting_cleanup_failures()`, classify each step (benign typed-exception -> drop; real -> append a `CleanupFailure`), continue through all steps; the context manager raises the collected group on exit. This is the largest part of the change (6 providers: local, docker, ssh, modal, vps, lima; vultr/ovh inherit vps). For the `vps` family the container-specific teardown is split into `DockerRealizer.teardown_placement` (which raises its own `CleanupFailedGroup`, absorbed by the provider), while `destroy_instance`/`delete_ssh_key` stay in `VpsProvider.destroy_host`.

### Orchestration (`execute_cleanup` / `_execute_stop` / `_execute_destroy`)

- The per-agent / per-host calls wrap each operation in `try: host.stop_agents(...) except CleanupFailedGroup as g: result.failures.extend(g.failures)` (likewise for `destroy_agent` / `destroy_host`), still recording the agent/host as acted-on. A bare `MngrError` (from `get_provider_instance` / `get_host` / a provider op that could not even be attempted) is a *different* exception type from the group and is caught separately and converted to a single `CleanupFailure` (category inferred from type: `HostNotFoundError`/`ProviderUnavailableError`/`LocalHostNotDestroyableError`/`NotImplementedError` -> `PROVIDER_INACCESSIBLE`; else `OTHER`).
- `ErrorBehavior.ABORT` still returns early after recording; `CONTINUE` proceeds to the next host/agent. Aggregation within a host/agent is unconditional.
- Agents are still appended to `stopped_agents` / `destroyed_agents` when the operation was attempted (even if it recorded failures) -- those lists reflect "we acted on this agent". A consumer distinguishes "fully clean" from "acted but incomplete" via `failures` and the exit code. Exception: a `PROVIDER_INACCESSIBLE` failure means we could not act at all, so those agents are **not** added to the stopped/destroyed lists.
- The offline-host STOP case (previously a warning appended to `errors`) is recorded as a real `PROVIDER_INACCESSIBLE` failure: the host is unreachable, so we cannot stop -- or even verify the state of -- its agents, and must not claim success we did not achieve. The affected agents are not added to `stopped_agents`. (DESTROY of an offline host goes through `destroy_host` and is unaffected.)

### CLI (`cli/cleanup.py`, and `stop` / `destroy`)

- After `execute_cleanup`, compute `exit_code_for_failures(result.failures)` and `ctx.exit(code)`.
- **Human output:** unchanged for the success lists; the error section prints each failure as `[<category>] <message>` and a final summary line naming the chosen exit code's cause.
- **JSON / JSONL output:** `failures` is emitted as a list of `{category, message, agent_name, host_id}` objects (replacing the old `errors` string list -- a schema change, see [Behavior changes](#behavior-changes)), plus an `exit_code` field.

## Behavior changes

- **`mngr stop` / `destroy` / `cleanup` now exit non-zero when a real failure leaves a resource behind.** Previously they always exited 0. Scripts that ignored the exit code are unaffected; scripts that now see a non-zero exit are seeing a previously-hidden failure.
- **JSON output schema change:** `errors: [string]` becomes `failures: [{category, message, ...}]` and an `exit_code` field is added. This is a breaking change for JSON consumers; called out here and in the changelog. (The minds desktop client's destroy flow consumes `mngr destroy`; verify it does not parse `errors` -- see [detached-destroy-flow](detached-destroy-flow/spec.md).)
- **No more silent swallowing:** failures previously hidden as warnings now surface in `failures` and the exit code. This may make previously-"passing" cleanups visibly report leaks; that is the intent.
- `CommandTimeoutError` is no longer raised *out of* `stop_agents`; it is caught internally and aggregated as a `TIMEOUT` failure (surfaced in the `CleanupFailedGroup`).

## Test plan

- **Classification unit tests** (host_test.py): for each stop command, a fake host whose handler returns the benign signature (exit/stderr) -> no failure; the real signature -> the expected category. Cover the kill-loop ESRCH-vs-EPERM split and the timeout -> `TIMEOUT` path (real local backend with `sleep`/explicit timeout, as today).
- **Aggregation tests:** a stop run where step A times out and step B reports a real non-zero -> both failures present in the raised `CleanupFailedGroup` (use the `get_cleanup_failures` test helper), all steps attempted.
- **Benign-only test:** every command "fails" benignly (session already gone) -> no failures, normal return, exit 0.
- **Per-provider destroy tests:** for each provider, a not-found exception -> benign (no failure); a real exception -> the mapped category. Use existing provider test fixtures / fakes.
- **Orchestration tests** (cleanup_test.py): `execute_cleanup` aggregates the raised `CleanupFailedGroup`'s failures into `result.failures`; a raised `MngrError` from provider access becomes a `PROVIDER_INACCESSIBLE`/`OTHER` failure; `PROVIDER_INACCESSIBLE` agents are not marked stopped/destroyed; offline-host stop is benign (no failure).
- **Exit-code mapping tests:** `exit_code_for_failures` returns the most-severe code for mixed failure lists; empty -> 0.
- **CLI tests** (cli/cleanup_test.py, stop_test.py, destroy_test.py): update the many `assert result.exit_code == 0` cases -- they remain 0 for clean/benign runs, and assert the specific non-zero code for runs with injected real failures. JSON output asserts the `failures`/`exit_code` schema.
- The original regression (`test_execute_cleanup_stop_on_online_host` hang) must still pass: a normal stop of a live agent records no failures and exits 0.

## Open questions / risks

- **VPS absent-signal:** the VPS-family container destroy steps (`remove_container`/`remove_volume`/`delete_btrfs_subvolume`, now in `DockerRealizer.teardown_placement`) avoid the "is this `MngrError` already-gone or real?" ambiguity by relying on no-op-on-missing helper semantics (`tolerate_missing`, `docker volume rm -f`) so that any raised error means a present resource that could not be removed. Note also `ContainerSetupError` (`libs/mngr_vps/imbue/mngr_vps/errors.py`), an `MngrError` subtype wrapping outer-host docker/snapshot `ConcurrencyExceptionGroup`/`ProcessError` failures, which keeps those failures catchable by the typed-exception classification path.
- **Severity order** is a judgment call (confirmed with the user: infra > processes > local-state > timeout > inaccessible > other). Encoded in one place (`exit_code_for_failures`) so it is easy to change.
- **Scope size:** touching 6 providers' `destroy_host` is the bulk of the work and the main risk; each provider's "already gone" detection must be verified against its SDK's actual exception types.
- **`stopped_agents` accounting** for partially-failed stops: an agent with a `PROCESSES_REMAIN` failure is still listed as `stopped` (we acted on it) but with a recorded failure. This is intentional; revisit if it proves confusing.
