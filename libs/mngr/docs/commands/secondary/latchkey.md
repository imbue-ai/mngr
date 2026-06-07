<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr latchkey

**Synopsis:**

```text
mngr latchkey <subcommand> [OPTIONS]
```

Latchkey gateway lifecycle and per-agent setup [experimental].

Wires the shared Latchkey gateway and per-agent permissions
without requiring the minds desktop app. Run ``mngr latchkey forward``
once at startup, then call ``mngr latchkey create-agent-env`` /
``mngr latchkey link-permissions`` per host.

Settings:

- ``[plugins.latchkey].directory`` (default ``~/.mngr/latchkey``)
- ``[plugins.latchkey].latchkey_binary`` (default ``latchkey`` on PATH)

Both are overridable via the matching ``MNGR_LATCHKEY_*`` env vars and
per-invocation ``--latchkey-directory`` / ``--latchkey-binary`` flags.

**Usage:**

```text
mngr latchkey [OPTIONS] COMMAND [ARGS]...
```
**Options:**


## mngr latchkey create-agent-env

Emit LATCHKEY_* env vars (+ opaque permissions handle) for a new agent.

Wraps :func:`imbue.mngr_latchkey.agent_setup.prepare_agent_latchkey`
in tunneled mode and emits its result as a single JSON object on stdout:

```
{
  "env": {
    "LATCHKEY_GATEWAY": "...",
    "LATCHKEY_GATEWAY_SECONDARY": "...",
    "LATCHKEY_GATEWAY_PASSWORD": "...",
    "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE": "...",
    "LATCHKEY_DISABLE_COUNTING": "1"
  },
  "opaque_permissions_path": "..."
}
```

