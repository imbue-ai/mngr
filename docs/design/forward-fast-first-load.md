# Fast first-load routing for remote minds in `mngr forward`

Status: design proposal (no implementation). Author: investigation task, 2026-07-08.

## 1. Problem and measured cost breakdown

When the minds desktop app launches and restores a window pointed at a **remote**
(`imbue_cloud`) mind, the window sits on the `mngr_forward` proxy's "Loading
workspace..." 503 loader for ~50-55s before it redirects in -- even when the
backing host is fully healthy, the container is up, and no restart is needed. A
**local** Docker mind in the same launch loads in ~20s. This was traced from live
staging logs; the remote host was up the whole time and answered the first real
request instantly once a route existed.

### 1.1 Why the proxy shows the loader

`mngr forward` byte-forwards `agent-<hex>.localhost/*` to a per-agent backend that
`ForwardResolver.resolve(agent_id)` produces. For the service-forwarding strategy
minds uses (`--service system_interface`), `resolve()` returns a `ProxyTarget`
only when **both** of the following are populated for the agent:

- the agent is in `_known_agent_ids` **and** its per-host SSH info is in
  `_ssh_by_agent` (for remote agents), and
- the requested service URL is in `_services_by_agent[agent_id]["system_interface"]`.

See `libs/mngr_forward/imbue/mngr_forward/resolver.py:153-167`:

```python
def resolve(self, agent_id: AgentId) -> ProxyTarget | None:
    ...
    if aid_str not in self._known_agent_ids:
        return None
    ssh_info = self._ssh_by_agent.get(aid_str)
    services = self._services_by_agent.get(aid_str, {})
    match self.strategy:
        case ForwardServiceStrategy(service_name=service_name):
            url = services.get(service_name)
            if url is None:
                return None          # <-- the 503 loader path
            return ProxyTarget(url=BackendUrl(url), ssh_info=ssh_info)
```

When `resolve()` returns `None`, the server emits an `UNRESOLVED` backend-failure
envelope and serves the styled 503 loading page
(`libs/mngr_forward/imbue/mngr_forward/server.py:579-582`,
`_service_unavailable_response` at `server.py:493-507`). The loader polls the
same URL once per second and only navigates in once it stops getting a 503
(`server.py:467-490`). So the user is blocked until the **first** successful
`resolve()`.

The routing table is **empty at every launch** -- nothing is persisted across
runs. So on a cold launch the entire ~55s is the time to populate those two
pieces of state for the workspace with an open window.

### 1.2 Where each piece comes from, and its latency

`mngr forward` under minds runs in `--observe-via-file` mode
(`apps/minds/imbue/minds/desktop_client/forward_cli.py:619-633`): it does not
spawn its own `mngr observe`, it tails the shared discovery events file written
by the single `mngr observe --discovery-only` under `mngr latchkey forward`.

**Piece 1 -- membership + SSH info: fast (~t+2.5s).** On attach,
`tail_discovery_events_file` replays from the cached snapshot offset
(`libs/mngr/imbue/mngr/api/discovery_events.py:1241-1274`), so the last
`DISCOVERY_PROVIDER` snapshot and the `HOST_SSH_INFO` events are delivered within
~200ms. These drive `resolver.update_known_agents(...)` and
`resolver.update_ssh_info(...)` (`stream_manager.py:352-359`, `386-410`). By
~t+2.5s the agent is "known" and its SSH tunnel target is set.

**Piece 2 -- the per-agent service URL map: slow (~t+55s).** This is the
bottleneck. The resolver learns service URLs **only** from a separate,
per-agent subprocess that the stream manager spawns:
`mngr event <agent-id> services requests --follow --quiet`
(`stream_manager.py:432-484`). Each spawned process's `services`-source lines are
parsed in `_on_event_output` and pushed via `resolver.update_services(...)`
(`stream_manager.py:496-535`).

Every one of these subprocesses is a full **cold `mngr` CLI startup +
`imbue_cloud` provider init + an SSH round trip** to the remote host to tail that
one agent's events file over SFTP (verified read path below, section 2.4). The
spawns are issued in a tight, **non-throttled** loop over a Python `frozenset`,
so their order is arbitrary and they all start almost simultaneously
(`stream_manager.py:361-368`; `run_process_in_background` returns immediately and
holds no semaphore -- `concurrency_group.py:421-464`). Under the resulting
cold-start CPU/IO contention among N simultaneous `mngr` processes, the remote
mind's stream effectively wins the contention race **last** (~t+23s in the traced
run) and then takes a further ~30s of cold provider-init + SSH connect + first
1s-interval SFTP poll before it delivers its first `service_registered` line
(~t+53-55s). The first `resolve()` success and first forwarded `GET /` land at
~t+55s.

