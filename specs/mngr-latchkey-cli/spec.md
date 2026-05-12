# `mngr latchkey` CLI plugin

## What

The `libs/mngr_latchkey/` package today is a plain Python library: minds
imports its classes directly and there is no `mngr` CLI surface. This
spec adds one. After this change, `imbue-mngr-latchkey` ships an
entry-point that registers a `mngr latchkey` `click.Group` with three
subcommands and a TOML config block (`[plugins.latchkey]`). That is
enough for a user to wire latchkey to agents end-to-end from the shell,
without running the minds app at all:

1. Run `mngr latchkey forward` in one terminal. It supervises the
   shared `latchkey gateway` subprocess and opens / tears down the
   per-agent reverse SSH tunnel for every agent discovered via
   `mngr observe`.
2. For each new agent: shell out to `mngr latchkey create-agent-env`
   to get the four `LATCHKEY_*` env vars (and the opaque permissions
   handle path) as JSON; feed those env vars into `mngr create --env
   KEY=VALUE ...`; then call `mngr latchkey link-permissions
   --agent-id <id> --opaque-path <path>` once the canonical agent id
   is known.

Gateway lifecycle is entirely internal to `mngr latchkey forward`:
there are intentionally no `ensure-gateway` / `stop-gateway` /
`gateway-info` subcommands. `forward` starts the gateway via the
existing `LatchkeyDiscoveryHandler` (which already calls
`Latchkey.ensure_gateway_started()` on every discovery) and stops it
via `Latchkey.stop_gateway()` on SIGINT/SIGTERM — coupled lifetime.
`create-agent-env` and `link-permissions` are one-shots that mint
state on disk and rely on a concurrently-running `forward` to actually
make agent traffic reach the gateway.

Auth (`latchkey auth ...`) and permissions editing
(`set_permissions_for_scope`) stay out of scope: users run upstream
`latchkey` directly for credentials, and permissions editing requires
UI choices we do not have outside minds.

## How

We add three files alongside the existing modules in
`libs/mngr_latchkey/imbue/mngr_latchkey/`:

- `plugin.py` — entry-point module. Calls
  `register_plugin_config("latchkey", LatchkeyPluginConfig)` at import
  time and implements the `register_cli_commands` pluggy hook to
  return `[latchkey_group]`. Mirrors `libs/mngr_forward/imbue/
  mngr_forward/plugin.py` line-for-line.
- `config.py` — defines `LatchkeyPluginConfig(PluginConfig)` with
  `directory: Path` (default `~/.mngr/latchkey/`) and `latchkey_binary:
  str` (default `"latchkey"`), plus a `merge_with` override that
  matches the shape used by `ForwardPluginConfig`.
- `cli.py` — the three click commands and their shared helpers. Single
  file is fine; `forward` is the heaviest but still manageable.

The entry point is wired in `pyproject.toml`:

```toml
[project.entry-points.mngr]
latchkey = "imbue.mngr_latchkey.plugin"
```

We also bump the package's `requires-python` if needed (it is already
`>=3.12`).

### Building a `Latchkey` from the CLI

All three subcommands need a configured `Latchkey` instance. We add a
single helper in `cli.py`, used by every subcommand:

```python
def _build_latchkey(opts: _CommonLatchkeyOpts) -> Latchkey:
    directory = opts.directory  # CLI > env > settings > default
    binary = opts.latchkey_binary  # CLI > env > settings > default
    latchkey = Latchkey(latchkey_binary=binary, latchkey_directory=directory)
    latchkey.initialize()
    return latchkey
```

`opts.directory` and `opts.latchkey_binary` are resolved via the
standard `mngr` precedence chain: CLI flag (`--latchkey-directory` /
`--latchkey-binary`) > env var (`MNGR_LATCHKEY_DIRECTORY` /
`MNGR_LATCHKEY_BINARY`) > `[plugins.latchkey]` in `settings.toml` >
hard-coded defaults. This is the same precedence `mngr_forward` uses
for its port etc., implemented by reading the plugin config off the
`MngrContext` returned by `setup_command_context`.

`Latchkey.initialize()` is allowed to raise (`LatchkeyBinaryNotFoundError`,
`LatchkeyVersionError`, `LatchkeyError`) and any such raise exits the
CLI with a non-zero status. There is no `--allow-degraded` mode.

### `mngr latchkey create-agent-env`

Thin wrapper around `prepare_agent_latchkey(latchkey, is_tunneled=True)`.
No `--no-tunneled` flag: every agent created through this path is
treated as SSH-tunneled, and the env always points at the constant
agent-side loopback URL `http://127.0.0.1:AGENT_SIDE_LATCHKEY_PORT`.
The single side effect on disk is the opaque permissions handle
(`<plugin_data_dir>/permissions/<uuid>.json`) created by
`new_opaque_permissions_path` + `save_permissions`.

