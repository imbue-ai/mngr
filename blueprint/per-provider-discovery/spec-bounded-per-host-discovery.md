# Spec: bound per-host discovery reads (no unbounded abandoned-thread leak)

Status: implemented on branch `mngr/bounded-per-host-discovery` (PR #2353,
stacked on `mngr/per-provider-discovery` / PR #2335). Both changes below are in
place; the connect-phase timeouts were deliberately left untouched per the
non-goals.

Correction (post-implementation): the original draft of this spec assumed the
per-file `data.json` read (`read_text_file`) also went through
`execute_idempotent_command` / pyinfra `_timeout`. It does not -- `read_text_file`
reads over an SFTP channel (`_get_file` -> paramiko `SFTPClient`), not an SSH exec
command. Only the directory listing (`_list_directory` -> `ls`) uses
`execute_idempotent_command`. The implementation therefore bounds the two reads by
two different mechanisms (see Change 1 below): the `ls` via
`execute_idempotent_command`'s `_timeout`, and the SFTP read via
`channel.settimeout(...)` on the SFTP channel. Both still surface a stall as
`HostConnectionError` (via the existing transient-retry + `_translate_ssh_errors`
path) and land in the offline fallback, so the end-to-end behavior matches the
intent below.

## Context

Per-provider discovery added a per-host-bounded discovery path so that one slow host
cannot stall its whole provider's snapshot. `ProviderInstanceInterface.discover_hosts_and_agents_within_timeouts`
runs each host's agent read on its own daemon thread (via `cg.start_new_thread(is_checked=False, daemon=True)`),
waits up to `host_discovery_timeout_seconds` for all of them, and marks any host that
did not finish in time as **UNKNOWN** while **abandoning** its still-running thread
(Python threads cannot be killed).

The gap: the abandoned thread keeps running until the underlying operation returns, and
today the discovery read is not fully time-bounded, so a wedged host can leak threads.

### What we verified about the current read path (remote/SSH hosts)

The per-host read is `connected_host(provider, host_id)` -> `get_host()` (connect) ->
`host.discover_agents()` -> `_list_directory()` + per-file `read_text_file()`, all via
pyinfra's SSH connector.

- **Connect phase is already bounded** and needs no change: pyinfra passes paramiko
  `timeout=state.config.CONNECT_TIMEOUT` (default 10s) plus paramiko's default
  `banner_timeout` (~15s). A black-holed host fails connect in ~10s -> `ConnectError`
  -> `HostConnectionError` -> the existing offline fallback, well under a poll interval.
  So the connect path does **not** accumulate threads. **Explicitly out of scope: do
  not touch the connect timeouts.**

- **The exec/read phase is unbounded**, via two distinct paths:
  - `_list_directory` (the `ls`) calls `execute_idempotent_command` with
    `_timeout=None`. pyinfra threads `_timeout` into
    `read_output_buffers(..., timeout=_timeout)`, which does
    `gevent.wait((stdout_reader, stderr_reader), timeout=timeout)` and `raise
    timeout_error()` on elapse. With `_timeout=None` there is no bound.
  - `read_text_file` (each `data.json`) does **not** go through
    `execute_idempotent_command` -- it reads over an SFTP channel
    (`read_file` -> `_get_file` -> paramiko `SFTPClient`), which has no read timeout
    set, so a stalled transfer blocks indefinitely on the channel socket.

  Either way, a connection that establishes and then stalls mid-read (sshd hung after
  auth, network drops mid-read) blocks **indefinitely** -> the abandoned thread never
  returns -> threads accumulate on every poll.

So the only genuinely-unbounded case is the mid-read stall, caused by the missing read
timeout on both the `ls` exec and the SFTP file read.

## Purpose

Make the per-host discovery read time-bounded so abandoned threads self-terminate, and
bound thread accumulation to at most one in-flight read per host with a loud signal when
a host is genuinely wedged. Together: a slow/stuck host degrades to UNKNOWN (or its
offline last-known agents) without leaking threads or hiding the problem.

## Non-goals

- **Connect-phase timeouts** — already bounded (~10s/~15s); leave as-is.
- **`complete_names.py`** — intentionally left eventually-consistent (stdlib-only, hot
  TAB path; self-heals on the next poll). Not span-aware, not bounded here.
- **Producer-side intervening-event immediate re-poll** — a separate, deferred
  convergence-speed optimization; correctness is already handled by the aggregator.

## Change 1: bound the per-host reads with a hard timeout

Thread `host_discovery_timeout_seconds` down as a per-read wall-clock on the reads that
discovery issues, so each read self-terminates instead of hanging. As implemented:

- `Host.discover_agents` gained an optional `timeout_seconds` (the per-host-bounded path
  passes it; other callers leave it `None` for prior, unbounded behavior). It threads the
  timeout into the two reads it makes:
  - the directory listing, via `_list_directory(..., timeout_seconds=...)` ->
    `execute_idempotent_command(timeout_seconds=...)` (pyinfra `_timeout` on the `ls`);
  - each `data.json` read, via a **discovery-specific read helper** on `OuterHost`
    (`read_text_file_within_timeout` / `read_file_within_timeout`) that flows through the
    private `_get_file` chain to `channel.settimeout(timeout_seconds)` on the SFTP channel.
- The shared `read_file` / `read_text_file` methods (and the `HostFileReadInterface`) are
  left **untouched**, so no other caller is affected; only the new bounded variants and
  the private `_get_file*` chain gained an optional `timeout_seconds`.
- On timeout, pyinfra / paramiko raise (`TimeoutError` / `socket.timeout`), which the
  existing code (`hosts/host.py` for the exec path, `_translate_ssh_errors(timed_out=...)`
  for the SFTP path) converts to `HostConnectionError`. The discovery path
  (`_discover_agents_on_host_with_offline_fallback`) already catches `HostConnectionError`
  and falls back to the provider's offline (last-known) agents. So a slow host resolves
  to its offline agents *within the timeout* rather than hanging -- a better outcome than
  UNKNOWN, and bounded.

### Semantics / edge cases

- **Per-command, not a single wall-clock bound.** `discover_agents` does `ls` + N x
  `cat`, so the timeout bounds each op; a host with N agents can take up to ~N x timeout.
  The outer per-host wall-clock wait in `discover_hosts_and_agents_within_timeouts`
  remains the wall-clock guarantee. Change 1 only guarantees each op self-terminates,
  which is what stops unbounded accumulation.
- **Residual: `recv_exit_status()`** after `read_output_buffers` has no timeout (channel
  EOF but no exit status). Narrow corner (EOF normally implies the status is ready);
  acceptable, note it, do not over-engineer.
- Batch providers (modal / vps / imbue_cloud) that override
  `discover_hosts_and_agents_within_timeouts` to delegate to their batch path are
  unaffected -- they already accept provider-level timeout only.

## Change 2: cross-poll per-host in-flight de-duplication + warn

Make the per-host reads stateful across polls so a host whose previous read is still
running is never re-spawned, mirroring how `_ProviderDiscoveryPoller` already keeps a
single provider-level orphan future (`_in_flight_future`) and refuses to start a second
discovery while one is in flight.

- Track in-flight per-host reads across polls (a registry of host_id -> future, held for
  the poller's lifetime alongside the existing per-provider orphan handling). On each
  poll, for a host whose prior read is still running: do **not** start a new read --
  reuse the in-flight future (mark UNKNOWN again if still pending, or harvest its late
  result once it completes).
- **Warn every time a poll skips a host because its prior read is still in flight.** With
  Change 1 in place this should essentially never happen, so if it does it is a precise,
  loud signal that a host is wedged past its timeout (log at warning with host_id and
  provider). This is the observability requirement.

### Effect

- At most **one** abandoned/in-flight read per host at any time (not one per poll).
- With Change 1, that read self-terminates within its op-timeout, so even the single
  in-flight thread is short-lived; Change 2 is the backstop + the wedged-host alarm.

### Implementation note

Change 2 makes the per-host reads stateful, so they must run on a long-lived executor /
future-registry held by the poller rather than being spawned ad hoc inside
`discover_hosts_and_agents_within_timeouts` each call. This likely moves the per-host
in-flight bookkeeping up into `_ProviderDiscoveryPoller` (or a registry it passes into
the discovery call), reusing the pattern already established for the provider-level
orphan future.

## Testing

- **Change 1:** a gated host whose read blocks past the per-host timeout resolves to its
  offline agents (or UNKNOWN when no offline view) within the timeout, and the call
  returns promptly rather than hanging. Assert the timeout is actually passed to the
  discovery read (bounded), not `None`.
- **Change 2:** with a permanently-gated host, two consecutive polls start the read only
  once (the second poll reuses the in-flight future), and a warning is emitted on the
  skip. Use the existing gated-mock pattern from
  `provider_instance_test.py::_PerHostGatedProvider` /
  `provider_discovery_stream_test.py::_ControllableProvider`; `poll_until` for thread
  start; release the gate in a `finally` so no thread is left blocked.

## References

- `libs/mngr/imbue/mngr/interfaces/provider_instance.py` -
  `discover_hosts_and_agents_within_timeouts`, `_set_host_agents_future`,
  `read_host_agents_for_bounded_discovery`, `_discover_agents_on_host_with_offline_fallback`.
- `libs/mngr/imbue/mngr/api/provider_discovery_stream.py` - `_ProviderDiscoveryPoller`
  (the existing provider-level single-orphan pattern to mirror).
- `libs/mngr/imbue/mngr/hosts/host.py` - `discover_agents` (now takes `timeout_seconds`),
  `_list_directory` (now takes `timeout_seconds`), `_run_shell_command` (`_timeout`
  plumbing; existing `TimeoutError` -> `HostConnectionError` handling).
- `libs/mngr/imbue/mngr/hosts/outer_host.py` - `read_file_within_timeout` /
  `read_text_file_within_timeout` (the discovery-specific bounded read helpers), the
  private `_get_file` / `_get_file_with_transient_retry` / `_get_file_via_paramiko` chain
  (SFTP `channel.settimeout`), and `_translate_ssh_errors` (SFTP `TimeoutError` ->
  `HostConnectionError`).
- pyinfra `connectors/ssh.py` (`run_shell_command` passes `_timeout` to
  `read_output_buffers`) and `connectors/util.py` (`read_output_buffers` raises on
  timeout) -- the exec (`ls`) path only; the SFTP read is bounded by paramiko's channel
  socket timeout instead.
