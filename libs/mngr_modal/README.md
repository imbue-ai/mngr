# imbue-mngr-modal

Modal provider backend plugin for [mngr](https://github.com/imbue-ai/mngr).

This plugin enables mngr to create and manage agents running in [Modal](https://modal.com) cloud sandboxes. Each sandbox runs sshd and is accessed via SSH, just like any other mngr host.

## Installation

`imbue-mngr-modal` is included by default when you install `mngr`. To install separately:

```bash
uv pip install imbue-mngr-modal
```

## Usage

```bash
# Create an agent on Modal
mngr create @.modal

# Create with custom resources
mngr create @.modal -b --cpu=2 -b --memory=4 -b --gpu=a10g

# Create with a custom Dockerfile
mngr create @.modal -b --file=path/to/Dockerfile

# Block outbound network access
mngr create @.modal -b offline

# Give the sandbox a tunnel for a TCP port the workload listens on (UDP is not available on Modal)
mngr create @.modal --publish 8080
```

See `mngr create --help` for all available options.
A published port's tunnel host and port are recorded on the host and reported by `mngr list --format json` under `host.published_endpoints`; every sandbox start (including a snapshot restore) allocates fresh tunnels and re-records them.

## Configuration

Configure the Modal provider in your mngr settings:

```toml
[providers.modal]
default_cpu = 2.0
default_memory = 4.0
default_sandbox_timeout = 1800
# Run sandboxes on Modal's VM runtime (https://modal.com/docs/guide/vm-sandboxes).
# Defaults to false (Modal's default gVisor runtime); set to true to opt in.
is_vm_runtime_enabled = false
```

### Credentials

By default a `DIRECT` instance authenticates the way the Modal SDK does on its own: `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` in the environment, else the active profile in `~/.modal.toml` (created by `modal token new`).

A provider instance can instead carry its own Modal token, independent of the environment and of `~/.modal.toml`, so several instances can point at different Modal accounts:

```toml
[providers.work_modal]
backend = "modal"
token_id = "ak-..."
token_secret = "as-..."
```

Or it can name a profile of this machine's `~/.modal.toml` (written by `modal token new --profile work`), so the block carries no secret at all:

```toml
[providers.work_modal]
backend = "modal"
modal_profile = "work"
```

The profile is read from the file (at `MODAL_CONFIG_PATH`, else `~/.modal.toml`) every time the provider is built, not through the Modal SDK, which loads the file once at import; so a long-lived `mngr observe` picks a newly created profile up on its next check. A profile missing on this machine reports the provider as awaiting its credential source (`ProviderCredentialSourceMissingError`, with `modal token new --profile work` as the fix) rather than ending its discovery poller. The chosen profile's own `server_url` is not honored: the SDK's active profile decides it. `modal_profile` and `token_id` / `token_secret` are mutually exclusive.

`token_id` and `token_secret` must be set together, though not necessarily in the same config layer (the secret can come from `settings.local.toml` or the environment while the id sits in `settings.toml`). They apply to `DIRECT` mode and are used for every Modal call the instance makes (sandboxes, apps, volumes, the `modal` CLI invocations for environment creation and deploys). Like every provider setting they can also come from the environment, without touching the SDK's own variables: `MNGR__PROVIDERS__WORK_MODAL__TOKEN_ID` / `MNGR__PROVIDERS__WORK_MODAL__TOKEN_SECRET` (the provider instance name, upper-cased, in place of `WORK_MODAL`; env-var segments allow only letters, digits and underscores, so an instance that should be configurable this way needs a name like `work_modal` rather than `work-modal`). An environment layer for a named instance also sets `MNGR__PROVIDERS__WORK_MODAL__BACKEND=modal`, since a layer that names no backend resolves it from the instance name.

## SSH key material

Every keypair that grants access to a sandbox is minted per host under the provider instance's key dir, `<profile>/providers/modal/<instance>/keys/host_keys/<host-uuid-hex>/` (`modal_ssh_key` / `modal_ssh_key.pub`, the client key, and `host_key` / `host_key.pub`, the sandbox sshd's host keypair, injected at every boot). The host-key pins live in that instance's `keys/known_hosts` store, attributed per host; the per-host dir carries a `known_hosts` symlink to it. This is the same layout as the vps providers (see `libs/mngr_vps/README.md`), published as `MODAL_HOST_ACCESS_MANIFEST` (`constants.py`), so a consumer carrying a host's access material to another machine handles every backend the same way.

One shared client key is read but never minted: `<profile>/providers/modal/<instance>/keys/modal_ssh_key`. When a host has no per-host client key on this machine, the provider presents that key instead of minting a per-host one. It exists for sandboxes that authorize an operator-installed key through their own `authorized_keys` (the TMR CI and mngr-seer debug flows register the team's keys on every sandbox they create; see `libs/mngr_tmr/scripts/setup_tmr_ci_debug.py` and `apps/mngr_issue_generator/scripts/setup_debug_access.py`). A per-host key, when present, always wins. Neither key is ever minted on a read path: a sandbox this machine holds no key for fails with `ModalHostKeyMissingError` (it was created on another device, or before per-host keys existed, so reach it from the device that created it or sync its record here).

Sandboxes created before the per-instance key dir existed (under the old `providers/modal/keys/` root) are not reachable after upgrading: their key material is not looked for under the old root. Destroy and recreate them.
