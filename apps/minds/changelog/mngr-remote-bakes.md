Pool-host docs now point at the canonical `minds pool create` flow (via the
new `just bake-pool-host` / `just list-pool-hosts` / `just destroy-pool-host`
recipes) instead of the low-level `mngr imbue_cloud admin pool create` recipe
with hand-exported OVH creds and a hand-passed `--management-public-key-file`.
The env-aware wrapper derives the management SSH key + OVH credentials from the
activated tier's Vault entries automatically; for staging/production it also
resolves the host_pool DSN from Vault.

`minds pool {create,list,destroy}` now resolve the staging/production host_pool
DSN from `secrets/minds/<tier>/neon.DATABASE_URL` themselves (alongside the OVH
creds and management key they already read from Vault), so the commands work on
those tiers without a hand-passed `--database-url` even when invoked directly.
An explicit `--database-url` still wins, and dev/ci continue to auto-resolve the
DSN from their per-env `secrets.toml`.

Did a broader accuracy pass over the minds docs, fixing things that had drifted
since the Vultr->OVH pool migration:

- Replaced stale Vultr references in the pool / env-teardown flows with OVH
  (environments.md, vault-setup.md, host-pool-setup.md). Vultr mentions that
  describe the CLOUD launch mode are left as-is -- that mode still uses
  `--template vultr`.

- Corrected the Modal app names in vault-setup.md (`llm-<tier>` / `rsc-<tier>`,
  not `litellm-proxy-<tier>` / `remote-service-connector-<tier>`).

- Fixed the `minds run` config-resolution description in design.md and
  overview.md: there is no implicit `client.toml` fallback -- `minds run`
  refuses to start when neither `--config-file` nor `MINDS_CLIENT_CONFIG_PATH`
  is set.

- Fixed the Electron backend invocation in desktop-app.md (`run`, not
  `forward`, and it passes `--config-file`).

- Dropped the stale `--id <id>` flag from the `mngr create` examples
  (design.md, user_story.md) -- minds reads the agent id back from the
  `created` JSONL event.

- Corrected `minds` -> `minds run` (user_story.md), `mngr events` -> `mngr
  event` (latchkey-permissions.md), the spurious `kv/` Vault path prefix
  (host-pool-setup.md), and the broken `apps/minds/scripts/install.sh` install
  snippet in the README (replaced with the real from-source dev flow).
