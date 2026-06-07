# imbue_cloud slow/rebuild path doesn't run the agent container under gVisor (runsc)

Status: **deferred** — to be tackled on a separate branch. This documents the
problem, why it's not fixed by the `mngr/runsc-everywhere` work, the constraints,
and proposed fixes.

## TL;DR

The `mngr/runsc-everywhere` branch makes the docker / lima / vultr / ovh
providers run the agent container under gVisor (`runsc`). For `imbue_cloud`:

- **Fast path is covered.** Pool hosts are baked by the OVH provider (whose
  template + provider config now enable runsc), so an adopted pre-baked
  container already runs under runsc.
- **Slow / rebuild path is NOT covered.** When `imbue_cloud` rebuilds a
  container on a leased pool host, it builds a fresh `vps_docker` provider with
  *default* config (`docker_runtime=None`, no hardening start-args), so the
  rebuilt container runs under the default runtime (`runc`), not runsc.

This is a fallback path, so impact is limited, but it's a real gap.

## Background: imbue_cloud's two create paths

`mngr create ...@<host>.imbue_cloud_<slug>` (the way minds creates pool-backed
workspaces) has two paths, selected by `-b fast_mode=...`. minds tries them in
order (`apps/minds/.../desktop_client/agent_creator.py` `_create_imbue_cloud_with_fallback`):