### 1.3 Cost breakdown (from the traced remote-mind launch)

| Phase | Approx. window | Mechanism | Code |
|---|---|---|---|
| Membership + SSH info known | 0 -> ~2.5s | cached discovery snapshot replay via `--observe-via-file` tail | `discovery_events.py:1241-1274`; `stream_manager.py:352-410` |
| Per-agent `mngr event` streams spawned (burst, contended) | ~2.5s -> ~23s | non-throttled `frozenset` spawn loop; cold-start CPU/IO contention among N `mngr` processes | `stream_manager.py:361-368`; `concurrency_group.py:421-464` |
| Cold `mngr event` connect + first service registration delivered | ~23s -> ~55s | `imbue_cloud` provider init + SSH connect + SFTP + first 1s poll | `stream_manager.py:432-535`; `api/events.py` remote tail (section 2.4) |
| First `resolve()` success -> first `GET /` forwarded | ~55s | service URL now present | `resolver.py:160-167`; `server.py:579-584` |

Two caveats on this table, stated honestly:

- The "~20s queue" is **contention, not a FIFO queue**. There is no explicit
  serialization or per-process cap in the spawn path. Reordering the spawn loop
  alone changes nothing (all N processes still start within milliseconds of each
  other); only *reducing the contention* the priority stream faces would move
  this number. This matters for evaluating option (c) below.
- The split between the "~20s contended queue" and the "~30s connect" portions
  is inferred from the log timeline, not from a code-level guarantee. Pinning the
  intrinsic (single-stream, uncontended) cost of one cold `mngr event ... --follow`
  over SSH would need a targeted one-stream timing measurement. The *dominant*
  fact -- that first-load routing depends on a cold, per-agent, per-SSH
  `mngr event` mechanism -- is verified structurally and is what the design
  attacks.

The local Docker mind is faster (~20s) for the same structural reason with the
expensive parts removed: no SSH round trip (local file reads) and a lighter
provider init, so its cold `mngr event` reaches first delivery sooner.

## 2. Verified findings: where service registrations live, and when they are knowable

This section answers the hypothesis the whole design hinges on:

> "The per-agent service registrations (service-name -> URL) are available in, or
> can be cheaply added to, the discovery snapshot / certified data that the
> observe stream already produces ... If so, the resolver could be seeded from the
> snapshot and `resolve()` would succeed almost immediately."

**Verdict: the *literal* claim is FALSE, but the *underlying feasibility* claim is
TRUE.** Service registrations are **not** in the discovery snapshot today, and
they are produced by a fundamentally separate, later-materializing mechanism.
**However**, the data physically lives on the agent's host filesystem, co-located
with files the discovery poll **already reads over its single per-host SSH
visit** -- so it *can* be added to the snapshot **without any new per-agent SSH
cost**. Details and evidence follow.

### 2.1 Who produces service registrations, and where they live

Service registrations are written by the **in-container service processes
themselves** (they self-register when they bind a port), into a per-agent JSONL
file on the agent's host filesystem:

```
<host_dir>/agents/<agent-id>/events/services/events.jsonl
```

Each line is `{"timestamp":..,"type":"service_registered","event_id":..,
"source":"services","service":"<name>","url":"<url>"}` (or
`"service_deregistered"`). Concrete producers:

- The ttyd terminal plugin writes it from a shell snippet once it detects it is
  listening on a port -- `libs/mngr_ttyd/imbue/mngr_ttyd/plugin.py:72-85`
  (`printf '{... "type":"service_registered" ... "source":"services" ...}' >>
  "$MNGR_AGENT_STATE_DIR/events/services/events.jsonl"`).
- The minds hello-world example service does the same in Python --
  `apps/minds/examples/hello-world/server.py:157-186`.
- The `system_interface` service that minds forwards registers itself the same
  way from inside the workspace container (its writer ships in the workspace
  image / FCT template, not this monorepo; but the entire feature works today by
  the live `mngr event ... services` stream reading exactly this file, so
  `system_interface -> <url>` is written here).

