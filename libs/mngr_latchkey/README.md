# mngr-latchkey

Latchkey gateway management for [mngr](https://github.com/imbue-ai/mngr).

This package owns the lifecycle of a single shared `latchkey gateway`
subprocess and the per-agent state that points the gateway at each
agent's own permissions file. It ships both as a Python library
and as a `mngr` CLI plugin that registers the `mngr latchkey`
command group.

## CLI

Once `imbue-mngr-latchkey` is installed, `mngr` discovers the plugin
via the standard entry-point mechanism and exposes:

```
mngr latchkey forward            # long-running supervisor: gateway + reverse tunnels
mngr latchkey create-agent-env   # emit LATCHKEY_* env vars + opaque permissions handle as JSON
mngr latchkey link-permissions   # swing the opaque handle's symlink to the canonical host path
mngr latchkey register-agent     # register an agent so it can reach the Minds API proxy
mngr latchkey admin-jwt          # mint a wildcard permissions-override JWT for the gateway
mngr latchkey gateway-info       # print the running gateway's URL + listen password as JSON
```

The plugin also hooks `mngr create`: once a new host's env is written and
before any agent starts, a host whose `LATCHKEY_GATEWAY` names
`host.docker.internal` but whose container cannot resolve that name (one
created before containers carried the mapping) is pointed at
`http://127.0.0.1:1989` instead, where the reverse SSH tunnel serves the
gateway.

`mngr latchkey forward` spawns the shared gateway eagerly on startup
and stops it on `SIGINT`/`SIGTERM` (coupled lifetime). It announces this
computer to every remote host's machine under a device id (`--device-id`, or
`MNGR_LATCHKEY_DEVICE_ID`, falling back to the mngr local host id; an embedder
such as the desktop app passes its own device id), which is how a workspace
tells the user's desktops apart (see [Desktops](#desktops)). Any in-flight
agents lose their gateway endpoint until the next `mngr latchkey
forward` is started; the per-host permissions files survive across
restarts.

While running, the supervisor also health-checks the shared gateway
subprocess: if it dies mid-session it is respawned on its original
port (so agent reverse tunnels and the published gateway port stay
valid), rather than leaving agent traffic silently broken until the
supervisor itself is restarted.

### Wiring a new agent using the CLI interface

```sh
# In one terminal, leave the supervisor running for the lifetime of the agents.
export MNGR_LATCHKEY_DIRECTORY=~/.minds/latchkey
mngr latchkey forward

# In another terminal, per host:
export MNGR_LATCHKEY_DIRECTORY=~/.minds/latchkey
mngr latchkey create-agent-env > /tmp/lk.json
OPAQUE_PATH=$(jq -r .opaque_permissions_path /tmp/lk.json)
HOST_ENV_ARGS=$(jq -r '.env | to_entries[] | "--host-env \(.key)=\(.value)"' /tmp/lk.json)

# Substitute your preferred mngr create invocation here. The latchkey
# env is passed via --host-env so every agent on the new host inherits
# the same gateway wiring.
CREATED=$(mngr create my-template $HOST_ENV_ARGS --format json)
HOST_ID=$(echo "$CREATED" | jq -r .host_id)
AGENT_ID=$(echo "$CREATED" | jq -r .agent_id)

# Finalize the opaque permissions handle: swing its symlink to the
# canonical host-keyed permissions path.
mngr latchkey link-permissions --host-id "$HOST_ID" --opaque-path "$OPAQUE_PATH"

# Register this agent for the host so it can reach the Minds API proxy.
# The baseline rule rejects every ``/minds-api-proxy/api/v1/agents/<id>/...``
# request whose ``<id>`` is not in the host's allowed-agent enum, so
# every agent that wants to call the Minds API must be registered
# here. Idempotent: re-running for an already-registered agent is a no-op.
# A host with a machine of its own (a remote workspace whose gateway was
# provisioned from this computer) is also handed the updated file, since
# its gateway enforces its own copy; the command fails if it cannot be.
mngr latchkey register-agent --host-id "$HOST_ID" --agent-id "$AGENT_ID"
```

## Settings

```toml
[plugins.latchkey]
directory = "~/.mngr/latchkey"   # default
latchkey_binary = "latchkey"     # default; resolved via PATH
```

Both fields are overridable via the matching env vars
(`MNGR_LATCHKEY_DIRECTORY`, `MNGR_LATCHKEY_BINARY`) and per-invocation
CLI flags (`--latchkey-directory`, `--latchkey-binary`). Precedence is
CLI flag > env var > settings.toml > built-in default.

## Logs

`mngr latchkey forward` writes its logs under the plugin data directory
(`<latchkey_directory>/mngr_latchkey/`):

- `events.jsonl` -- the supervisor's **structured** log, written via the
  standard mngr/Imbue Studio JSONL sink: one flat JSON object per line with a
  nanosecond `timestamp`, `level`, `message`, and source location,
  size-rotated (rotated copies `events.jsonl.<timestamp>`, oldest
  pruned). Read this when you need to observe timing. The shared
  `latchkey gateway` subprocess's output is routed through the same log
  (each line at `DEBUG`, prefixed with `[latchkey gateway]`), so it is
  timestamped and rotated too rather than living in a separate unrotated
  file.

- `latchkey_forward.log` -- the raw stdout/stderr capture of the detached
  supervisor process. Its file descriptor is handed straight to the
  process, so it cannot be rotated mid-write; instead the supervisor is
  spawned with `--quiet`, so in steady state it logs nothing here (all
  logging goes to `events.jsonl`). This file therefore stays effectively
  empty and only ever captures rare startup-failure output (Click errors
  or a pre-logging traceback) that never reaches the structured log -- so
  it is the place to look if the supervisor dies before it starts logging.

  Each spawn appends a `<timestamp> === spawning ... ===` marker before
  handing the descriptor over. The child's own lines cannot be stamped
  from the parent (it writes to the descriptor directly), so the marker is
  what dates whatever follows it, letting a traceback here be lined up
  against the timestamped logs uploaded alongside it. Spawn time is also
  the only moment the file can safely be rotated -- no child holds the
  descriptor yet -- so an oversized capture (one left by an older build,
  or by a child crash-looping before its logging is configured) is rotated
  there, once it passes 10MB, to `latchkey_forward.log.<timestamp>`, keeping
  only the newest rotation. Without that the file is append-only for the life
  of the install, and is gzipped and re-uploaded with every bug report.

## Troubleshooting

### Two forwards for one latchkey directory

The forward is supervisor-managed, and exactly one should be running per
latchkey directory: each holds an exclusive lock on its own directory for its
whole life, so a second one for the same directory refuses to start. Forwards
for *different* directories are expected, so tell them apart by the
`--latchkey-directory` in each title:

```sh
ps -eo pid,args | grep '[m]ngr latchkey forward'
kill <stray-pid>
```

`SIGTERM` runs the supervisor's teardown, which stops its `mngr observe` child,
its reverse tunnels and the shared gateway subprocess, so killing the
supervisor leaves nothing behind to clean up by hand.

### A remote host's gateway keeps failing to wire

Every discovery cycle (30s by default) re-runs each remote host's SSH wiring
steps -- the desktop-to-VPS tunnel, the desktop-gateway reverse tunnel, and VPS
gateway provisioning -- until they succeed, so a transient SSH failure (a
connection reset, a dead transport, a handshake blip, an authentication
timeout, or a host that is not answering yet -- whether its SYN is dropped,
refused, or has no route from this computer) heals by itself and is only logged
at `INFO` the first time and `DEBUG` on later cycles. A host that keeps failing that way for
`TRANSIENT_FAILURE_REPORT_THRESHOLD` (10) consecutive cycles while reporting as
running is logged once at `ERROR` with a traceback (which is what reaches
Sentry), then retried quietly until it succeeds, at which point a new outage
would report afresh. The host stopping (or reporting `UNAUTHENTICATED`) also
ends the streak, so failures before and after a restart are two separate
outages. Failures that retrying cannot fix -- trust material missing
on this computer, a rejected key, a malformed file -- are logged at `ERROR`
immediately.

A remote host that is restored onto new coordinates by anyone other than this
computer (an operator migration, a start from another device, a watchdog
re-drive) keeps its host id while its VM, address and ports all change. The
supervisor follows it within one discovery cycle: the container endpoint
discovery reports each cycle is compared against the one the host's gateway
route was resolved for, and a change drops the cached route, refreshes the
provider's host listing, removes the desktop-to-VPS tunnel to the old endpoint,
and re-runs the (idempotent) VPS gateway provisioning, since the recreated VM's
tmpfs holds no gateway secrets. A desktop-to-VPS tunnel failure also stops the
cached route being reused, so a move the comparison did not see still
re-resolves on the next cycle instead of streaking against a dead endpoint; the
route stays cached as the comparison's baseline, since a migration usually
announces itself as exactly this failure before discovery reports the new
coordinates. A failure of this computer's own end of the tunnel (trust material
missing on disk, a socket it could not bind) says nothing about the host and
keeps the route in use.