Prints exactly this JSON object to stdout, pretty-printed, followed by
a single trailing newline:

```json
{
  "env": {
    "LATCHKEY_GATEWAY": "http://127.0.0.1:1989",
    "LATCHKEY_GATEWAY_PASSWORD": "<sha256 hex>",
    "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE": "<jwt>",
    "LATCHKEY_DISABLE_COUNTING": "1"
  },
  "opaque_permissions_path": "/home/.../mngr_latchkey/permissions/<uuid>.json"
}
```

`opaque_permissions_path` is always a string in this command's output;
the `null` case the library tolerates (no-`Latchkey` degraded mode) is
not reachable here because we always build a real `Latchkey`.

Note: this command does *not* spawn the shared gateway. The two env
values that depend on a running gateway are derived independently —
`LATCHKEY_GATEWAY_PASSWORD` from `latchkey gateway create-jwt
--no-validate` (no gateway needed), `LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE`
ditto. So `create-agent-env` can run before `forward` is started.
Confirmed by reading `prepare_agent_latchkey` with `is_tunneled=True`:
it does *not* call `ensure_gateway_started`.

### `mngr latchkey link-permissions`

```
mngr latchkey link-permissions --agent-id <id> --opaque-path <path>
```

Both flags are required and named (not positional) because they are
both stringy values and positional order would be confusing. Calls
`finalize_agent_permissions(latchkey, opaque_path, agent_id)`. Idempotent
re-linking (re-creation of the same agent id) already works at the
library level — `link_opaque_permissions_to_agent` preserves an
existing canonical file and discards the freshly-created baseline.
On `LatchkeyStoreError` we exit non-zero.

### `mngr latchkey forward`

Long-running foreground process. Built on top of
`imbue.mngr_forward.stream_manager.ForwardStreamManager`, which the
`mngr_latchkey` package already depends on transitively via
`imbue-mngr-forward` (today only for its `relay` helper). We reuse it
to consume `mngr observe` / `mngr event` envelopes; this is the same
mechanism `mngr forward` itself uses and avoids inventing a second
discovery plumbing.

Note on discovery plumbing: the Q&A briefly mentioned an "in-process
`MngrStreamManager`" alternative. There is no shared `MngrStreamManager`
in the codebase today — `mngr_forward` has `ForwardStreamManager` and
minds has `EnvelopeStreamConsumer`. Both are subprocess-based. We
reuse `ForwardStreamManager` because the dependency is already present
and the pattern matches the most directly comparable plugin.

Wiring at startup, in order:

1. `setup_command_context` -> `MngrContext`.
2. Resolve `directory` and `latchkey_binary` from the merged config.
3. Build and initialize `Latchkey` (this version-checks the binary and
   adopts any pre-existing detached gateway record from a previous
   `forward` run).
4. Build `SSHTunnelManager` (the plugin's own, in
   `imbue.mngr_latchkey.ssh_tunnel`), start its reverse-tunnel
   health-check loop.
5. Build `LatchkeyDiscoveryHandler` and `LatchkeyDestructionHandler`.
6. Build `ForwardStreamManager` with the discovery / destruction
   callbacks registered; start it.
7. Block in a wait loop until SIGINT / SIGTERM.
8. On shutdown: stop the stream manager, call `tunnel_manager.cleanup()`,
   call `latchkey.stop_gateway()`. The shared gateway dies with
   `forward` — coupled lifetime, per the user's preference for the
   single-tenant standalone case.

No filtering flags — `--agent-include` / `--agent-exclude` /
`--event-include` / `--event-exclude` are deliberately not exposed,
because latchkey is meant to be agent-wide. No SIGHUP-bounce, no
`--no-observe`. We log progress on stderr only (loguru's default
sink); stdout stays empty, since there's no machine-readable login URL
or similar to emit and a JSONL event stream would only matter to
programmatic supervisors that we are not designing for here.

### Concurrency

`create-agent-env`, `link-permissions`, and `forward` can run
concurrently (the typical workflow is `forward` in one terminal,
periodic `create-agent-env` + `link-permissions` invocations in
another). The library is already designed for this:

- `Latchkey.ensure_gateway_started()` is idempotent across processes
  via the on-disk gateway record and `_is_info_alive` reconciliation.
- `new_opaque_permissions_path` uses unique UUID names.
- `link_opaque_permissions_to_agent` uses atomic `os.replace`.

We document this as supported, and `create-agent-env` and
`link-permissions` each call `Latchkey.initialize()` independently so
they don't depend on `forward` already running.

### Common CLI scaffolding