The empty file is pre-created at agent-creation time
(`libs/mngr/imbue/mngr/hosts/host.py:2372-2415` `create_agent_state`, which
`touch`es `events/services/events.jsonl`).

**Key consequence:** the file lives on the **host's disk** (written by an
in-container process through the bind-mounted `MNGR_AGENT_STATE_DIR`). It is
host-side state at a per-agent path -- not a control-plane concept and not
something a laptop can know without reaching the host.

### 2.2 When service URLs become knowable

Only **after** the container is up and the service has bound its port and written
its registration line. The ttyd snippet writes only once it sees
"Listening on port"; the hello-world service writes only after its HTTP server is
listening. There is no way to know a service URL before its process is running.
This is exactly why they materialize late and why they cannot be derived from
lease/control-plane metadata.

This is the honest refutation of the literal hypothesis: service URLs are a
separate, later-materializing, in-container concern, distinct from the
lifecycle/label metadata discovery already carries.

### 2.3 What the discovery snapshot carries today (and does not)

A `DiscoveredAgent` carries `certified_data` (a free-form `Mapping`) --
`libs/mngr/imbue/mngr/primitives.py` (`DiscoveredAgent`). For `imbue_cloud` it is
populated straight from each agent's `data.json`
(`libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/providers/instance.py:530-551`,
`certified_data=data`). The `discovered_agent_from_agent_details` converter
(`discovery_events.py:287-304`) fills `type/work_dir/command/create_time/
start_on_boot/labels/plugin` -- **no service URLs**.

There is a related-but-different artifact: the listing collection script already
reads a single per-agent `status/url` file
(`libs/mngr/imbue/mngr/providers/listing_utils.py:107-108,153-154`), which
surfaces into `AgentDetails.url` on the full `mngr list` path
(`instance.py:893`). But (a) it is a **single** self-reported URL
(`base_agent.py:427-432` `get_reported_url`), not the named service map the
resolver looks up by service name, and (b) it is **dropped** when `AgentDetails`
is converted to the `DiscoveredAgent` that goes into the snapshot
(`discovery_events.py:287-304` omits it). So the streaming discovery path the
forward consumes carries **no** service URL of any kind today. This is why
`mngr forward --no-observe` explicitly forbids `--service`: "service URLs are not
in `mngr list` output" (`libs/mngr_forward/imbue/mngr_forward/cli.py:356-361`).

### 2.4 The read path for a remote agent's services (why it is slow)

`mngr event <id> services --follow` for a remote (`imbue_cloud`) agent resolves
the agent to an online host and reads its events over SSH/SFTP: for a
non-local host, `read_file` goes through paramiko SFTP
(`libs/mngr/imbue/mngr/hosts/outer_host.py` `read_file` -> `_get_file_via_paramiko`),
and `--follow` polls the whole `events/services/events.jsonl` file once per
second per source (`libs/mngr/imbue/mngr/api/events.py` remote tail loop,
`FOLLOW_POLL_INTERVAL_SECONDS = 1.0`). This is **one SSH connection + SFTP channel
per agent**, inside a **cold `mngr` process** the stream manager spawns per agent.
That per-agent cold-`mngr`-over-SSH mechanism is the entire ~50s cost.

### 2.5 The decisive feasibility finding

Crucially, `imbue_cloud` discovery **already reaches each host with exactly one
outer SSH visit per poll** and runs a shell script that **loops over every agent
directory on the host**, `cat`-ing per-agent files:

`libs/mngr/imbue/mngr/providers/listing_utils.py:80-111`:

```bash
if [ -d '{host_dir}/agents' ]; then
    for agent_dir in '{host_dir}/agents'/*/; do
        ...
        cat "$data_file"                       # data.json
        ...
        url=$(cat "${{agent_dir}}status/url" 2>/dev/null | tr -d '\n')
        echo "URL=$url"
        ...
    done
fi
```

Discovery flow for `imbue_cloud`: one control-plane API call lists leased hosts
(`instance.py:373-412`), then **one** outer root-SSH per host runs this listing
script (`instance.py:478-565`, `_collect_listing_raw_via_outer` at
`instance.py:567-615`) -- `docker exec` for a running container, `docker cp` +
the stopped-variant script otherwise. That single per-host script already reads
`data.json`, `status/url`, activity mtimes, and tmux info for **all** agents on
the host.