## Error reporting (Sentry)

`mngr latchkey forward` can report errors to Sentry. It is **off by default** and
configured entirely via `MNGR_LATCHKEY_SENTRY_*` environment variables (the
`MNGR_LATCHKEY_` prefix distinguishes `mngr latchkey` from the upstream core
`latchkey` project). The supervisor owns no Sentry project / environment
definitions: it receives concrete values as strings, which the embedder resolves
and passes in.

The **infrastructure** (which project, how the build is tagged) is snapshotted
into the daemon's environment when it is spawned:

- `MNGR_LATCHKEY_SENTRY_DSN` -- the Sentry DSN to report to.
- `MNGR_LATCHKEY_SENTRY_ENVIRONMENT` -- the Sentry environment label (e.g.
  `production`, `staging`, `development`).
- `MNGR_LATCHKEY_SENTRY_RELEASE` / `MNGR_LATCHKEY_SENTRY_GIT_SHA` -- the release
  version and git SHA events are tagged with.
- `MNGR_LATCHKEY_SENTRY_S3_BUCKET` -- the S3 bucket to upload the supervisor's
  logs (`events.jsonl`, rotated copies, `latchkey_forward.log`) and a captured
  traceback to. Empty / unset means there is no bucket, so nothing is uploaded.

Sentry initializes whenever `DSN`, `ENVIRONMENT`, `RELEASE`, and `GIT_SHA` are all
present (run standalone without them, it simply does nothing). They are required
together: the supervisor has no fallback of its own.

The **consent** -- whether to send reports at all (log/traceback attachments ride
along with reports) -- is read live, not snapshotted, so the embedder can toggle
it on a running daemon without respawning it:

- `MNGR_LATCHKEY_SENTRY_CONSENT_FILE` -- path to a JSON file
  (`{"report_unexpected_errors": bool}`) that the embedder writes and rewrites
  whenever the user changes their consent. The daemon reads it on every event, so
  a grant/revoke takes effect immediately. An absent/unreadable file means
  reporting is off.

Events are tagged with the `mngr-latchkey-forward` service name so they are
distinguishable from other Imbue Python processes that report to the same
projects. When the Imbue Studio desktop client spawns the supervisor it sets all of
these automatically -- resolving the DSN / environment / bucket from its own
Sentry settings and maintaining the consent file from the user's error-reporting
settings.

## Desktop egress

Some destinations block the datacenter IP ranges a remote workspace's VPS sits
in, so a request to them is only accepted when it leaves from the user's own
computer. Desktop egress sends a remote workspace's requests to a chosen
service out through one of the user's computers. The workspace does nothing
different: it calls its gateway as usual, and the machine's gateway decides per
request where the request leaves from, following the service's **route**: the
places to try, in order.

A request to a service whose route starts at a desktop takes these steps:

1. The workspace sends the request to the machine's gateway (on the VPS).
2. The machine's gateway runs its permission check, including the per-account
   rules, and injects the credentials from the machine's own store. It names
   the service it matched the request to in the request header
   `X-Latchkey-Matched-Service`.