We use the existing helpers in `imbue.mngr.cli.common_opts` (most
importantly `add_common_options` and `setup_command_context`) so the
three subcommands inherit `--config-file`, `--host-dir`, `--verbose`,
etc. consistent with the rest of `mngr`. `CommandHelpMetadata` entries
are registered for each subcommand so `mngr help latchkey ...` works.

## Checklist

1. Add `libs/mngr_latchkey/imbue/mngr_latchkey/config.py` with
   `LatchkeyPluginConfig` (mirrors `ForwardPluginConfig`).
2. Add `libs/mngr_latchkey/imbue/mngr_latchkey/cli.py` with the
   `latchkey` `click.Group`, the three subcommands, the
   `_build_latchkey` helper, and a `_CommonLatchkeyOpts` dataclass
   that resolves precedence CLI > env > settings > default. Hard-fail
   on `LatchkeyError` / `LatchkeyStoreError` via `click.UsageError` /
   exit-1 paths as appropriate.
3. Add `libs/mngr_latchkey/imbue/mngr_latchkey/plugin.py` with the
   `register_plugin_config` call and the `register_cli_commands` hook.
4. Add `imbue.mngr_latchkey.hookimpl` to `__init__.py` if and only if
   the standard pattern (mirror `mngr_forward`'s `__init__.py`)
   requires it; otherwise leave `__init__.py` empty per the repo
   convention.
5. Update `libs/mngr_latchkey/pyproject.toml` to declare
   `[project.entry-points.mngr]` -> `latchkey =
   "imbue.mngr_latchkey.plugin"`. Add a `click-option-group` dep if
   the existing imports require it (likely yes, copying
   `mngr_forward`).
6. Update `libs/mngr_latchkey/README.md`: replace the "no `mngr` CLI
   surface yet" wording with usage examples for the three subcommands
   and the `[plugins.latchkey]` config block.
7. Unit tests:
   - `config_test.py`: precedence (CLI > env > settings > default),
     `merge_with` round-trips.
   - `cli_test.py`: each subcommand invoked via `CliRunner` against a
     `Latchkey` test double; assert JSON shape, exit codes, error
     paths. Reuse / extend fixtures from the existing
     `agent_setup_test.py` and `core_test.py`.
8. Run `just test-quick libs/mngr_latchkey`, then `just test-offload`
   for the full suite. Fix any ratchets.
9. Add `changelog/mngr-latchkey-cli.md` describing the new CLI.

## Notes

- **Edge case: opaque permissions path leaks if `link-permissions` is
  never called.** `create-agent-env` writes a `<uuid>.json` under
  `<plugin_data_dir>/permissions/`; if the caller never follows up,
  that file lingers forever (the JWT keeps working too). This matches
  the current minds behaviour and is fine; future cleanup can add a
  GC step but is out of scope here.
- **Edge case: `forward` exits while agents still exist.** Because
  `forward` stops the shared gateway on exit, in-flight agents lose
  their gateway endpoint until the next `forward` is started. This is
  the explicit consequence of the coupled-lifetime choice and is
  documented in `forward`'s `--help`. The on-disk per-agent
  `latchkey_permissions.json` files survive across `forward` restarts
  (the library never deletes them).
- **`ForwardStreamManager` dependency.** `mngr_latchkey` already
  declares `imbue-mngr-forward` as a dependency (currently used only
  for the `relay` helper). Reusing `ForwardStreamManager` is a strict
  expansion of that existing dependency, no new packages needed. If
  `ForwardStreamManager` turns out to be too tied to `mngr forward`'s
  envelope conventions, the fallback is to lift the discovery loop
  into a small wrapper inside `mngr_latchkey` — flagged here only
  because we discover this at implementation time, not now.
- **Defaults chosen for skipped Q&A questions.** The user did not
  answer the final round of follow-ups. The spec assumes:
  - Module layout (a): one `cli.py`, one `config.py`, one `plugin.py`,
    mirroring `mngr_forward`.
  - No SIGHUP / no `--no-observe` parity with `mngr forward` — keep
    `forward` minimal.
  - Plain stderr logging on `forward` startup; stdout empty.
  - Concurrent operation is documented as supported with no extra
    guardrails.
  - Tests: unit tests with `Latchkey` mocked; defer integration tests
    that need a real `latchkey` binary. Acceptance / release tests can
    be added later when there is CI infrastructure for it.
- **Things explicitly out of scope** (worth recording so a future
  expansion doesn't surprise anyone): `latchkey auth browser` /
  `latchkey services info` wrappers, permissions editing
  (`set_permissions_for_scope`), gateway introspection commands,
  selective `--agent-include` / `--agent-exclude` filtering on
  `forward`, JSONL event emission on `forward`'s stdout, `--strict` /
  `--allow-degraded` modes on `create-agent-env`.
