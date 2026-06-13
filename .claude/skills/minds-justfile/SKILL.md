---
name: minds-justfile
description: Use the root justfile as the canonical entry point for ANY minds task -- minds app (desktop client), pool hosts, minds environments (activate/deploy/destroy), minds deployments, and minds tests. Before running ad-hoc `uv run minds ...` / `mngr imbue_cloud ...` commands, check the justfile for a named recipe; if none exists for the task, ADD one. Use whenever the request involves the minds app, pool/leased hosts, a minds env/tier (dev/staging/production), or a minds deploy.
---

# Minds tasks go through the justfile

The root `justfile` is the canonical, auditable, named home for every
operational minds task. Recipes encode the right flags, the right env-var /
Vault wiring, and the activation guards -- so they "just work" and stay
reviewable. Hand-rolled `uv run minds ...` / `uv run mngr imbue_cloud ...`
invocations drift, leak secrets, and miss steps (e.g. deriving the pool
management key from Vault, passing the host_pool DSN for staging/production).
This is the same class of mistake as reaching for the low-level
`mngr imbue_cloud admin pool create` recipe in the docs instead of the
env-aware `minds pool create` wrapper.

## The rule

When a task involves any of: the **minds app / desktop client**, **pool hosts /
leased mode**, a **minds environment or tier** (dev / staging / production),
a **minds deployment**, or **minds tests** --

1. **Look in the justfile first.** Run `just --list`, and/or
   `grep -nE 'minds|pool|deploy|env' justfile`. Read the recipe's leading
   comment block -- it documents prerequisites (almost all require an
   activated env) and usage.
2. **Use the recipe.** Prefer `just <recipe> ...` over the underlying command.
3. **If no recipe fits, ADD one.** Write a new, well-commented recipe that
   wraps the canonical command, then use it. Keep the recipe thin -- push any
   credential/secret resolution into the env-aware Python CLI rather than
   reimplementing it in bash. Do not paper over a missing recipe with a one-off
   shell command -- the point is a named, auditable script that the next person
   (or agent) can audit and re-run. Fix stale recipes you encounter the same way.
4. **Keep secrets out of argv where the wrappers already handle it.** The
   minds env-aware CLIs read OVH creds, the pool management key, and the
   staging/production host_pool DSN from Vault themselves (Vault addressing via
   `apps/minds/imbue/minds/envs/vault_reader.py`, which defaults
   `VAULT_ADDR`/`VAULT_NAMESPACE` to the HCP cluster). Don't re-export those by
   hand.

## Almost everything requires an activated minds env

Most minds recipes refuse to run without an activated env, by design:

```bash
eval "$(uv run minds env activate <name>)"      # use-only (mngr/minds run, pool, tests)
eval "$(uv run minds env activate --deploy <name>)"   # deploy mode (env deploy/destroy/recover)
```

`<name>` is `dev-<your-user>` for a personal dev env, or `staging` /
`production`. Deploy-mode (`--deploy`) additionally pins `MODAL_PROFILE`; it's
required only for `minds env deploy/destroy/recover`.

## Current minds-relevant recipes (run `just --list` for the live set)

Environments / deploy:
- `just deploy [args]` -- `minds env deploy` for the activated env (tier
  deploys need `--yes-i-mean-<tier>`).

Pool hosts (OVH-backed, leased mode):
- `just bake-pool-host <attributes-json> <region> [workspace_dir] [count] [extra flags]`
  -- bake pre-provisioned pool host(s) via `minds pool create`. The baked
  version comes from the `workspace_dir` checkout (a forever-claude-template
  checkout at the tag/branch you want); `<attributes>` is only the lease-match
  label. Extra flags forward to `minds pool create` (e.g. `--no-recycle`,
  `--mngr-source`).
- `just list-pool-hosts` -- list `pool_hosts` rows for the activated env.
- `just destroy-pool-host <pool-host-id>` -- cancel one host's OVH VPS + drop its
  row (manual single-host teardown; steady-state release is automatic via the
  connector's hourly cron, and `minds env destroy` tears down a whole tier).

Desktop client / dev loop:
- `just minds-start` / `just minds-stop` / `just minds-build`
- `just propagate-changes <agent>` -- sync local mngr into a running Docker agent.
- `just forward-system-interface <agent>` -- Cloudflare tunnel for an agent.
- `just sync-vendor-mngr [fct]` -- sync `vendor/mngr` in forever-claude-template.
- `just create-new-mind-repo <name> [parent_dir]` -- new private FCT clone.
- `just minds-tailwind` -- fetch the Tailwind bundle.

Tests:
- `just minds-test-deployment [args]`, `...-cleanup`, `...-up`, `...-down`,
  `minds-test-services-against`, `minds-test-deployment-only`,
  `just minds-test-electron`, `just test-offload-minds-snapshot <image-id>`.

## Related skills

- `minds-dev-workflow` -- the end-to-end dev iteration loop (uses these recipes).
- `release-minds` -- cut a minds release.