3. The machine's gateway runs curl through the curl router
   (`LATCHKEY_CURL`). The router looks up the matched service in the rules
   file, finds its route, removes the `X-Latchkey-Matched-Service` header, and
   sends the request to the gateway of the first desktop on the route that it
   can reach, over that desktop's reverse tunnel, with a header asking the
   desktop gateway to inject no credentials. It knows the desktops from the
   records the forwarding extension reads (see [Desktops](#desktops),
   `LATCHKEY_EXTENSION_DEVICES_DIR`), and authenticates with the gateway
   password and the permissions-override JWT for this host that the record
   carries. A route may end at the machine itself, in which case the router
   makes the request directly when no desktop before it can be reached.
4. The desktop gateway runs its own permission check, then makes the request
   to the third party from the user's computer.

Credentials never leave the machine's store for this: they are injected on the
machine, and the desktop only forwards.

### The rules file

`~/.latchkey/proxyRules.json` on the machine gives services their routes. It
is one JSON object. Each key is a latchkey service name, and its value is the
service's route: a list of hops, tried in order.

```json
{
  "github": ["host-3f9c"],
  "slack": ["host-3f9c", "host-81d2", "self"]
}
```

A hop is one of:

- a desktop's device id: that desktop (see [Desktops](#desktops)). Every
  desktop a route sends requests to is named this way: no hop stands for
  whichever desktop happens to be connected;
- `self`: the machine itself, which makes the request directly. It is always
  there to do so, so nothing after it would ever be tried, and it is only
  valid as the last hop.

So `["host-a"]` sends the service's requests through one desktop,
`["host-a", "host-b"]` tries one desktop and then another, and
`["host-a", "self"]` tries a desktop and falls back to the machine. A route
names no place twice, and one without `self` fails the request when none of
its desktops can be reached.

A service the file does not name has the route `["self"]`: the machine makes
its requests itself. That route is never written out, so the file names only
the services that go anywhere else.

- The router does not match URLs itself. Latchkey states which service it
  matched a request to in the request header `X-Latchkey-Matched-Service`, and
  the router looks that name up in the file. Latchkey sets the header only on
  a request it injected credentials into, so a request it injected nothing
  into always leaves from the machine.
- Latchkey sets that header only with its diagnostic headers turned on. The
  package's `gateway-run` script exports `LATCHKEY_DIAGNOSTIC_HEADERS=1` for
  that.
- Latchkey puts its header ahead of the caller's arguments and leaves a header
  of the same name that the caller supplied in place. The router reads the
  first occurrence, which is latchkey's, and removes every occurrence before
  curl runs.
- Every catalog service can be given a route, including the ones whose URLs
  latchkey matches by regular expression, such as GitHub and AWS.
- A URL can belong to several services. The Google Drive files API, for
  example, is shared by Google Drive, Google Docs and Google Sheets. Latchkey
  reports the service whose credentials it used, so a route for `google-docs`
  does not apply to a request that latchkey served with `google-drive`
  credentials.
- The package's `gateway-run` script creates the file as `{}` when the machine
  has none, and exports its path as `LATCHKEY_DESKTOP_PROXY_CONFIG`. The router fails
  every request when that variable names a missing file, which is why the
  script that exports the variable also creates the file.
- The router reads the file on every request, so an edit takes effect without
  restarting the gateway.

The file used to hold a boolean per service, `true` sending the service's
requests to the most recently announced desktop. A file still in that shape
names no desktops, so a `true` in it is read as the desktops that hold a
[device-gated rule](#the-device-gated-rule) for every scope of the service, in
the order the host's permissions file lists them
(`desktop_egress_route_from_grants`), and as `["self"]` when there is none. A
`false` is read like a service the file does not name. Such a file is
rewritten as routes by the first route change made for its host.

This needs latchkey 3.15.0 or later, which added `LATCHKEY_DIAGNOSTIC_HEADERS`,
and latchkey-curl-shims v0.6.0 or later, the first release whose router reads
routes. The router tries a route's hops in order and sends the request to the
first one that is satisfied. A desktop satisfies its hop when its
[device record](#desktops) was touched in the last three minutes, and `self`
always does. The request is sent once: a failure at the desktop it went to is
a failed request, not a reason to try the next hop. A `true` in a file still
in the old shape goes to the most recently announced desktop whose record is
that fresh.

This computer keeps a copy of the file at
`<latchkey_directory>/mngr_latchkey/hosts/<host_id>/proxyRules.json`, handled
like its copy of the host's permissions file:

- `MachineCredentials.refresh` reads the machine's file in the same
  `read-state` as the credentials and the policy, and adopts it. The machine always
  wins. When the machine has no file, the copy here is removed. Unlike the
  policy, the machine is never seeded from the copy here.
- `read_host_desktop_egress_rules` returns the copy's text.
- A writer edits the copy here and its copy of the permissions file, then
  pushes snapshots of both with
  `MachineCredentials.set_permissions_and_desktop_egress_rules`, which costs
  one remote command. A permission grant that also changes a route pushes the
  granted account with them, still in one remote command
  (`MachineCredentials.connect_service_with_permissions_and_desktop_egress_rules`).
  An edit that is not pushed is discarded by the next refresh.

The rules file is the record of what the user chose. The rules in the
permissions file that make a desktop accept a routed request follow from it,
and are written in the same change as the route they serve (see below).

### The device-gated rule

The desktop gateway checks a routed request against the host's permissions
file (`hosts/<host_id>/latchkey_permissions.json` on this computer), like any
other request from that host. The rule that allows it is for one desktop: it
is gated on that desktop's device id by a `const`:

```json
{
  "rules": [{ "desktop-egress:<device id>:slack-api": ["any"] }],
  "schemas": {
    "desktop-egress:<device id>:slack-api": {
      "allOf": [
        { "$ref": "#/$defs/slack-api" },
        {
          "properties": {
            "customMetadata": {
              "type": "object",
              "properties": {
                "deviceId": { "const": "<device id>" },
                "account": { "type": "null" }
              },
              "required": ["deviceId"]
            }
          },
          "required": ["customMetadata"]
        }
      ]
    }
  }
}
```

The gate is on the device id because the permissions file is shared between
the user's computers: it is pushed to the machine, and every other computer
the user connects from adopts it. Without the gate, a rule for one desktop
would make every other computer forward the service's requests too.

A route needs one rule per scope of its service for each desktop it names
(`desktop_egress_forwarders`); a route of `["self"]` needs none. Whoever
writes a route writes these rules with it and deletes the ones no route needs
any more, so a desktop forwards exactly the services whose routes name it.
This is also why a route names its desktops one by one: there is no rule for
a desktop nobody named.

The device id reaches the check through detent's custom metadata. An embedder
starts the desktop gateway with `DETENT_CUSTOM_METADATA={"deviceId": "<id>"}`
in its environment (`imbue.mngr_latchkey.device_metadata.build_device_metadata_env`
builds the value; Imbue Studio passes it through the forward supervisor's
`extra_env`). Detent reads that variable as `customMetadata` whenever latchkey
supplies none of its own. Latchkey supplies `{"account": ...}` for every
request it injects credentials into, and supplies nothing for a routed
request, because it injects nothing into one. So `customMetadata.deviceId` is
visible only to checks on requests that carry their own credentials.

The `"account": { "type": "null" }` clause says the same thing in the rule
itself: latchkey reports the account whose credentials it injects, and it
injects none into a forwarded request, so the rule refuses any request that
carries one. One consequence is that a device-gated rule and a [per-account
grant](#per-account-grants) never match the same request: one requires
`customMetadata.account` and the other refuses it. Their order in the file
does not matter.

The permission is `any` because the desktop's check is only about whether this
computer sends the service's requests from its network. What the workspace may
do with the service was already decided on the machine, whose gateway ran the
per-account check before routing.

The desktop gateway runs with `LATCHKEY_PASSTHROUGH_UNKNOWN=1`. Latchkey
refuses a request that asks for no credential injection unless that is set.
The permission check still runs on such a request, and denies whatever no rule
allows.

`imbue.mngr_latchkey.desktop_egress` is the single owner of both shapes:

- `DesktopEgressRoute` is a route, and `build_desktop_egress_route` builds
  one from hops, refusing hops that do not form one with a
  `DesktopEgressError`. `desktop_egress_forwarders` gives the desktops a route
  names, and `desktop_egress_mode_for_route` says what an on/off switch on one
  computer can show for it (`DesktopEgressMode`): off for `["self"]`, on for
  the route through that computer alone, custom for any other.
- `build_desktop_egress_grant` composes a rule (key, permissions, backing
  schema) for one desktop, and `list_desktop_egress_grants` reads rules back
  by inspecting the schema structure. As with per-account grants, the rule key
  (`desktop_egress_scope_key`) is only a name and is never parsed.
- `serialize_desktop_egress_rules` and `parse_desktop_egress_rules` write and
  read the rules file. Reading gives a `DesktopEgressRules`: the routes the
  file holds, and the services that a file written before routes turned on
  with a bare `true`, whose routes `desktop_egress_route_from_grants` derives
  from the rules in the permissions file. `desktop_egress_route_for_service`
  gives a service its route from the routes read.

## Remote gateway package

A remote host's VPS gets its gateway as one Debian package, `mngr-latchkey`,
built by `imbue.mngr_latchkey.remote.package` from the `remote/debian/tree/`
directory in this repository, which is laid out exactly as the package's files
land on the machine:

```
DEBIAN/control, postinst, prerm, postrm      the package, and what installing it does
usr/bin/mngr-latchkey                        the one command the desktop drives the machine through
usr/lib/mngr-latchkey/read-state             "mngr-latchkey read-state": assemble what the machine holds
usr/lib/mngr-latchkey/apply-state            "mngr-latchkey apply-state": make the machine match a document
usr/lib/mngr-latchkey/announce-device        "mngr-latchkey announce-device": record a desktop as connected
usr/lib/mngr-latchkey/list-requests          "mngr-latchkey list-requests": the permission requests the machine keeps
usr/lib/mngr-latchkey/forget-request         "mngr-latchkey forget-request": drop one, and withdraw it from every desktop
usr/lib/mngr-latchkey/gateway-run            what supervisord runs as latchkey-gateway
usr/lib/mngr-latchkey/tunnel-run             what supervisord runs as latchkey-tunnel (see below)
usr/lib/mngr-latchkey/functions              sourced by all of the above
usr/lib/mngr-latchkey/extensions/            the desktop-gateway proxy extension
etc/supervisor/conf.d/latchkey-*.conf        the two supervisord programs
etc/nftables.d/mngr-bridge-services.nft      the policy keeping the bridge-bound ports on the docker bridge
etc/systemd/system/mngr-bridge-services-firewall.service   the oneshot that loads it at boot
var/log/mngr-latchkey/                       gateway.log and tunnel.log
```

Files ending in `.j2` are jinja templates rendered from the versions, ports,
paths and names the Python side decides (`RemotePackageContext`), so there is
one source of truth for both. The build is pure Python (tar + ar), so it runs
on a macOS desktop, and it is reproducible: the package version is
`<plugin version>+<hash of the sources and context>`, so `dpkg -l mngr-latchkey`
on a VPS says exactly which build it runs.

The gateway's share of provisioning a host (`provision_remote_gateway`, which
also provisions the owner-exec daemon on the same pass) is then five remote
operations, whatever state the machine is in: resolve the VPS's docker bridge
address (the one the gateway binds, and the owner-exec daemon with it), upload
the `.deb`, run a short bootstrap (apt dependencies, Node.js from NodeSource,
`dpkg -i`), read the machine, apply what it should hold. When this computer
has no key recorded for the machine, the read also brings the machine's
credential store back, still encrypted, so that if the machine knows no key
either, the desktop's key can be tried against it here before the store is
given up on. The package's `postinst` installs the pinned latchkey CLI and the curl
shims from `imbue-ai/latchkey-curl-shims` (both version-gated, the shims
verified against sha256 sums pinned in the package), loads the nftables policy
(and enables the unit that re-loads it at boot) before either bridge-bound
service can start, scrubs what the ad-hoc provisioning left on a machine it
set up, and registers the supervisord programs. The package is installed before the owner-exec
daemon is provisioned, for the same reason. apt is never run from a maintainer
script (dpkg holds its lock), which is why the bootstrap exists.

Every exchange with a provisioned machine afterwards is one of the five
commands, each a single remote command with a document on its stdin
(`<key> <base64>` lines in a quoted heredoc: data the shell never interprets,
and nothing from it reaches the `argv` of `mngr-latchkey`; of the `latchkey`
invocations its scripts make, only the service and account names travel in
`argv`, never a secret):

- `read-state` prints prefixed base64 answers: the secrets the gateway runs
  under, whether the machine has a credential store, its config, its policy,
  its desktop egress rules, the [format version](#data-format-changes) the
  policy is written in, whether it holds a reverse tunnel's keypair, when
  the document carries `container_host_id` the `--add-host` mappings that
  host's container was created with, and, when it carries
  `include_credential_store`, its credential store as it holds it, still
  encrypted under its own key. Nothing on the machine decrypts the store: the
  asking computer re-encrypts it for itself, so no key of that computer's
  reaches the machine.
- `apply-state` applies whichever entries the document carries, in a fixed
  order under one `set -e`: the machine's own key and listen password
  (adopted, refused if the machine already runs under different ones), the
  config, a credential bundle to merge or an account to clear, the desktop
  egress rules, the policy, the format version the policy and the rules are
  written in, the address the gateway binds (`gateway.conf`
  under `~/.latchkey`), the container to tunnel into, and a gateway restart.
  Whatever the document carries, it also refreshes the gateway's extension
  from the package's copy, so a restart loads the installed package's version.
  A machine that lost its key to a reboot is handed it back inside the same
  document, so that costs no extra round trip.
- `announce-device` records the desktop that sent it as connected to the
  machine (see [Desktops](#desktops)): its record, named by its device id,
  lands under the RAM-backed `devices/` directory.
- `list-requests` answers with the permission requests the machine keeps for
  the user's desktops (see [Permission requests across desktops](#permission-requests-across-desktops)),
  as one JSON array; `forget-request` drops the one the document names
  (`request_id`) and withdraws it from every connected desktop, through the
  machine's own gateway, best-effort.

The gateway binds the VPS's docker bridge address, which the agent's container
reaches as `http://host.docker.internal:1989` through the `--add-host` mapping
the VPS provider creates every container with. The `latchkey-tunnel` program
is not autostarted: provisioning hands `apply-state` a container to tunnel
into only for an agent whose `LATCHKEY_GATEWAY` names its own loopback -- a
container created without the mapping (the read reports none among its
creation-time extra hosts), or one an earlier provisioning already tunneled
into (the read reports the keypair that tunnel authenticates with) -- and the
tunnel then forwards the container's loopback port to the address the gateway
binds. That whole route is marked `CLEANUP:` in the code for removal once no
such container remains; each kept tunnel is logged at `INFO` so it is known
when that is.

`imbue.mngr_latchkey.remote._machine` owns the Python side of that protocol;
the credential transfers in `remote._transfer` and provisioning are both
clients of it. To look at a machine by hand:

```sh
dpkg -l mngr-latchkey
mngr-latchkey read-state </dev/null
ls -l /run/mngr-latchkey/devices/
supervisorctl status latchkey-gateway latchkey-tunnel
tail /var/log/mngr-latchkey/gateway.log
```

## Machine stores

Credentials belong to the machine that uses them: the user's computer owns one
credential store (shared by every local host), and each remote host's VPS owns
its own. So that a remote host's credentials can be *read* with the ordinary
offline latchkey commands -- and so a browser sign-in has somewhere to land
before it is handed over -- every remote host gets a **machine store**: its
existing per-host directory, made into a usable `LATCHKEY_DIRECTORY`. It is a
scratch pad, refilled from the machine (`MachineCredentials.refresh`) whenever
what it says is about to be shown or acted on, never trusted between times:

```
<latchkey_directory>/mngr_latchkey/hosts/<host_id>/
    credentials.json.enc          this machine's credentials      (owned)
    data-format-version           the mirror's own format stamp   (owned)
    latchkey_permissions.json     the machine's policy, cached    (owned)
    proxyRules.json               its desktop egress rules, cached (owned)
    permissions-format-version    the format the policy is in, cached (owned)
    machine_encryption_key        the machine's own key           (owned)
    machine_gateway_password      the machine's own password      (owned)
    permissions.json           -> latchkey_permissions.json
    config.json                -> the desktop's config.json
    browser_state.json.enc     -> the desktop's browser state
    encryption_key             -> the desktop's encryption key
    last-daily-count           -> the desktop's usage-ping stamp
```

The last of those is shared for a plainer reason than the rest: it rate-limits
latchkey's once-a-day usage ping, which is about the user, so an unshared stamp
would make an ordinary offline read against each machine store ping once a day
per remote host.

`imbue.mngr_latchkey.remote._mirror` owns that layout. Two properties are worth
knowing:

- **A mirror is held under the desktop's key**, whatever key the mirrored
  machine uses for its own store. Upstream encrypts the browser session with
  the same per-directory key as the credential store, so this is what lets every
  machine store share one browser session (which keeps signing a second machine
  in to a service down to a consent click rather than a full re-login).
  Transfers re-encrypt at the boundary instead, and always on this computer,
  with the copy of the machine's key it records: what is shipped to a machine
  is encrypted with *its* key (`Latchkey.export_credentials_subset`'s
  `destination_key`, handed to the CLI on stdin so it never reaches `argv`),
  and a store read back arrives under the machine's key and is re-encrypted
  for the desktop here (`Latchkey.reencrypt_foreign_store`). The desktop's own
  key never leaves this computer.

- **Each machine's key is recorded in its machine store -- as a mirror, not the
  truth.** A machine holds its own key only in RAM (provisioning writes it to a
  tmpfs file, deliberately never to the disk beside the encrypted store), and
  that RAM copy is authoritative while it exists: any of the user's computers
  may have provisioned the machine, so each desktop's provisioning pass *adopts*
  the key the machine is already running under rather than deciding one, and
  keeps the durable copy so a rebooted machine (whose tmpfs is wiped) can be
  handed its key back. Only a machine that is not running a key, with none
  recorded here, gets one decided: a fresh key when it holds no credential
  store; the desktop's key when its store verifiably opens under it (a machine
  provisioned by a build older than minds-v0.5.1, which gave every machine the
  desktop's key), tried here against the copy of the store the read brought
  back, so the desktop's key reaches the machine only once it is known to be
  the machine's own; and otherwise -- a store written under a key held only by a computer that is
  gone -- the store is abandoned and a fresh key minted, because signing in
  again is possible and waiting for a computer that may never return is not.

- **A machine's gateway listen password is recorded the same way, and is
  adopted for a blunter reason.** It is the password the workspaces on that
  machine present (`LATCHKEY_GATEWAY_PASSWORD`), and their host env file is
  written once, at `mngr create`, with nothing to rewrite it afterwards. So the
  value the creating computer chose is fixed for the machine's whole life: a
  second computer that wrote its own here would answer every request those
  workspaces make with a 401. Provisioning therefore adopts what the machine is
  running under, falls back to the record here for a machine whose tmpfs a
  reboot wiped, and only seeds its own value (`Latchkey.derive_gateway_password`,
  which is also what it bakes into the workspaces it creates) into a machine
  neither is true of. The password the *desktop* gateway listens on is a
  separate secret, and the one place the two used to be the same is described
  under [Remote desktop-gateway proxy
  extension](#remote-desktop-gateway-proxy-extension).

- **A machine store is not a plugin root.** `Latchkey.plugin_data_dir` would
  resolve to a nested `mngr_latchkey/` underneath it, and `initialize()` would
  rewrite the shared `config.json` through its link, so only the
  credential/service-introspection subset of `Latchkey` may be pointed at one.

`imbue.mngr_latchkey.remote.credentials` is how a machine is reached.
`MachineCredentials` is built for the duration of one exchange -- the caller
opens the machine's outer host, does what it came to do, and lets both go --
and every method costs a single remote command: `connect_service`,
`disconnect_account`, `set_permissions`, `connect_service_with_permissions`,
`set_permissions_and_desktop_egress_rules` and
`connect_service_with_permissions_and_desktop_egress_rules` push, and `refresh` reads the
machine's credentials, its policy *and* its [desktop egress
rules](#the-rules-file) back in one go. Nothing is queued: an exchange either succeeds before its caller
returns or raises `RemoteGatewayError`, so an embedder (the Imbue Studio desktop app)
can block a user's click on it and report what the machine said.

Those scripts never reach the logs. Each one embeds what it is moving -- a
credential store, the machine's own key, or the policy being applied -- so its text is as sensitive as what it carries, and at a whole
base64-encoded store on one line it is far too big to read anyway. Each is run
inside `commands_kept_out_of_logs` (`imbue.mngr.utils.command_logging`), so the
host layer traces a stand-in naming the kind of script and its size in place of
the command, and pyinfra's own echo of the command it is about to run is
dropped along with it.

`refresh` also settles which side wins, and for both halves the answer is the
machine. The credentials are obviously its own -- only it can rotate the tokens
it holds. The policy is its own for a less obvious reason: the user may have
more than one computer, and any of them can push a grant. A desktop that
treated its own copy as the truth would quietly revert what another computer
granted, so what the machine holds is adopted here instead. The one write
`refresh` makes toward the machine is the seed: a machine with no policy at all
gets this desktop's copy, which is how a freshly provisioned gateway stops
permitting everything.

What keeps that safe is that the copy here is never edited *without* being
pushed. Every writer of `latchkey_permissions.json` -- a UI toggle, a grant, an
agent registration, a recovery repair -- pushes the result to the machine in the
same breath, and a push that fails is reported (to the user when there is one to
report to, to the log otherwise) rather than left behind as a local edit that a
later refresh would silently discard.

The one write `refresh` makes after adopting is the migration: a machine whose
policy is in a format older than this build writes has it brought up to date here
and handed back in a second command (see [Data-format
changes](#data-format-changes)).

## Permissions config

The package owns the `latchkey_permissions.json` schema (a subset of
detent's rule format). Per-host edits go through the gateway's
bundled `permissions` extension (see [Gateway HTTP extensions](#gateway-http-extensions));
only the deny-all default, the admin file, and the per-agent opaque
baseline are written directly via `imbue.mngr_latchkey.store.save_permissions`.

### Per-account grants

Third-party service access is granted **per latchkey account**, not per
service. Latchkey (>= 3.2.0) tells detent which account's credentials it
injected into a request (as `customMetadata.account`; the unnamed default
account is the empty string), and detent (>= 1.11.0) can compose schemas, so
each grant is a rule keyed `<scope>:<account>` backed by a generated schema
that intersects the built-in scope with that account:

```json
{
  "rules": [{ "slack-api:hynek@imbue-ai": ["slack-read-all"] }],
  "schemas": {
    "slack-api:hynek@imbue-ai": {
      "allOf": [
        { "$ref": "#/$defs/slack-api" },
        {
          "properties": {
            "customMetadata": {
              "type": "object",
              "properties": { "account": { "const": "hynek@imbue-ai" } },
              "required": ["account"]
            }
          },
          "required": ["customMetadata"]
        }
      ]
    }
  }
}
```

Detent stops at the first rule whose *scope* matches, and a request made with
another account does not match this one, so per-account rules simply stack.

The `<scope>:<account>` **name is only a naming convention** -- a stable,
human-readable identifier -- and is never parsed: both a detent scope name and
an account may legitimately contain a colon. Everything that needs to know what
a rule grants inspects the *schema structure* instead (the `$ref` to the base
scope next to the `customMetadata.account` gate).

The name is still required to be *unique* per (scope, account) pair, since the
gateway merges rules by key, so the scope half is percent-escaped (`%` -> `%25`,
then `:` -> `%3A`) before the two are joined. That makes the mapping injective
whatever either half contains -- and it is the identity for every scope name the
catalog ships, so keys read exactly as above. The account is the last field and
is never escaped.
`imbue.mngr_latchkey.account_scopes` is the single owner of both sides of that
structure: `build_account_grant` composes a grant (key + permissions + backing
schema) and `list_account_grants` / `resolve_account_scope` /
`resolved_schema_names` read grants back.
`ServicesCatalog.list_service_account_grants` layers the catalog on top, which
is what every consumer (the Imbue Studio connectors page, the permission dialog's
pre-check, the revoke paths, and VPS credential sync) actually calls. The
gateway's `permission_requests` extension carries a JavaScript copy of the two
*generating* helpers (it computes a pending request's effect in-process), guarded
against drift by `account_scopes_test.py`; nothing on the JavaScript side reads
grants back.

Minds' own gateway-self scopes (`latchkey-self`, `minds-api-proxy-*`) stay
account-agnostic: latchkey attaches no account metadata to requests an
extension serves, so an account-gated schema would never match them.

## Data-format changes

A change to the shape of a host's `latchkey_permissions.json` that the readers
cannot absorb ships as a **permissions migration**
(`imbue.mngr_latchkey.migrations`). Migrations are per host, because that is
where the source of truth lives: each host's directory carries a
`permissions-format-version` stamp, one integer naming the format its policy is
written in, and a directory without one is at version 0. A host with a machine
of its own keeps the same stamp beside its policy in the machine's
`~/.latchkey`, where it is the source of truth; every push of the policy carries
the stamp of the copy it was taken from, and every read adopts the machine's
stamp with the policy. (It is a different file from upstream's
`data-format-version`, which sits in the same directories and stamps the
credential store.)

A migration is a `PermissionsMigration` with a `version` (consecutive from 1)
and an `apply(permissions, context)` that takes the parsed policy
(`LatchkeyPermissionsConfig`) as the version below wrote it and returns it as
its own version writes it. The `PermissionsMigrationContext` names the desktop
running the migration (its device id). The runner does the reading, writing
and stamping.
The build's migrations are listed in `migrations/runner.py`
(`PERMISSIONS_MIGRATIONS`), and the version the last one ends in is what a
policy this build creates is stamped with, so a fresh file is never migrated.

The runner (`migrate_permissions`) compares a host's stamp against the build's
and applies the migrations above it in order, feeding each the last one's
result and re-stamping after each, so a failure leaves the stamp at the last
step that completed. It runs:

- for a host with a machine of its own, right after the machine's policy has
  been adopted -- by `MachineCredentials.refresh` and by the provisioning pass
  -- and the migrated policy is handed back to the machine, stamp and all, in
  one command (`migrate_permissions_and_push`). So a machine is never left
  holding a policy this build has read but cannot edit, and a second computer
  that reads the machine afterwards finds it already migrated.
- for a host without one (a local host, or a remote host no provisioning pass
  from this computer has reached), in place, when `mngr latchkey forward`
  starts and before the gateway that reads the file does.

The stamp is written after the policy it describes, on this computer and on the
machine alike, so a failure between the two re-runs the migration rather than
skipping it: a migration must leave a policy already in its target shape alone.

The migrations so far:

1. File-sharing grants name the desktop whose file they share. Each
   `minds-file-server-<access>-<path>` gains a twin
   `minds-file-server-<access>-<device id>:<path>`, whose URL pattern sits
   under the device id. A policy from before was granted by the only desktop
   there was, so the twins name the desktop migrating it. The original grant
   stays beside its twin, so a workspace built against the device-less URL
   keeps reaching what it was given; a later migration deletes these once no
   supported workspace uses that URL.

A policy stamped *newer* than a build knows is refused rather than read: a build
that does not know a format cannot edit a policy in it without corrupting it. A
refresh raises `PermissionsFormatNewerError` (the Permissions tab says so),
while the provisioning pass and the forward's startup sweep log a warning and
leave the policy as it is, since a gateway has to be wired whichever build wrote
it. An older build's *edits* to such a policy are not guarded against: the
user's computers are assumed to run builds no further apart than one can
migrate.

---

# Reference

The sections below are deeper detail for power users, front-end authors,
and embedders. Most callers only need the CLI above.

## Gateway HTTP extensions

`mngr latchkey forward` drops three desktop-only `.mjs` extensions into
`<latchkey-directory>/extensions/`. All expose plain HTTP endpoints
on the gateway's listen port and authenticate the caller via two
headers:

* `X-Latchkey-Gateway-Password: <password>` -- the gateway listen
  password from `mngr latchkey gateway-info`.
* `X-Latchkey-Gateway-Permissions-Override: <jwt>` -- a JWT minted
  for the permissions file you want the gateway to evaluate the
  request against. For full access to both extensions, use the JWT
  from `mngr latchkey admin-jwt`.

A shell client would typically wire these up once:

```sh
ADMIN_JWT=$(mngr latchkey admin-jwt)
eval "$(mngr latchkey gateway-info | jq -r '@text "GATEWAY_URL=\(.url); GATEWAY_PASSWORD=\(.password)"')"
auth=(-H "X-Latchkey-Gateway-Password: $GATEWAY_PASSWORD" -H "X-Latchkey-Gateway-Permissions-Override: $ADMIN_JWT")
```

### `permission-requests` extension

A pending-permission queue. Agents submit a request when they hit a
blocked service; UIs (the Imbue Studio desktop client, your own front-end)
consume the stream and approve/delete on resolution.

* `POST /permission-requests` with body
  `{"agent_id": "...", "rationale": "...", "type": "...", "payload": {...}}`,
  and optionally a filename-safe `request_id` of the caller's choosing (an id
  already pending is a 409), which is how one request keeps a single identity
  across the user's desktops: a remote host's machine assigns it before
  forwarding (see [Permission requests across desktops](#permission-requests-across-desktops)).
  Two `type` values are accepted:
  * `"predefined"` -- detent scope/permission grant for one signed-in
    account of the service, with payload
    `{"scope": "...", "permissions": ["...", ...], "account": "...",
    "proxy": true}`.
    The scope must be one named in the bundled `services.json` catalog,
    and each permission must be either one the catalog lists for that
    scope or the catch-all `any`. `account` is the latchkey account the
    grant applies to (the unnamed default account is the empty string);
    it is optional, and an agent that does not know which account to use
    omits it. A request with no account has an **empty** `effect` -- it
    can only be resolved by a client that names the chosen account in the
    approve override body (see below), which is what the Imbue Studio dialog
    does after the user picks or signs one in.

    An optional `"proxy": true` asks Imbue Studio to also send the service's
    requests out through the user's computer once the grant is approved: the
    computer that approves the request is put first on the service's
    [desktop egress route](#the-rules-file). The user decides in the approval
    dialog, and a request cannot name a route or any other computer. The
    extension only checks that `proxy` is a boolean (anything else is a 400)
    and stores `true`; `false`, `null` and an absent `proxy` store nothing.
    It never acts on it: like a file-sharing `sync` it is not a permission,
    so it does not enter the `effect`.
  * `"file-sharing"` -- access to one path through the `minds-api-proxy`
    extension, with payload `{"path": "<absolute-path>", "access":
    "READ"|"WRITE"}`. The path must be absolute (or start with `~`, which is
    expanded) and free of `..` segments. An optional `"sync": {"conflict":
    "NEWER"|"THIS_COMPUTER"|"WORKSPACE"}` (`conflict` itself optional,
    defaulting to `NEWER`) asks Imbue Studio to also keep a synchronized copy of
    the folder on the workspace's machine once the grant is approved. The
    extension validates and stores it but never acts on it: a sync is not
    a permission, so it does not enter the `effect`.

    The path is always on the desktop the gateway runs on: the grant is
    minted for its own device id (`LATCHKEY_EXTENSION_LOCAL_DEVICE_ID`, set
    by the forward supervisor from `--device-id`), as a permission named
    `minds-file-server-<access>-<device id>:<path>` matching
    `/minds-api-proxy/api/v1/files/<device id><path>`, which is where that
    desktop's file server serves the path. A desktop therefore never grants
    access to another desktop's files; a workspace asks a particular desktop
    by sending the request there (the `X-Latchkey-Device` header, see
    [Desktops](#desktops)). A gateway without a device id refuses
    file-sharing requests with a 503. `imbue.mngr_latchkey.file_sharing`
    reads these names back. It also reads the names from before grants named
    a desktop (`minds-file-server-<access>-<path>`, matching
    `/minds-api-proxy/api/v1/files<path>`), which a policy that already holds
    them keeps but the extension never mints.

  The extension generates a `request_id` server-side, stores the
  caller-supplied fields plus the `target` permissions.json (taken
  from the extension context) and a precomputed `effect`
  (`{rules?, schemas?}`) that an approval would splice into
  `target`, stamps the filing time as `created_at` (ISO-8601 UTC),
  and returns the full persisted record. Available to agents.
* `GET /permission-requests` returns the current queue as
  newline-delimited JSON. Each line carries the full persisted
  shape. Add `?follow=true` to keep the connection open and stream
  every newly-POSTed request as it arrives, and a line
  `{"event": "deleted", "request_id": "..."}` for every request that stops
  being pending through the extension (approved, or deleted). Available to
  the admin.
* `POST /permission-requests/approve/<request_id>` approves the
  named request: the extension reads it, splices its `effect` into
  its `target` permissions.json (creating the file if missing,
  merging rules by scope key and schemas by name), then removes the
  pending request file. Returns `200` with `{request_id, target,
  applied}` where `applied` is the freshly-rewritten permissions
  file. Available to the admin.

  An optional JSON body overrides what the approval grants, recomputing
  the effect from the user's choices: `{"account": "...",
  "permissions": [...]}` for a `predefined` request (the permission list
  is optional), `{"path": "..."}` for `file-sharing`, and
  `{"permissions": [...], "target_workspace_id": ...}` for `workspace`.
* `DELETE /permission-requests/<request_id>` removes a single pending
  request without applying its effect. UIs call this on deny so a
  fresh `?follow=true` consumer never sees the resolved request
  again. Available to the admin, and to every agent for the ids it was told
  (the baseline grants it, so a remote host's machine can withdraw a request
  one desktop answered from the others, and an agent can withdraw its own).

Pending requests are stored as one JSON file per request under
`<latchkey-directory>/permission_requests/v3/`. The `v3` segment is
the on-disk schema version; future shape changes get a new directory
rather than trying to migrate files in place (`v3` introduced the
per-account `predefined` payload).

### `minds-api-proxy` extension

Transparent HTTP reverse proxy from the gateway to an embedder-supplied
"Minds API" base URL.

* `ANY /minds-api-proxy` forwards to `<minds-api>/`.
* `ANY /minds-api-proxy/<rest>...` forwards to
  `<minds-api>/<rest>...`, preserving the inbound method, query
  string, headers (minus hop-by-hop entries and the gateway-internal
  password / permissions-override headers), and body. The upstream
  response status, headers, and body stream straight back.

The upstream base URL is read from the
`LATCHKEY_EXTENSION_MINDS_API_URL` env var on every request. If the
var is unset/empty/unparseable the proxy responds 503 with a JSON
error body. There is no in-process cache to invalidate: an embedder
that needs to repoint the proxy at a new upstream simply respawns
the gateway (or the `mngr latchkey forward` supervisor that owns it)
with a fresh value for the env var.

The proxy authenticates *to* the upstream Minds API on behalf of the
agent. When `LATCHKEY_EXTENSION_MINDS_API_KEY` is set, the proxy
overwrites the inbound `Authorization` header with
`Bearer <LATCHKEY_EXTENSION_MINDS_API_KEY>` before forwarding. Agents
therefore never see the key, and an agent that tries to spoof an
`Authorization` header has its value dropped on the floor. When the
env var is unset, the inbound `Authorization` value is forwarded
unchanged (useful for tests / local fixtures that do not bother
stubbing the key; the upstream will simply 401 the request).

Other than the `Authorization` overwrite, the extension performs no
authentication of its own beyond the gateway's normal permission
check (against the synthetic `latchkey-self.invalid` URL). Restricting
which paths an agent can reach through the proxy is therefore a job
for the agent's `latchkey_permissions.json`.

### Remote desktop-gateway proxy extension

Remote workspaces reach the VPS-resident gateway at
`http://host.docker.internal:1989`: the gateway binds the VPS's docker bridge
address (never a public interface), and the workspace container resolves that
name to it because the VPS provider creates every container with the matching
`--add-host` mapping. It is the same fixed port local workspaces use on their
own loopback. Provisioning also loads an nftables policy on the VPS (table
`inet mngr_bridge_services`, boot-persistent through a systemd oneshot) that
drops traffic to the gateway's port -- and the owner-exec daemon's, which binds
the same address -- unless it arrives on the docker bridge interface or on
loopback; without it Linux would deliver a packet for the bridge address that
reached the public interface to the bound socket all the same. The same policy
drops any other new connection arriving from the docker bridge, so those two
ports are all the workspace can reach on its VPS (not its sshd, for one).
Third-party requests terminate there so the VPS can inject the credentials its
own store holds. The VPS gateway loads one dedicated `desktop_gateway_proxy.mjs`
extension for the endpoint families whose state remains on the user's
computers: `/permissions`, `/permission-requests`, and `/minds-api-proxy`
(including all subpaths), plus `/devices`. It forwards those requests to a
desktop gateway over that desktop's own reverse tunnel, authenticating the hop
with the desktop's own gateway password and a desktop-target permissions JWT
it minted for this host -- both of which *replace* whatever the caller sent,
since the caller's password authenticates it to the VPS gateway and its
override would let it choose the policy the desktop evaluates it against.
Native VPS requests carry no override and are authorized by the machine's own
`~/.latchkey/permissions.json` (seeded at provisioning, then rewritten by the
full permission snapshot the desktop pushes on every edit). Which desktop a
request goes to is described under [Desktops](#desktops).

The workspace therefore always has one gateway URL and one agent-side skill.
If the user's computer is offline, third-party calls through the VPS gateway
continue to work, while desktop-owned extension routes fail with a clear HTTP
503 (no desktop connected) or 502 (a desktop that stopped answering) response.
That includes calls carrying an *expiring* credential -- an OAuth connection or
Zoom: the store the VPS gateway runs on is the machine's own, so it renews the
tokens in it itself rather than waiting for the desktop to (see
[Machine stores](#machine-stores)).

### Desktops

Every one of the user's computers running `mngr latchkey forward` is a
*desktop* to the machines it connects to, and each keeps a tunnel of its own to
each of them, so two desktops running at once never contend for one. On every
discovery cycle the forward supervisor asks the machine's sshd for a loopback
port of this desktop's own (`setup_reverse_tunnel` with a dynamic remote port),
and announces itself with `mngr-latchkey announce-device`: one record per
desktop under the RAM-backed `/run/mngr-latchkey/devices/`, named
`<device id>.json` and carrying

```json
{
  "device_id": "host-3f9c...",
  "hostname": "laptop.local",
  "port": 41988,
  "gateway_password": "<this desktop gateway's listen password>",
  "permissions_override": "<a JWT naming this host's permissions file on that desktop>"
}
```

(`imbue.mngr_latchkey.devices.DeviceRecord` is the one owner of that shape).
The record's modification time says when the desktop was last heard from, and
the extension reports it rather than judging it: `/devices` gives each
desktop's `last_seen_at` beside the interval a connected desktop refreshes its
record at (`DEVICE_ANNOUNCEMENT_INTERVAL_SECONDS`, the discovery cycle), so a
caller can read a record's age for itself, and a request goes to the desktop
it names, or to the most recently announced one, whatever that record's age.
What retires a desktop that went to sleep, offline or quit is its tunnel, not
its record: the package ships an sshd drop-in (`ClientAliveInterval 30`,
`ClientAliveCountMax 3`) so the machine's sshd drops a session that stops
answering probes within 90 seconds, and the forwarded port with it, after
which a request for that desktop is refused at once (a 502 naming the desktop)
instead of hanging on a channel that never opens. Since every connected
desktop keeps announcing, the most recently announced one is a live one
whenever any desktop is connected. The extension reads the directory on every
request, so a desktop arriving or leaving takes effect without a gateway
restart, and a repaired tunnel's new port reaches the machine with the next
announcement. The records live in RAM beside the machine's own secrets because
each carries that desktop's; a reboot wipes them together, and the next
announcement from a connected desktop recreates its record. The device id is
the one the desktop app identifies this install by; a standalone forward uses
the mngr local host id, which the earliest installs of the desktop app adopted
as their device id.

The extension exposes the desktops it knows and lets a caller pick among them:

* `GET /devices` answers `{"devices": [{"device_id", "hostname",
  "last_seen_at"}, ...], "announcement_interval_seconds": 30}`, most recently
  seen first. It is granted to every agent by the baseline
  (`latchkey-self-read-devices`), so a workspace can learn which desktops
  exist before it addresses one.
* An `X-Latchkey-Device` header on a `/permissions`, `/permission-requests`
  or `/minds-api-proxy` request names where it goes:
  * absent: the most recently announced desktop, which is what every
    workspace built before the header did (503 when none has ever announced
    itself);
  * one device id: that desktop (503 for one the gateway does not know);
  * `*`: every desktop the gateway knows, or a comma-separated list of device
    ids: each named desktop the gateway knows (the others are ignored). When
    that comes down to no desktop, the answer is 503, as for a request without
    the header when no desktop has announced itself. When it comes down to
    exactly one desktop, the request is forwarded to it and
    its response relayed as is, streaming included. Otherwise the request goes
    to every target (the body read whole first, since it is sent once per
    desktop) and the answer is `200` with the responses side by side:
    `{"responses": [{"device_id", "hostname", "status", "content_type",
    "body"}, ...]}`, where a desktop
    that could not be reached contributes an entry carrying an `error` (and
    the status it would have answered alone) instead of a body. Such an
    aggregated answer, and only it, carries the response header
    `X-Latchkey-Multiple-Desktops-Matched: true`, so a caller can tell it from
    a single desktop's response without inspecting the body. A streaming
    request (`?follow=true`) is only ever forwarded to a single desktop.

The desktop gateway does not load this extension: it serves the desktop-owned
routes itself and ignores the header, and a small `device_list.mjs` extension
answers `/devices` there with the desktop it runs on as the one device
(`LATCHKEY_EXTENSION_LOCAL_DEVICE_ID` and `..._HOSTNAME`, set by the forward
supervisor from its `--device-id` and the hostname). Since a machine that
resolves the header to one desktop answers with that desktop's own response,
a workspace asks about desktops and addresses them the same way whether its
gateway runs on a machine or on the desktop.

Desktop egress reads the same records: the curl router sends a routed request
to a desktop over that desktop's tunnel and with the pair its record carries,
exactly as the extension forwards a request. It never takes the header: which
desktop it picks is said by the service's route in
[the rules file](#the-rules-file), whose hops name desktops by these device
ids.

### Permission requests across desktops

A permission request outlives the desktops it was sent to: the user answers it
on whichever desktop they are at, and a desktop that was offline when it was
filed shows it the next time it connects. `POST /permission-requests` is
therefore the one forwarded request the machine takes part in:

* The machine assigns the request its id before forwarding and puts it in the
  body as `request_id` (a body that already carries one is a 400), so every
  desktop the header names files the same request under the same id. Agents
  send `X-Latchkey-Device: *` to reach every desktop at once.
* The machine keeps a record of what the agent sent under
  `~/.latchkey/filed_permission_requests/v1/<request_id>.json`
  (`imbue.mngr_latchkey.filed_permission_requests.FiledPermissionRequest`):
  the agent's body as sent, the desktops it was for (`"*"` or the device ids
  the header named, known to the machine or not) and when it was filed. The
  machine validates nothing: the record is kept when some desktop accepted the
  request (a 2xx), or when no desktop could be reached to judge it, and
  dropped when every desktop that answered refused it.
* The answer to the agent is the one every forwarded request gets for the
  header's shape, except that a request that reached no desktop at all is a
  503 saying that the request was kept on the machine for the desktops to pick
  up when they next connect. (One that reached only an unreachable desktop is
  kept too, and answered with that desktop's 502 as any request would be.)
* Every answer the machine composes rather than relays -- the responses side
  by side, that 503, that 502 -- opens with the `request_id` the machine
  assigned, the `request_type` (the body's `type`, under the name the desktops
  file it as), the `rationale` and the `payload` (each `null` when the body
  lacks it), ahead of its other fields, so a parser of the agent's output
  learns what was filed and under which id whatever the desktops said. The one
  desktop's own response is relayed untouched: accepted, it carries all of that
  itself.
* A desktop that answers the request runs `mngr-latchkey forget-request` on
  the machine, which drops the record and sends `DELETE
  /permission-requests/<request_id>` with `X-Latchkey-Device: *` through the
  machine's own gateway, so every connected desktop drops its copy too (its
  follow stream carries the deletion to its UI). The hop to each desktop
  presents the pair the desktop announced, whose JWT names the host's own
  permissions file, so that file's baseline grants the DELETE. The same
  DELETE, forwarded from an agent, also drops the machine's record.
* A desktop syncs against the records when a machine first appears to it and
  periodically afterwards (`mngr-latchkey list-requests`): a record for this
  desktop it does not hold is filed on its own gateway under the record's id,
  with the host's permissions file as the target, and the gateway judges the
  body exactly as it would have live; a request it holds for the host that the
  machine no longer keeps was answered on another desktop and is dropped; a
  body its gateway refuses is one no desktop will ever take, so the machine is
  asked to forget it. (Imbue Studio's
  `imbue.minds.desktop_client.latchkey.machine_request_sync` does this.)

A file-sharing request names one desktop by nature (the path is on that
desktop's disk), so it is addressed to one desktop rather than broadcast; the
record then names that desktop, and only it files the request when it syncs.

### `permissions` extension

Reads and edits a detent permissions file at a caller-supplied path.
The gateway is launched with the environment variable
`LATCHKEY_EXTENSION_PERMISSIONS_ROOT` pointing at this package's data
directory; any `path` query parameter that resolves outside that
root is rejected with HTTP 403.

* `GET /permissions?path=<file>` returns the full permissions file.
* `GET /permissions/available` returns the full permission catalog as
  a JSON object keyed by raw service name. Each value is an array of
  scope entries (a single service may expose more than one scope), each
  with the shape `{"scope": "<schema_name>", "display_name": "...",
  "description": "...", "permissions": [{"name": "<schema_name>",
  "description": "..."}, ...]}`. The scope-level `description` and each
  permission's `description` carry detent's per-schema `$comment`
  summaries (both optional).
* `GET /permissions/available/<service_name>` returns the permission
  catalog entries for `<service_name>` (e.g. `slack`, `google-gmail`)
  as an array, using the same value shape, or 404 if the service is
  unknown. The catch-all `any` permission is always injected at index 0
  of every scope's `permissions` array, so a caller can always
  request unrestricted access under a known scope. This endpoint
  is backed by a `services.json` file (keyed by raw service name)
  that ships alongside the extension; the path query parameter
  is not consulted.
* `GET /permissions/rules?path=<file>&rule_key=<scope>` returns the
  rule for `<scope>`, or 404 if absent.
* `POST /permissions/rules?path=<file>&rule_key=<key>` with the body
  `{"permissions": ["slack-read-all", ...], "schemas": {"<name>": {...}}}`
  adds or replaces the rule for `<key>`. `schemas` is optional and is
  merged by name into the file's `schemas` object; everything else in
  the file is preserved verbatim. The target file (and any missing
  parent directories, e.g. `hosts/<host_id>/`) is created if it does
  not yet exist.

  The extension never synthesizes schemas and never interprets
  `<key>`, so a caller whose key is not a name detent already knows (a
  built-in schema, or one already defined in the file) **must** define
  it here. That is how per-account grants are written: their key names a
  generated schema composed by
  `imbue.mngr_latchkey.account_scopes.build_account_grant`, which owns
  that shape (see [Per-account grants](#per-account-grants)).
* `DELETE /permissions/rules?path=<file>&rule_key=<scope>` removes
  the named rule.

The `services.json` catalog is generated from detent's built-in request
schemas; do not edit it by hand. Regenerate it against a detent checkout
with:

```sh
uv run python libs/mngr_latchkey/scripts/generate_services_json.py \
  --detent-root /path/to/detent
```

Display names and the service ordering are editorial metadata detent does
not carry; they live as curated constants in that script.

Services hidden from agents (`core.HIDDEN_BUILTIN_SERVICES`, currently just
`notion`) are left out of the catalog: latchkey never injects their
credentials, so an entry for one would only offer grants that can never be
used. The generator skips them, so the catalog and the gateway's
`settings.hideBuiltinServices` cannot drift apart.

`services.json` also carries Imbue Studio's own *additional* (custom) services --
ones detent has no schemas for, currently `claude.ai`. Their definitions are
hand-maintained in `imbue/mngr_latchkey/additional_services.json` (a
`display_name`, a `registration`, the single Detent `scope` it exposes with an
inline scope `schema`, and its grantable `permissions`, each with an inline
`schema`), and the generator *folds their catalog entries into* `services.json`.
That way every reader of the catalog -- `ServicesCatalog` and both gateway
extensions -- works from one file in one shape and never has to know which of
the two sources a service came from.

`registration` is written in **latchkey's own shape**: it is the object that
lands verbatim under `registeredServices.<name>` in latchkey's `config.json`,
so a service is described here exactly as `latchkey services register` would
persist it. Nothing on the Python side models or validates its contents --
latchkey owns that schema and checks it when it loads the config -- so adding
a service, or picking up a field a later latchkey adds, is a data-only change.

For `claude-ai` that is a `baseApiUrl` plus a `loginUrl` and a `loginFlow`,
which give it a `latchkey auth browser` sign-in (latchkey ships these generic
flows so a service outside its builtin catalog can still be signed into).
`claude-ai` uses `cookie-capture`, which finishes once the named cookies have
been set and stores them as a `Cookie` header -- for claude.ai, the single
`sessionKey` cookie, scoped by `cookieUrl` to claude.ai itself because sign-in
may start on another host. A service registered with only a `baseApiUrl` can
be authenticated by hand with `latchkey auth set` instead.

Because a custom scope is not a detent builtin, its schemas have to reach the
gateway's permission check. They are **inlined into every permissions file
Imbue Studio writes**: the agent baseline (`baseline_permissions.ADDITIONAL_SERVICE_SCHEMAS`)
carries them, and `agent_setup.reconcile_baseline_permissions` refreshes them on
files that already exist, so the bundled definition always wins over a stale
copy. Granting a custom scope is then a plain rule write against a file that
already defines the scope.

Inlining rather than sharing one file via detent's `include` is deliberate.
Detent resolves an `include` relative to the directory of the file that
references it, and a host's permissions file is reachable through several
directories -- its canonical `hosts/<host_id>/` path, the opaque handle in
`permissions/` that a desktop workspace's JWT names, and
`~/.latchkey/permissions.json` on a VPS. A shared file would have to be copied
next to each of them, and an include that fails to resolve fails the *whole*
permission check for that host, not just the rule that needed it. A
self-contained file has nothing to resolve.

`imbue.mngr_latchkey.additional_services` is the single Python chokepoint for
the file. It exposes the registration entries, the merged detent schemas the
baseline inlines, and the catalog projection the generator folds into
`services.json`. No gateway extension reads it -- they only read
`services.json`.

The registration entries are Imbue Studio's half of latchkey's own `config.json`:
`core.merge_minds_latchkey_config` read-merges them into the file's
`registeredServices` block (alongside `settings.hideBuiltinServices`) rather
than shelling out to `latchkey services register`, which cannot update a
registration that already exists. That merge runs for **every** gateway that
serves Imbue Studio agents -- the desktop one at `initialize()` and each gateway spawn,
and a VPS one during remote provisioning. It has to: the registration is what
lets a gateway resolve a request to a custom service at all, so a VPS holding
the synchronized credentials but not the registration would silently never
inject them.

A typical end-to-end shell flow:

```sh
# Stream pending requests as they come in.
curl -N "${auth[@]}" "$GATEWAY_URL/permission-requests?follow=true"

# Grant the agent slack-read-all for one Slack account on its host's
# permissions file. Grants are per account, so the rule names a generated
# schema that gates the built-in slack-api scope on that account -- and the
# caller, not the gateway, defines it.
HOST_PERMS=$MNGR_LATCHKEY_DIRECTORY/mngr_latchkey/hosts/$HOST_ID/latchkey_permissions.json
RULE_KEY='slack-api:hynek@imbue-ai'
curl -X POST "${auth[@]}" -H "Content-Type: application/json" \
  -d '{"permissions": ["slack-read-all"],
       "schemas": {"slack-api:hynek@imbue-ai": {"allOf": [
         {"$ref": "#/$defs/slack-api"},
         {"properties": {"customMetadata": {"type": "object",
            "properties": {"account": {"const": "hynek@imbue-ai"}},
            "required": ["account"]}},
          "required": ["customMetadata"]}]}}}' \
  "$GATEWAY_URL/permissions/rules?path=$HOST_PERMS&rule_key=$RULE_KEY"

# Clear the pending request now that it has been resolved.
curl -X DELETE "${auth[@]}" "$GATEWAY_URL/permission-requests/$REQUEST_ID"
```

## Embedding

Embedders (such as the Imbue Studio desktop client) typically want a single
detached ``mngr latchkey forward`` supervisor that survives embedder
restarts and adopts the existing one instead of double-spawning. The
:class:`LatchkeyForwardSupervisor` does exactly that:

```python
from imbue.mngr_latchkey.forward_supervisor import LatchkeyForwardSupervisor

supervisor = LatchkeyForwardSupervisor(
    mngr_binary="/path/to/mngr",          # default: ``mngr`` on PATH
    latchkey_binary="/path/to/latchkey",  # default: ``latchkey`` on PATH
    latchkey_directory=root_dir,
)
supervisor.ensure_running()  # idempotent; spawns or adopts as needed
# ... do whatever the embedder does ...
# Optional: ``supervisor.stop()`` to terminate the detached process and
# tear down the gateway. Omitting this leaves the supervisor running
# detached, which is what Imbue Studio does so the gateway survives a
# desktop-client restart.
```

## Python API

Every CLI subcommand is a thin wrapper around the library; the library
remains importable for embedders such as the Imbue Studio desktop client.

```python
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.agent_setup import (
    LatchkeyGatewayLocation,
    prepare_agent_latchkey,
    finalize_host_permissions,
)
from imbue.mngr_latchkey.discovery import (
    LatchkeyDiscoveryHandler,
    LatchkeyDestructionHandler,
)
from imbue.mngr_forward.ssh_tunnel import SSHTunnelManager

latchkey = Latchkey(
    latchkey_binary="/path/to/latchkey",  # default: "latchkey" on PATH
    latchkey_directory=root_dir,
)
latchkey.initialize()

# (a) Pre-create env vars + opaque permissions handle for a new host.
setup = prepare_agent_latchkey(
    latchkey,
    is_tunneled=True,
    gateway_location=LatchkeyGatewayLocation.VPS,
)
# setup.env: LATCHKEY_GATEWAY[_PASSWORD,_DISABLE_COUNTING]
# Desktop-gateway setups also include LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE.
# LATCHKEY_GATEWAY is a constant per location: http://127.0.0.1:1989 for a
# desktop gateway (reverse-tunneled in), http://host.docker.internal:1989 for
# a VPS gateway (reached over the container's docker bridge).
# Discovery realizes the desktop or VPS location selected before creation.
# setup.opaque_permissions_path: pass to finalize_host_permissions later

# ... mngr create returns the canonical host id ...

# (b) Point the opaque handle at the canonical host permissions path.
finalize_host_permissions(latchkey, setup.opaque_permissions_path, host_id)
# Raises LatchkeyStoreError on failure -- callers decide whether to abort
# or just surface a warning.

# (c) Plug the discovery and destruction handlers into your agent
# discovery stream so reverse tunnels are opened on discovery and
# closed on destruction.
tunnel_manager = SSHTunnelManager()
tunnel_manager.start_reverse_tunnel_health_check()
on_discovered = LatchkeyDiscoveryHandler(
    latchkey=latchkey, tunnel_manager=tunnel_manager, concurrency_group=cg
)
on_destroyed = LatchkeyDestructionHandler(tunnel_manager=tunnel_manager)
```

The `latchkey_directory` is used both as the upstream `LATCHKEY_DIRECTORY`
for spawned `latchkey` subprocesses and as the root of this package's own
metadata subdirectory (`<latchkey_directory>/mngr_latchkey/`, accessible
via `Latchkey.plugin_data_dir`).

### Storing user-supplied credentials

Services with no browser sign-in report an example of the command that
stores their credentials, each value the caller must supply written as an
angle-bracketed placeholder (`LatchkeyServiceInfo.set_credentials_example`,
e.g. `latchkey auth set-nocurl aws <access-key-id> <secret-access-key>`).
`imbue.mngr_latchkey.credential_commands` turns such an example into a
fillable form and back into a runnable command, so an embedder can collect
the values in its own UI instead of sending the user to a terminal:

```python
from imbue.mngr_latchkey.credential_commands import (
    build_credential_command_argv,
    parse_credential_command_example,
)

# Raises CredentialCommandError when the example is not a latchkey command,
# or has no placeholders (nothing to ask the user for).
parsed = parse_credential_command_example(service_info.set_credentials_example)
# parsed.parameters: one (name, label) pair per placeholder, to render as inputs

argv = build_credential_command_argv(
    parsed,
    {"access-key-id": "...", "secret-access-key": "..."},
    account,  # "" for latchkey's unnamed default account
)
is_success, detail = latchkey.auth_set_credentials("aws", argv)
```

The argv carries the user's secrets, so it is passed to the subprocess as a
list (never a shell string) and is never logged. `--account` is a *global*
latchkey option and is therefore placed before the subcommand rather than
after the example's own arguments. `auth set` stores whatever it is handed,
so callers should re-read `services_info` afterwards to find out whether
the credentials are actually usable.