1. **Fast path** (`fast_mode=require`) — `ImbueCloudProvider._create_host_fast_path`
   in `libs/mngr_imbue_cloud/.../instance.py`. Leases a pool host whose baked
   attributes exactly match and **adopts its pre-baked container as-is** (no
   rebuild). Explicitly **rejects** `--image` / `--start-arg`
   (`instance.py` ~line 1139: "fast_mode=require does not accept --image or
   --start-arg").
2. **Slow path** (`fast_mode=prevent`) — `_create_host_slow_path` →
   `_rebuild_leased_container` → `_build_delegated_vps_provider`. Leases any free
   host, tears down its baked container, and **rebuilds** it from the FCT
   Dockerfile via a delegated `vps_docker` create.

## Why the fast path is covered

Pool hosts are baked via `mngr imbue_cloud admin pool create`, which uses the
forever-claude-template `ovh` create template + `[providers.ovh]` provider
config. On `mngr/runsc-everywhere` those carry:

- `[providers.ovh]`: `install_gvisor_runtime = true`, `docker_runtime = "runsc"`
- `[create_templates.ovh]`: `start_arg__extend = ["--security-opt=no-new-privileges", "--workdir=/"]`

So a freshly-baked pool container runs under runsc, and the fast path adopts it
unchanged → runsc for free.

Caveat (already accepted, "no backwards compat"): hosts baked **before** this
change won't be on runsc until the pool is re-baked.

## Why the slow path is NOT covered

`_rebuild_leased_container` builds the delegated provider in
`_build_delegated_vps_provider` (`libs/mngr_imbue_cloud/.../instance.py` ~line 1351):

```python
vps_config = VpsDockerProviderConfig(
    backend=ProviderBackendName("vps_docker"),
    host_dir=self.config.host_dir,
    container_ssh_port=self.config.container_ssh_port,
)
```

This constructs `VpsDockerProviderConfig` with **defaults** for everything else,
so `docker_runtime` is `None` and `default_start_args` is empty. The rebuilt
container's `docker run` therefore gets no `--runtime runsc` and none of the
`--workdir=/` / `--security-opt=no-new-privileges` hardening. (See
`mngr_vps_docker/.../instance.py`: `runtime_args = ("--runtime", self.config.docker_runtime)
if self.config.docker_runtime is not None else ()`.)

`ImbueCloudProviderConfig` (`libs/mngr_imbue_cloud/.../config.py`) extends
`ProviderInstanceConfig`, **not** `VpsDockerProviderConfig`, so it has no
`docker_runtime` / `install_gvisor_runtime` / `default_start_args` fields to
forward in the first place.

## The constraint that blocks the "easy" fix

The obvious fix — add `start_arg__extend = ["--workdir=/", "--security-opt=no-new-privileges"]`
to `[create_templates.imbue_cloud]` so the rebuild's `docker run` gets them —
**does not work and actively breaks creation**:

- minds applies the `imbue_cloud` template to **both** create attempts (only the
  `-b fast_mode=...` differs).
- The first attempt is `fast_mode=require`, which **rejects** `--start-arg` with
  a plain `MngrError` (not `FastPathUnavailableError`), so minds' fallback logic
  re-raises it instead of retrying the slow path → imbue_cloud creation fails
  entirely.

Additionally, even for the slow path the template's repo/branch must remain a
**remote** ref the pool host can clone; a local-worktree path would break it
(this is the same class of issue as the `_operator_workspace_default` opt-in).

So the runsc settings for the slow path cannot ride on the template; they must
come from **provider config** that the delegated `vps_docker` provider reads.

## Proposed fix (for the future branch)

1. Add `docker_runtime: str | None` and a run-args field (e.g.
   `default_container_run_args: tuple[str, ...]`, or reuse `default_start_args`)
   to `ImbueCloudProviderConfig`. `install_gvisor_runtime` is likely unnecessary
   here because the leased pool host already has runsc installed from its OVH
   bake — but verify.
2. In `_build_delegated_vps_provider`, propagate those onto the
   `VpsDockerProviderConfig` it constructs (`docker_runtime=self.config.docker_runtime`,
   `default_start_args=self.config.default_container_run_args`).
3. Decide where the per-account `imbue_cloud` provider gets these values. The
   per-account block (`[providers.imbue_cloud_<slug>]`) is written by minds
   bootstrap (`apps/minds/.../bootstrap.py` `set_imbue_cloud_provider_for_account`),
   not by FCT settings. Options:
   - have minds bootstrap write `docker_runtime`/run-args into that block, or
   - set sensible defaults on `ImbueCloudProviderConfig` (broader: affects all
     imbue_cloud users; assumes the leased host has runsc), or
   - an `MNGR__PROVIDERS__...__DOCKER_RUNTIME` env var in the minds/bake env.
   Recommendation: minds-bootstrap-written block, mirroring how the other
   per-account knobs are set, so it's scoped to minds and explicit.

## Testing

- Unit: assert `_build_delegated_vps_provider` produces a `VpsDockerProviderConfig`
  with `docker_runtime`/run-args propagated from `self.config`.
- End-to-end (release/manual): force the slow path (`fast_mode=prevent`) against a
  pool and confirm the rebuilt container runs under runsc
  (`docker inspect --format '{{.HostConfig.Runtime}}' <container>` → `runsc`).

## Note on the runsc workaround args

Under gVisor the agent container needs `--workdir=/` (runsc aborts when the
image WORKDIR `/mngr/code`, inside the mounted `/mngr` volume, already exists as
the process cwd) and `--security-opt=no-new-privileges`. Whatever carries
`docker_runtime=runsc` to the slow-path rebuild must also carry these, exactly as
the docker / vultr / ovh templates do via `start_arg`.

## Relevant code

- `libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/instance.py` — `_create_host_fast_path`,
  `_create_host_slow_path`, `_rebuild_leased_container`, `_build_delegated_vps_provider`.
- `libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/config.py` — `ImbueCloudProviderConfig`.
- `libs/mngr_vps_docker/imbue/mngr_vps_docker/config.py` — `VpsDockerProviderConfig`
  (`docker_runtime`, `install_gvisor_runtime`, `default_start_args`).
- `libs/mngr_vps_docker/imbue/mngr_vps_docker/instance.py` — where `--runtime` is added.
- `apps/minds/imbue/minds/desktop_client/agent_creator.py` — `_create_imbue_cloud_with_fallback`
  (the require-then-prevent fallback) and the `-b repo_url=`/`repo_branch_or_tag=` wiring.
- `apps/minds/imbue/minds/bootstrap.py` — `set_imbue_cloud_provider_for_account`.
- forever-claude-template `.mngr/settings.toml` — `[providers.ovh]` / `[create_templates.ovh]`
  (the bake) and `[create_templates.imbue_cloud]` (must NOT gain `start_arg`).
