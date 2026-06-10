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

All in `libs/mngr/imbue/mngr/api/data_types.py` unless noted.

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

**Propagation: return, don't raise.** Because the model is aggregate-and-continue, the low-level cleanup methods **return** their collected failures rather than raising. `Host.stop_agents`, `Host.destroy_agent`, and each provider's `destroy_host` change their return type from `None` to `list[CleanupFailure]` (empty = fully clean). This is an interface change on `HostInterface`/`OnlineHostInterface` (`stop_agents`, `destroy_agent`) and `ProviderInstanceInterface` (`destroy_host`).

Returning (rather than raising a carrier exception) keeps the success-accounting clean: `execute_cleanup` can record both "we acted on this agent" (append to `stopped_agents`/`destroyed_agents`) and "and here are the failures" without exception control-flow skipping the append. Exceptions are still used at the **orchestration boundary** for operations that fail so hard we could not act at all (e.g. `get_provider_instance` / `get_host` raising `HostNotFoundError`): `execute_cleanup` catches those `MngrError`s and converts them to a `PROVIDER_INACCESSIBLE` (or `OTHER`) `CleanupFailure` (see [Orchestration](#orchestration)). A `CommandTimeoutError` is **not** raised out of `stop_agents`; it is caught internally and returned as a `TIMEOUT` failure.

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
| VPS-Docker `remove_container` / `remove_volume` / `delete_btrfs_subvolume` | not-found (currently a swallowed `MngrError` warning -> needs a typed/inspected "absent" signal) | `HOST_RESOURCE_REMAINS` |
| VPS-Docker `vps_client.destroy_instance` | instance-already-gone (provider 404) | `HOST_RESOURCE_REMAINS` (a leaked paid instance is the worst case) |
| VPS-Docker `vps_client.delete_ssh_key` | key-already-deleted | `HOST_RESOURCE_REMAINS` (leaked credential) |
| Lima `limactl_delete` / `limactl_disk_delete` | VM/disk-already-gone (`LimaCommandError` with not-found text) | `HOST_RESOURCE_REMAINS` |
| `remove_host_from_known_hosts` | `FileNotFoundError` | benign always (cosmetic local file) |
| `_host_store.write_host_record` (mark DESTROYED) | -- | `OTHER` (record not updated; state inconsistency) |
| `LocalProviderInstance.destroy_host` | -- | `PROVIDER_INACCESSIBLE` (`LocalHostNotDestroyableError`: local host is intentionally not destroyable) |
| `SSHProviderInstance.destroy_host` | -- | `PROVIDER_INACCESSIBLE` (`NotImplementedError`) |
| `provider.get_host` / host record lookup | -- | `PROVIDER_INACCESSIBLE` (`HostNotFoundError`, `ProviderUnavailableError`) |
| `on_before_agent_destroy` / `on_destroy` hooks | -- | `OTHER` (plugin failure) |

**Provider "absent" signals that are currently only a swallowed warning** (e.g. VPS-Docker `remove_container`/`remove_volume` raising a generic `MngrError`) must be made distinguishable. Preferred: have the underlying helper raise a typed not-found (or return a "was-absent" boolean). Where that is impractical this pass, fall back to stderr/message matching consistent with the shell-command approach, and note it.

## Execution model

### Stop path (`Host.stop_agents`)

1. For each agent, run the collection + kill + kill-session steps best-effort, classifying each command via the shared helper. Do **not** abort on a real failure -- record a `CleanupFailure` (tagged with the agent) and continue to the next step / agent.
2. A timed-out step yields a `TIMEOUT` failure for that agent and we continue (the remaining steps are still attempted; if the tmux server is wedged they will likely also time out and add their own failures -- that is fine, they aggregate).
3. Return the collected `list[CleanupFailure]` (empty if everything was clean or only-benign).

This replaces the current fail-fast `raise_on_timeout` propagation. `raise_on_timeout` remains the **internal mechanism** the helper uses to *detect* a local timeout (a local timeout otherwise looks like a benign non-zero exit); the helper catches the resulting `CommandTimeoutError` and turns it into a `TIMEOUT` `CleanupFailure` rather than letting it propagate out of `stop_agents`.

### Destroy path

- `Host.destroy_agent`: runs `on_destroy` hook, `stop_agents`, `rm -rf <state_dir>`, `remove_persisted_agent_data`, each best-effort with classification. The `list[CleanupFailure]` returned by `stop_agents` is merged in. Returns the combined `list[CleanupFailure]`.
- Each provider `destroy_host`: wrap each step in classification (benign typed-exception -> drop; real -> `CleanupFailure`), continue through all steps, return the collected `list[CleanupFailure]`. This is the largest part of the change (7 providers: local, docker, ssh, modal, vps_docker, lima; vultr/ovh inherit vps_docker).

### Orchestration (`execute_cleanup` / `_execute_stop` / `_execute_destroy`)

- The per-agent / per-host calls now `result.failures.extend(host.stop_agents(...))` / `extend(host.destroy_agent(...))` / `extend(provider.destroy_host(...))`. A still-raised `MngrError` (from `get_provider_instance` / `get_host` / a provider op that could not be made to return) is caught and converted to a single `CleanupFailure` (category inferred from type: `HostNotFoundError`/`ProviderUnavailableError`/`LocalHostNotDestroyableError`/`NotImplementedError` -> `PROVIDER_INACCESSIBLE`; else `OTHER`).
- `ErrorBehavior.ABORT` still returns early after recording; `CONTINUE` proceeds to the next host/agent. Aggregation within a host/agent is unconditional.
- Agents are still appended to `stopped_agents` / `destroyed_agents` when the operation was attempted (even if it recorded failures) -- those lists reflect "we acted on this agent". A consumer distinguishes "fully clean" from "acted but incomplete" via `failures` and the exit code. Exception: a `PROVIDER_INACCESSIBLE` failure means we could not act at all, so those agents are **not** added to the stopped/destroyed lists.
- The offline-host STOP case (currently a warning appended to `errors`) is reclassified as **benign**: an agent on an offline host is not running, so it is already effectively stopped. No failure is recorded. (DESTROY of an offline host goes through `destroy_host` and is unaffected.)

### CLI (`cli/cleanup.py`, and `stop` / `destroy`)

- After `execute_cleanup`, compute `exit_code_for_failures(result.failures)` and `ctx.exit(code)`.
- **Human output:** unchanged for the success lists; the error section prints each failure as `[<category>] <message>` and a final summary line naming the chosen exit code's cause.
- **JSON / JSONL output:** `failures` is emitted as a list of `{category, message, agent_name, host_id}` objects (replacing the old `errors` string list -- a schema change, see [Behavior changes](#behavior-changes)), plus an `exit_code` field.

## Behavior changes

- **`mngr stop` / `destroy` / `cleanup` now exit non-zero when a real failure leaves a resource behind.** Previously they always exited 0. Scripts that ignored the exit code are unaffected; scripts that now see a non-zero exit are seeing a previously-hidden failure.
- **JSON output schema change:** `errors: [string]` becomes `failures: [{category, message, ...}]` and an `exit_code` field is added. This is a breaking change for JSON consumers; called out here and in the changelog. (The minds desktop client's destroy flow consumes `mngr destroy`; verify it does not parse `errors` -- see [detached-destroy-flow](detached-destroy-flow/spec.md).)
- **No more silent swallowing:** failures previously hidden as warnings now surface in `failures` and the exit code. This may make previously-"passing" cleanups visibly report leaks; that is the intent.
- `CommandTimeoutError` is no longer raised *out of* `stop_agents`; it is caught internally and returned as a `TIMEOUT` failure.

## Test plan

- **Classification unit tests** (host_test.py): for each stop command, a fake host whose handler returns the benign signature (exit/stderr) -> no failure; the real signature -> the expected category. Cover the kill-loop ESRCH-vs-EPERM split and the timeout -> `TIMEOUT` path (real local backend with `sleep`/explicit timeout, as today).
- **Aggregation tests:** a stop run where step A times out and step B reports a real non-zero -> both failures present in the returned list, all steps attempted.
- **Benign-only test:** every command "fails" benignly (session already gone) -> no failures, normal return, exit 0.
- **Per-provider destroy tests:** for each provider, a not-found exception -> benign (no failure); a real exception -> the mapped category. Use existing provider test fixtures / fakes.
- **Orchestration tests** (cleanup_test.py): `execute_cleanup` aggregates the returned failures into `result.failures`; a raised `MngrError` from provider access becomes a `PROVIDER_INACCESSIBLE`/`OTHER` failure; `PROVIDER_INACCESSIBLE` agents are not marked stopped/destroyed; offline-host stop is benign (no failure).
- **Exit-code mapping tests:** `exit_code_for_failures` returns the most-severe code for mixed failure lists; empty -> 0.
- **CLI tests** (cli/cleanup_test.py, stop_test.py, destroy_test.py): update the many `assert result.exit_code == 0` cases -- they remain 0 for clean/benign runs, and assert the specific non-zero code for runs with injected real failures. JSON output asserts the `failures`/`exit_code` schema.
- The original regression (`test_execute_cleanup_stop_on_online_host` hang) must still pass: a normal stop of a live agent records no failures and exits 0.

## Open questions / risks

- **VPS-Docker absent-signal:** several VPS-Docker destroy steps currently raise a generic `MngrError` for both "already gone" and real errors. Cleanly classifying them as benign may require threading a typed not-found through `remove_container`/`remove_volume`/`delete_btrfs_subvolume`. If that is out of scope for one pass, the fallback is message-matching, flagged in code.
- **Severity order** is a judgment call (confirmed with the user: infra > processes > local-state > timeout > inaccessible > other). Encoded in one place (`exit_code_for_failures`) so it is easy to change.
- **Scope size:** touching 7 providers' `destroy_host` is the bulk of the work and the main risk; each provider's "already gone" detection must be verified against its SDK's actual exception types.
- **`stopped_agents` accounting** for partially-failed stops: an agent with a `PROCESSES_REMAIN` failure is still listed as `stopped` (we acted on it) but with a recorded failure. This is intentional; revisit if it proves confusing.
