# Plan: importing a latchkey auth setup into eval boxes

**Status: PLAN ONLY — nothing implemented.** Scoped with two independent read-only security passes
(threat model + safe mechanics) over `vendor/mngr/libs/mngr_latchkey` and `latchkey@2.19.1`.

## Goal

Let unattended eval workspaces (Modal sandboxes running an autonomous AI agent) use the operator's
connected third-party services (Slack, Gmail, Drive, GitHub, AWS, …) via `latchkey curl`, so evals
can exercise real integrations. Must have a **`latchkey-copy` toggle that defaults OFF** and a hard
off switch the operator can always select.

## TL;DR recommendation

1. **Default OFF, opt-in per run.** Copying real credentials into a sandbox that runs arbitrary
   agent code, unattended, on a **shared** box + Modal env, is high blast-radius. Off is the only
   fully safe posture and must be the zero-config default.
2. **Do NOT ship the naive version** (full credential store + `admin-jwt` wildcard + a persistent
   in-box `encryption_key`). That gives a single prompt-injected eval live root access to every
   connected account, plus the key to decrypt everything offline and mint fresh admin JWTs forever.
3. **When opted in, reuse the pattern the codebase already has** — `remote_gateway.py` was built for
   "run the gateway on a remote box" and does the safe thing: a **filtered credential subset** (only
   allowlisted services), a **narrow per-agent permissions JWT** (not the `{"any":["any"]}` admin
   file), and an **ephemeral 0600 key file it deletes immediately**.
4. **Strongly prefer throwaway test accounts** over the operator's real accounts. A scratch Slack /
   Google / sandbox-AWS removes almost all blast radius even if every other control fails.

## Two layers (they are separate)

- **Credentials** — the encrypted store + its key. Portable: the key lives at
  `<latchkey_dir>/encryption_key` (0600, no machine binding), so copying `credentials.json.enc` +
  `encryption_key` makes creds identical in the box.
- **Authorization** — which agent may call which service. Separate permissions files; normally each
  agent carries a per-agent deny-all override JWT. Copying creds does NOT authorize anything.

## Threat model (why the naive version is unacceptable)

- `mngr latchkey admin-jwt` mints a `{"any":["any"]}` token the CLI itself calls "a root credential"
  (`cli.py:840`). It authorizes read + **write + destructive** schemas on every catalogued service:
  read all Slack/Gmail/Drive/GitHub-private data; send/delete messages as the operator; delete repos;
  AWS IAM/Secrets/KMS (coarse "any" schemas, no read-only split).
- **Exfil is game-over-plus.** 0600 doesn't stop the file's owner, and the box runs agent code. With
  the key an attacker decrypts the whole store offline (OAuth refresh tokens valid until revoked),
  derives the gateway password, and mints new admin JWTs indefinitely — so rotating the JWT is
  useless once the key leaks.
- **Shared box + shared Modal env** (`box.py` `MODAL_ENV_USER_ID = "evaluator"`) means one persona's
  agent, via the wildcard-unlocked minds-api-proxy, can list/export/SSH/destroy sibling workspaces.
- **Persistence in the accounts:** Gmail auto-forward filters, AWS IAM users/keys, GitHub deploy
  keys — all outlive sandbox teardown.
- Unattended = a prompt injection from any content the agent reads (a GitHub issue, a web page, a
  Slack message) inherits all of the above with no human in the loop.

## Design: the `latchkey-copy` toggle

Add to the eval config (default off):

```json
{ "latchkey": { "mode": "off" | "copy",
                "services": ["github-read-*", "slack-read-all"],   // allowlist; required when copy
                "authorization": "per-service" | "wildcard",       // default per-service
                "use_test_accounts": true } }
```

- `mode: "off"` (default): entrypoint does nothing — box mints its own fresh key, empty store.
  Today's behavior, unchanged.
- `mode: "copy"`: requires a non-empty `services` allowlist (fail closed if empty). Copies only a
  **re-encrypted subset** of the store for those services.

## Safe mechanics (verified)

Two disjoint subtrees share `LATCHKEY_DIRECTORY` (`/root/.minds-staging/latchkey` in the box):
upstream files at the root; plugin state under `mngr_latchkey/`.

**Copy ONLY these root files, and only a filtered subset of the store:**

| from operator dir | to box | notes |
|---|---|---|
| a re-encrypted `credentials.json.enc` (subset) | `<box>/credentials.json.enc` | via `latchkey auth re-encrypt --services …` (`core.py:785`); never the full store |
| `encryption_key` | `<box>/encryption_key` (0600) | required or the subset can't decrypt |

- **Never copy** anything under `mngr_latchkey/` (forward record = box's own gateway port; per-host
  permissions keyed to the operator's host-ids; opaque symlink handles; logs). Copying them clobbers
  the box's own state or dangles. Also leave `extensions/`, `config.json` alone (re-materialized).
- **0600 guarantee:** the reader rejects any group/other bit (`encryption_key.py` `_FORBIDDEN_PERMISSION_BITS`).
  In the entrypoint (root, before forward): `install -m 600 -o root -g root <staged> <dest>`. Stage
  via a mounted dir or `docker cp` between `docker create` and `docker start`; do **not** bind-mount
  the key file directly.
- **Timing:** the copy MUST precede `mngr latchkey forward` — the gateway reads the key once at spawn
  and derives the password + all JWTs from it. (Injecting later needs a forward restart.)
- **Ephemeral key (preferred):** rather than persist `encryption_key` in the box FS, pass it via the
  gateway process env and remove the file, mirroring `remote_gateway.py:457-500`. Keeps the master
  secret out of the agent-readable filesystem.

## Authorization options

- **A — `admin-jwt` wildcard** (inject as each agent's `LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE`):
  simplest, but root-equivalent. **Reject for the default.**
- **B — seed `latchkey_default_permissions.json`:** a trap — it's *shadowed* by the per-agent
  override JWT that `mngr create` already injects, so it silently has no effect. Avoid.
- **C — per-service override (recommended):** keep the normal deny-all baseline and mint a narrow
  permissions-file JWT (per-service, read-only where the catalog offers a split:
  `github-read-*`, `slack-read-all`, `google-drive-read-*`, `aws-s3-read`). Note some AWS schemas are
  coarse "any" (IAM/STS/KMS/SecretsManager) — exclude unless explicitly required. Requires the
  post-create host_id, so it's a post-create step, not a pre-forward file drop.

**Combined defense:** even an over-broad JWT is contained if the store only holds the allowlisted
services (`export_credentials_subset`) — unlisted services return "No credentials." So the filter is
the primary control; the JWT scope is defense-in-depth.

## Open decisions for the operator

1. **Test accounts vs real accounts?** Strongly recommend test. If real, accept that any opted-in
   batch may leak and plan rotation.
2. **Per-service allowlist granularity** — which services, read-only where possible.
3. **Per-eval isolation** — if credentialed access is on, don't share one box/env across personas;
   isolate + tear down aggressively (contradicts the current shared-box/shared-env design, so this
   is a real design cost to weigh).
4. **Egress + audit** — restrict outbound network from eval sandboxes and log every gateway call.

## Non-goals / notes

- No TTL on JWTs through the mngr wrapper (derived-password design assumes non-expiring); TTL only
  helps if the key isn't in the box anyway.
- Store filename `credentials.json.enc` is a constant in the npm binary (`latchkey@2.19.1`
  `config.js:51`), corroborated by this repo's `re-encrypt` contract; re-confirm on a latchkey major
  bump.