The per-agent `events/services/events.jsonl` files sit in the **same**
`<host_dir>/agents/<id>/` tree, right next to the `data.json` and `status/url`
the script already `cat`s. So discovery can read each agent's current service
registrations by adding a few more `cat`s **inside the loop that already runs** --
zero new SSH connections, just more bytes over the one connection discovery
already makes. The `HOST_SSH_INFO` needed to reach that URL is also already
emitted right after the snapshot (`provider_discovery_stream.py:167-184`,
`_build_lease_ssh_info` at `instance.py:715-729`).

That is the crux: **the cost of learning service URLs can move from "N cold
`mngr event` subprocesses, each with its own SSH connection and 1s SFTP polling"
to "one extra file read per agent inside the single per-host discovery script
that already executes."**

## 3. Proposed directions

All options keep `mngr_forward` app-agnostic (no `minds`/window concept inside the
plugin). The per-agent `mngr event ... services requests --follow` streams are
**retained in every option** -- they carry the `requests` source minds needs and
remain the authoritative live-correction path for services that register *after*
the last discovery poll (cold container start, service restart). The options
differ only in how the **initial** service map is seeded.

### Option (a): ship the service map in the discovery snapshot; seed the resolver at replay

**Idea.** Extend the discovery poll to read each agent's current service
registrations during its existing per-host visit, carry them on the snapshot, and
have `mngr_forward` seed `resolver.update_services(...)` from the snapshot at
replay time -- so `resolve()` succeeds at ~t+3s instead of ~t+55s.

**Mechanism / implementation shape (mngr side):**

1. In the listing script (`listing_utils.py`, both the running and stopped
   variants), for each agent also emit the contents (or a compact
   already-replayed form) of `events/services/events.jsonl`. Service events are
   infrequent, so the file is tiny.
2. Parse it in the `imbue_cloud` provider's listing parser and any other provider
   that shares this script, replaying `service_registered` / `service_deregistered`
   into a current `dict[service_name, url]` per agent.
3. Carry it on the snapshot. Cleanest is a dedicated, typed, optional field on
   `DiscoveredAgent` (e.g. `services: Mapping[str, str] | None`) rather than
   overloading `certified_data`, so consumers read it explicitly. This flows
   through `ProviderDiscoverySnapshotEvent.agents` unchanged.

**Mechanism / implementation shape (mngr_forward side):**

4. In `_handle_provider_snapshot` / `_setup_agent`
   (`stream_manager.py:333-378`), when a `DiscoveredAgent` carries a non-empty
   `services` map, call `resolver.update_services(agent_id, snapshot_services)`
   before/alongside starting the live event stream. The live stream, on connect,
   re-emits current registrations and overwrites the seed
   (`resolver.update_services` is a full replace -- `resolver.py:121-133`), so the
   seed is corrected within the normal window and never lingers stale.

This is app-agnostic: the plugin just reads a generic mngr discovery field.

**Expected latency.** First-load for a healthy, already-up remote container drops
from ~55s to **~t+3s** (bounded by the snapshot-replay time; SSH info is already
present at replay, and the seeded service URL completes `resolve()`). This
directly matches the observed reality that "the host answered the first real
request instantly once a route existed." Local Docker minds also improve
(smaller absolute win).

**Correctness / staleness.** The seed is only as old as the **last discovery
poll** (cadence `discovery_poll_interval_seconds`, ~10s class), and it is
refreshed by the very same discovery stream the forward already trusts, then
promptly overwritten by the live per-agent stream. Two cases:

- Container already up at poll time (the reported scenario): snapshot carries
  the correct `system_interface` URL -> instant route. `system_interface`'s
  in-container port is stable for a container's lifetime, so the URL rarely
  changes between polls.
- Container not yet up at poll time (cold container start): the services file is
  empty, the snapshot carries no service map, and behaviour is **exactly today's**
  (fall back to the live stream). No regression.

The staleness window is bounded and self-correcting -- unlike option (b). Even in
the worst case (a URL that changed within the last poll interval), the live
stream corrects it within its normal window, and a wrong-for-a-few-seconds route
is the same failure class the system already tolerates on any mid-session service
restart.

**Coupling.** Spans `mngr` (listing script + provider parsers + `DiscoveredAgent`
field) and `mngr_forward` (seed from snapshot). Does **not** touch `minds`.
`mngr_forward` stays origin-agnostic. The new field is a generic mngr concept.