Pipe the ``env`` values into ``mngr create --host-env KEY=VALUE``
so every agent on the host inherits the same gateway wiring, then
call ``mngr latchkey link-permissions`` with the
``opaque_permissions_path`` and the canonical host id once
``mngr create`` returns it. The gateway URL is always the constant
agent-side loopback URL (``http://127.0.0.1:1989``); there is no
on-host (DEV) mode -- a running ``mngr latchkey forward`` is
expected to bridge the agent's loopback port back to the shared
gateway on the desktop.

**Usage:**

```text
mngr latchkey create-agent-env [OPTIONS]
```
**Options:**

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mngr/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNGR_HEADLESS env var or 'headless' config key. | `False` |
| `--safe` | boolean | Always query all providers during discovery (disable event-stream optimization). Use this when interfacing with mngr from multiple machines. | `False` |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-S`, `--setting` | text | Override a config setting for this invocation (KEY=VALUE, dot-separated paths; append __extend to the leaf key to extend list/dict/set fields) [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--latchkey-binary` | text | Path to the upstream ``latchkey`` CLI. Falls back to $MNGR_LATCHKEY_BINARY, then ``[plugins.latchkey].latchkey_binary`` in settings.toml, then 'latchkey' on PATH. | None |
| `--latchkey-directory` | path | Root directory for ``LATCHKEY_DIRECTORY`` and the plugin's ``mngr_latchkey/`` metadata subtree. Falls back to $MNGR_LATCHKEY_DIRECTORY, then ``[plugins.latchkey].directory`` in settings.toml, then '~/.mngr/latchkey'. | None |


## Examples

**Wire env vars into mngr create**

```bash
$ eval "$(mngr latchkey create-agent-env | jq -r '.env | to_entries[] | "--host-env \(.key)=\(.value)"')"
```

## mngr latchkey link-permissions

Link an opaque permissions handle to a canonical host ID.

Wraps :func:`imbue.mngr_latchkey.agent_setup.finalize_host_permissions`.
Idempotent: re-running for the same host preserves prior grants and
discards the freshly-created baseline.

**Usage:**

```text
mngr latchkey link-permissions [OPTIONS]
```
**Options:**

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mngr/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNGR_HEADLESS env var or 'headless' config key. | `False` |
| `--safe` | boolean | Always query all providers during discovery (disable event-stream optimization). Use this when interfacing with mngr from multiple machines. | `False` |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-S`, `--setting` | text | Override a config setting for this invocation (KEY=VALUE, dot-separated paths; append __extend to the leaf key to extend list/dict/set fields) [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--host-id` | text | Canonical host ID returned by ``mngr create``. | None |
| `--opaque-path` | path | Opaque permissions handle emitted by ``mngr latchkey create-agent-env``. | None |
| `--latchkey-binary` | text | Path to the upstream ``latchkey`` CLI. Falls back to $MNGR_LATCHKEY_BINARY, then ``[plugins.latchkey].latchkey_binary`` in settings.toml, then 'latchkey' on PATH. | None |
| `--latchkey-directory` | path | Root directory for ``LATCHKEY_DIRECTORY`` and the plugin's ``mngr_latchkey/`` metadata subtree. Falls back to $MNGR_LATCHKEY_DIRECTORY, then ``[plugins.latchkey].directory`` in settings.toml, then '~/.mngr/latchkey'. | None |


## Examples

**Finalize permissions for a freshly-created host**

```bash
$ mngr latchkey link-permissions --host-id $HOST_ID --opaque-path /path/from/create-agent-env.json
```

## mngr latchkey register-agent

Register an agent on a host, granting it access to the Minds API proxy.

Wraps :func:`imbue.mngr_latchkey.agent_setup.register_agent_for_host`.
The per-host ``latchkey_permissions.json`` ships with an empty
allowed-agent enum on the first rule (the one that gates
``/minds-api-proxy/api/v1/agents/<id>/...``); this command appends
the supplied agent ID to that enum so the gateway will let that
agent through to its own ``/api/v1/agents/<id>/...`` subtree.
Idempotent: re-running for an already-registered agent is a no-op.

**Usage:**

```text
mngr latchkey register-agent [OPTIONS]
```
**Options:**

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mngr/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNGR_HEADLESS env var or 'headless' config key. | `False` |
| `--safe` | boolean | Always query all providers during discovery (disable event-stream optimization). Use this when interfacing with mngr from multiple machines. | `False` |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-S`, `--setting` | text | Override a config setting for this invocation (KEY=VALUE, dot-separated paths; append __extend to the leaf key to extend list/dict/set fields) [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--host-id` | text | Canonical host ID the agent runs on. Identifies which per-host permissions file to edit. | None |
| `--agent-id` | text | Canonical agent ID to add to the host's allowed-agent enum. | None |
| `--latchkey-binary` | text | Path to the upstream ``latchkey`` CLI. Falls back to $MNGR_LATCHKEY_BINARY, then ``[plugins.latchkey].latchkey_binary`` in settings.toml, then 'latchkey' on PATH. | None |
| `--latchkey-directory` | path | Root directory for ``LATCHKEY_DIRECTORY`` and the plugin's ``mngr_latchkey/`` metadata subtree. Falls back to $MNGR_LATCHKEY_DIRECTORY, then ``[plugins.latchkey].directory`` in settings.toml, then '~/.mngr/latchkey'. | None |


## Examples

**Register an agent for the Minds API proxy**

```bash
$ mngr latchkey register-agent --host-id $HOST_ID --agent-id $AGENT_ID
```

## mngr latchkey forward

Run the shared Latchkey gateway and reverse-tunnel it into every discovered agent.

Long-running foreground process that:

1. Initializes the configured ``Latchkey`` (version-checks the binary,
   adopts or discards any pre-existing detached gateway record).
2. Eagerly spawns the shared ``latchkey gateway`` subprocess.
3. Spawns ``mngr observe --discovery-only --quiet`` and, for every
   agent discovered, opens a reverse SSH tunnel that bridges the
   agent's ``127.0.0.1:AGENT_SIDE_LATCHKEY_PORT`` to the host-side
   gateway port. Agents discovered without SSH info are left to
   reach the gateway via whatever direct route exists.
4. On agent destruction, drops that agent's reverse tunnel.
5. On SIGINT/SIGTERM, terminates the observe subprocess, all reverse
   tunnels, *and* the shared gateway. The coupled-lifetime semantics
   are intentional: any agents still alive when this process exits
   will lose their gateway endpoint until the next ``mngr latchkey
   forward`` is started.
6. On SIGHUP, bounces only the ``mngr observe`` child (the gateway and
   every reverse tunnel stay up) so a provider-set change made by an
   embedder takes effect without a full restart.

No filtering flags: every discovered agent gets a tunnel. The plugin
emits stderr-only logs; stdout stays empty for the lifetime of the
process.

**Usage:**

```text
mngr latchkey forward [OPTIONS]
```
**Options:**

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mngr/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNGR_HEADLESS env var or 'headless' config key. | `False` |
| `--safe` | boolean | Always query all providers during discovery (disable event-stream optimization). Use this when interfacing with mngr from multiple machines. | `False` |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-S`, `--setting` | text | Override a config setting for this invocation (KEY=VALUE, dot-separated paths; append __extend to the leaf key to extend list/dict/set fields) [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--mngr-binary` | text | Path to the mngr binary used to spawn the underlying ``mngr observe`` subprocess. | `mngr` |
| `--latchkey-binary` | text | Path to the upstream ``latchkey`` CLI. Falls back to $MNGR_LATCHKEY_BINARY, then ``[plugins.latchkey].latchkey_binary`` in settings.toml, then 'latchkey' on PATH. | None |
| `--latchkey-directory` | path | Root directory for ``LATCHKEY_DIRECTORY`` and the plugin's ``mngr_latchkey/`` metadata subtree. Falls back to $MNGR_LATCHKEY_DIRECTORY, then ``[plugins.latchkey].directory`` in settings.toml, then '~/.mngr/latchkey'. | None |


## Examples

**Run with defaults**

```bash
$ mngr latchkey forward
```

**Use a bundled latchkey binary**

```bash
$ mngr latchkey forward --latchkey-binary /opt/latchkey/bin/latchkey
```

## mngr latchkey admin-jwt

Mint a wildcard ``permissions-override`` JWT for the shared gateway.

Materializes the admin permissions file at
``<plugin_data_dir>/latchkey_admin_permissions.json`` (idempotent --
an existing file is reused as-is) and mints a JWT signed for that
path via ``latchkey gateway create-jwt --no-validate``. The JWT is
printed on stdout as a single line.

The returned token unlocks every service and every extension
endpoint reachable through the gateway, so treat it like a root
credential and pass it as the
``X-Latchkey-Gateway-Permissions-Override`` header to gateway
requests that need wildcard access (e.g. the minds desktop client
streaming pending permission requests from the
``permission-requests`` extension).

**Usage:**

```text
mngr latchkey admin-jwt [OPTIONS]
```
**Options:**

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mngr/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNGR_HEADLESS env var or 'headless' config key. | `False` |
| `--safe` | boolean | Always query all providers during discovery (disable event-stream optimization). Use this when interfacing with mngr from multiple machines. | `False` |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-S`, `--setting` | text | Override a config setting for this invocation (KEY=VALUE, dot-separated paths; append __extend to the leaf key to extend list/dict/set fields) [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--latchkey-binary` | text | Path to the upstream ``latchkey`` CLI. Falls back to $MNGR_LATCHKEY_BINARY, then ``[plugins.latchkey].latchkey_binary`` in settings.toml, then 'latchkey' on PATH. | None |
| `--latchkey-directory` | path | Root directory for ``LATCHKEY_DIRECTORY`` and the plugin's ``mngr_latchkey/`` metadata subtree. Falls back to $MNGR_LATCHKEY_DIRECTORY, then ``[plugins.latchkey].directory`` in settings.toml, then '~/.mngr/latchkey'. | None |


## Examples

**Capture into a shell variable**

```bash
$ ADMIN_JWT=$(mngr latchkey admin-jwt)
```

## mngr latchkey gateway-info

Print the running shared gateway's URL + password as JSON.

Reads the supervised ``mngr latchkey forward`` record
to discover the bound gateway port and derives the gateway password
locally (via ``latchkey gateway create-jwt`` against a sentinel path,
the same way :meth:`Latchkey.derive_gateway_password` does in
Python). Emits a single JSON object on stdout:

```
{
  "url": "http://127.0.0.1:32867",
  "password": "<sha256-of-derived-jwt>"
}
```

Fails with a non-zero exit when no supervisor is running for the
active latchkey directory, or when the supervisor has not yet
stamped its bound gateway port onto its on-disk record (i.e. the
supervisor is still in its startup window).

The password is intentionally NOT persisted on disk; this command
is the supported way to retrieve it without writing your own
Python integration.

**Usage:**

```text
mngr latchkey gateway-info [OPTIONS]
```
**Options:**

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mngr/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNGR_HEADLESS env var or 'headless' config key. | `False` |
| `--safe` | boolean | Always query all providers during discovery (disable event-stream optimization). Use this when interfacing with mngr from multiple machines. | `False` |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-S`, `--setting` | text | Override a config setting for this invocation (KEY=VALUE, dot-separated paths; append __extend to the leaf key to extend list/dict/set fields) [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--latchkey-binary` | text | Path to the upstream ``latchkey`` CLI. Falls back to $MNGR_LATCHKEY_BINARY, then ``[plugins.latchkey].latchkey_binary`` in settings.toml, then 'latchkey' on PATH. | None |
| `--latchkey-directory` | path | Root directory for ``LATCHKEY_DIRECTORY`` and the plugin's ``mngr_latchkey/`` metadata subtree. Falls back to $MNGR_LATCHKEY_DIRECTORY, then ``[plugins.latchkey].directory`` in settings.toml, then '~/.mngr/latchkey'. | None |


## Examples

**Capture into shell variables**

```bash
$ eval "$(mngr latchkey gateway-info | jq -r '@text "GATEWAY_URL=\(.url); GATEWAY_PASSWORD=\(.password)"')"
```

## Examples

**Inspect available subcommands**

```bash
$ mngr latchkey --help
```

**Start the supervisor**

```bash
$ mngr latchkey forward
```