**Risks.** (i) Requires touching the discovery snapshot schema and each provider
that should populate it. (ii) The `mngr latchkey forward` observer that writes the
shared discovery log must run a `mngr` new enough to emit the field; an older
observer simply omits it and the forward falls back to today's behaviour (safe).
(iii) Adds a small amount of data + a per-agent file read to each poll; negligible
vs. the SSH visit already paid.

### Option (b): persist the resolver's last service map to disk; seed at startup

**Idea.** Write the resolver's `_services_by_agent` to disk (e.g. under
`$MNGR_HOST_DIR/plugin/forward/`, where the signing key already persists --
`cli.py:98-99`) and load it at startup, correcting lazily as the live streams
catch up.

**Expected latency.** Same ~t+3s first-load *if* the persisted entry is still
correct.

**Correctness / staleness -- this is the disqualifier.** A persisted map can be
**arbitrarily** stale (from a previous session hours or days ago; a different
container; a service that moved ports). And a *wrong* seeded route is **strictly
worse than no route**, because of how minds' health machinery classifies the two:

`apps/minds/imbue/minds/desktop_client/system_interface_health.py:83-95`:

```
UNRESOLVED is ignored outright ... this self-resolves the moment [discovery
catches up], so enrolling would only mark a healthy workspace STUCK and
needlessly restart it ... A workspace that is present but unreachable does NOT
land here: discovery retains its (stale) route, so the dial failure surfaces as
CONNECT_ERROR / a 5xx, which still enrolls and still drives recovery.
```

Today's slow path is `UNRESOLVED` (no route) -- the loader waits patiently and
the health tracker **ignores** it, so a healthy-but-warming workspace is never
restarted. A wrong seeded route turns that into `CONNECT_ERROR` (a dial to a dead
endpoint), which **enrolls the agent for probing** and drives it to `STUCK` after
`stuck_threshold_seconds` (~5s), triggering the recovery UI / auto-restart of a
perfectly healthy workspace (`system_interface_health.py:236-264`). Persisting
across launches maximises the chance the seed is wrong, so this option risks
converting a slow-but-safe load into a spurious restart.

**Coupling.** `mngr_forward`-local (a persistence file), so lowest coupling. But
the correctness hazard above makes it a poor primary choice. If ever pursued, it
would need the seed to be **validated before use** (a cheap health probe through
the tunnel before trusting the persisted URL) and/or a hard freshness bound --
which is most of the cost of option (a) without option (a)'s guarantee that the
data came from the current discovery poll.

### Option (c): prioritize the open-window agent's event stream (`--priority-agent` hint)

**Idea.** `mngr forward` is deliberately app-agnostic and has no "window" concept,
but the app knows `window-state.json` before it spawns the forward
(`apps/minds/electron/main.js:137`). A clean, app-agnostic interface would be a
`--priority-agent <agent-id>` hint (repeatable) the app passes so the plugin
handles those agents' event streams first.

**Honest assessment of the ceiling.** As established in section 1.2, the spawn
loop is **not** a FIFO queue -- it is a non-throttled burst, and the ~20s is
cold-start CPU/IO contention. Therefore:

- **Pure reordering does nothing.** Moving the priority agent to the front of the
  `frozenset` iteration still starts all N processes within milliseconds; the
  priority stream still contends with N-1 siblings.
- **The only real lever is reducing contention for the priority stream**: spawn
  its `mngr event` first *and alone*, briefly deferring the other agents' spawns
  (e.g. a short delay or a small spawn window) so the priority stream cold-starts
  and connects uncontended, then release the rest. That is a genuine change to the
  spawn scheduler, not just an ordering tweak.
- Even done well, this only removes the contention portion. It **cannot** reach
  ~t+3s, because a single uncontended cold `mngr event` still pays `imbue_cloud`
  init + SSH connect + the first 1s SFTP poll before its first delivery. So the
  floor for (c) is "one cold stream's intrinsic connect time," not "snapshot
  replay time."

**Expected latency.** Improves the priority workspace from ~55s toward roughly the
single-stream intrinsic cost (needs measurement; plausibly ~10-20s), by removing
sibling contention. Non-priority workspaces are unaffected or slightly slower
(they yield CPU to the priority stream first).

**Correctness / staleness.** None -- it only reorders/defers work; every route
still comes from a live stream, so there is no stale-route hazard at all. This is
its main virtue.

**Coupling.** Adds one app-agnostic CLI flag to `mngr_forward` and a small change
to the spawn scheduler in `stream_manager.py`. `minds` passes the restored
window's agent id(s). The plugin never learns *why* an agent is prioritized. This
respects the app-agnostic constraint cleanly.

### Option (d): hybrid -- (a) as the fast path, (c) as the safety net

Ship (a) as the primary mechanism (instant seed for already-up containers, the
common case) and (c) as a cheap complement that helps the residual case where the
snapshot has no service map yet (a container that came up between the last poll
and launch). (a) gets the ~t+3s win whenever the container was up at poll time;
(c) shortens the fallback for the cold-container case by giving the open window's
live stream uncontended cold-start. The two do not conflict and target different
sub-cases.

## 4. Recommendation

**Primary: implement option (a)** -- carry the per-agent service map on the
discovery snapshot and seed the resolver at replay, retaining the live per-agent
streams as the correction path.

Rationale:

- It attacks the actual root cause (first-load routing depends on a cold,
  per-agent, per-SSH mechanism) rather than a symptom, and collapses the healthy
  common case to ~t+3s.
- It adds **no new per-agent SSH cost**: discovery already visits each host once
  per poll and already reads per-agent files in that visit
  (`listing_utils.py:80-111`); the service file is co-located. This is the finding
  that makes (a) cheap and is the reason to prefer it over anything that pays more
  SSH.
- Staleness is bounded to one poll interval, refreshed by the same trusted
  discovery stream, and overwritten by the live stream -- with a safe fallback to
  today's behaviour whenever the seed is absent. It never manufactures a
  wrong-across-launches route the way (b) does.
- It keeps `mngr_forward` app-agnostic and does not touch `minds`.

**Optionally add option (c)** as a low-risk complement (option d) if the
cold-container fallback case proves common enough to matter; it is independently
shippable and has zero staleness risk.

**Do not pursue option (b)** as a primary approach: persisting a service map
across launches maximises the chance of a stale route, and a stale route is
strictly worse than no route because it flips the failure from the health-ignored
`UNRESOLVED` class into the `CONNECT_ERROR` class that drives minds' STUCK /
auto-restart recovery (`system_interface_health.py:83-95`).

## 5. Key open questions and risks needing a human decision

1. **Snapshot schema surface.** Add a typed `services` field to `DiscoveredAgent`,
   or a parallel per-agent map on `ProviderDiscoverySnapshotEvent`, or stash it in
   `certified_data`? A dedicated typed field is cleanest but touches the discovery
   models (`extra="forbid"` discriminated union) and every producer; confirm the
   preferred shape and the back-compat story for older on-disk logs and older
   `mngr latchkey forward` observers (they must degrade to today's behaviour, not
   error).

2. **Which providers populate it.** `imbue_cloud` clearly benefits and shares the
   `listing_utils` script. Confirm whether the local Docker / Lima providers use
   the same listing path and should populate the field too (they should, for a
   smaller local-load win), and that the stopped-container (`docker cp`) variant
   handles the services file sensibly (a stopped container has no live services;
   emitting an empty map is correct).

3. **Poll cadence vs. freshness.** Is `discovery_poll_interval_seconds` short
   enough that "container up but service registered just after the last poll" is
   rare in practice? If not, quantify how often the fallback (live stream) path
   would still gate first-load, and decide whether (c) is worth adding.

4. **Single-stream intrinsic latency (bounds option (c)).** Measure one cold,
   uncontended `mngr event <remote-id> services --follow` from spawn to first
   delivery. This sets the floor for option (c) and validates the section-1.2
   contention-vs-intrinsic split. If the intrinsic cost is already many seconds,
   (c) is clearly dominated by (a) and may not be worth building.

5. **`status/url` vs. the services map.** This design reads
   `events/services/events.jsonl` (the exact source the live stream and resolver
   use), not the single `status/url` artifact. Confirm there is no simpler,
   equally-correct signal (e.g. if `status/url` is guaranteed to equal the
   `system_interface` URL) before adding a services-file read -- though reading the
   named service map is the robust choice regardless.

6. **Interaction with the recovery redirect's freshness math.** Minds' recovery
   path already compares outage onset against discovery snapshot timestamps
   (`system_interface_health.py:131-140`). Confirm that seeding a route from a
   snapshot does not perturb that comparison in a way that changes recovery
   decisions.
