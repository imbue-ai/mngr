# Incident report: Minds desktop client bricked by an unparseable `aws` provider block

**Reporter:** diagnosis from bowei's `minds (1).log`
**Date of incident:** 2026-06-15
**Affected user:** bowei@imbue.com (running the bundled Minds.app, not from source)
**Severity:** Critical — the desktop client became permanently unusable (no agent creation, no forwarding) and could not self-recover across restarts.

---

## 1. Summary

A minds build from the AWS-provider transition window wrote an **enabled** `[providers.aws-us-east-1]` block (plus three sibling regions) into bowei's mngr host-profile settings file, but the `mngr` toolchain bundled with that same app **does not ship the `aws` backend plugin**. mngr's config parser is strict: an enabled provider block whose backend cannot be resolved aborts the entire config load. Because config loading is on the critical path for *every* `mngr` subprocess, this single block took down agent creation, then the latchkey-forward supervisor, and finally the whole client on restart.

The root cause is a **packaging split-brain**: the commit that added the code which *writes* the AWS provider blocks (the minds desktop layer) did not add the `imbue-mngr-aws` plugin to the bundled mngr toolchain that *reads* them. The two halves of the app were shipped out of sync.

This class of bug was independently discovered and fixed on `main` on 2026-06-15 (commit `55205e6f5`, PR #2125), which is the same day as — but after — bowei's session.

---

## 2. Symptoms (from the log)

In chronological order:

1. **First agent creation ("assistant") succeeded.** The imbue_cloud fast/slow paths failed only for lack of pool hosts (`FastPathUnavailableError`, then `ImbueCloudLeaseUnavailableError` — "ask Josh to provision more"), and it fell back to a LIMA VM that built and started cleanly (`{"event": "created", "agent_id": "agent-323c9c88…"}`, "Workspace ready"). **Config parsed fine at this point** — the imbue_cloud attempts got all the way to the connector lease.

2. **Every subsequent agent creation died instantly** with:
   ```
   Error: Provider 'aws-us-east-1' references unknown backend 'aws'. If this backend
   is provided by a disabled plugin, either enable the plugin or add
   `plugin = "<plugin-name>"` to this provider block. Currently disabled plugins: recursive
   ```
   This happened regardless of launch mode — imbue_cloud, the LIMA fallback, and a manual LIMA retry all failed identically, because the failure is at config-parse time, before any provider logic runs.

3. **The latchkey-forward supervisor wedged.** When the user toggled providers in the settings panel (creating `is_enabled=false` blocks for `ovh`, `modal`, `docker`, `ssh`), each toggle SIGHUP'd `mngr latchkey forward` to reload. On reload it re-parsed the now-broken config, failed, and never re-stamped its gateway port, producing the endless:
   ```
   WARNING: permission-requests stream dropped (Timed out after 30.0s waiting for
   `mngr latchkey forward` to stamp its bound gateway port …; is the supervisor stuck?)
   ```

4. **Restarts did not recover.** On each fresh `minds run`, `mngr forward` exited code 1 immediately with the same `aws-us-east-1` error, firing the CRITICAL notification:
   ```
   `mngr forward` exited with code 1. The minds desktop client is no longer forwarding
   agent traffic; restart minds to recover.
   ```
   The app was fully bricked.

---

## 3. The fatal mechanism

The error originates in `libs/mngr/imbue/mngr/config/loader.py:603-622`, in `_parse_providers`:

- A provider block is silently skipped if its `plugin` is in `disabled_plugins`, **or** if it has `is_enabled = false` **and** its backend isn't registered.
- `aws` is **not** in the disabled set (only `recursive` is — that's a minds default), and the `aws-us-east-1` block is **enabled**, so neither escape clause applies.
- It then calls `get_provider_config_class("aws")`, which raises `UnknownBackendError` because the `aws` backend plugin is not installed in this runtime.
- In strict mode (the default) that becomes a `ConfigParseError`, which aborts the **entire** config load — not just that one provider.

Because config loading is a prerequisite for `mngr create`, `mngr forward`, `mngr list`, and `mngr observe`, one enabled provider for a missing backend disables the whole toolchain.

---

## 4. Where the `aws-us-east-1` block came from

It was written by minds' own desktop code — **not** by the connector and **not** by the imbue_cloud plugin.

- The only writer of `aws-<region>` provider blocks in the codebase is `_write_aws_provider_blocks` in `apps/minds/imbue/minds/bootstrap.py:174`, called from `_ensure_mngr_settings` during minds startup.
- It writes one block per region in `CONFIGURED_AWS_REGIONS = ("us-east-1", "us-east-2", "us-west-1", "us-west-2")` (`apps/minds/imbue/minds/primitives.py:24`), each named `aws-` + region. `aws-us-east-1` is the first of the four.
- Each block sets exactly `backend = "aws"`, `default_region`, `default_instance_type`, `install_gvisor_runtime`, `docker_runtime` — a precise fingerprint of the block that trips the parser.
- It is gated by `_aws_credentials_plausibly_configured()` (`bootstrap.py:145`): true when `AWS_ACCESS_KEY_ID`/`AWS_PROFILE` is set or `~/.aws/credentials`/`~/.aws/config` exists. bowei, as an imbue developer, has AWS credentials on his laptop, so the gate fired and the blocks were written.

Ruled out:
- **The connector.** `mngr_imbue_cloud` and the production connector (`rsc-production-api`) never write `[providers.*]` blocks into settings.toml — minds owns that file. The imbue_cloud plugin only leases hosts; it does not persist provider config locally.
- **The repo's own `.mngr/settings.toml`.** That file *does* declare a `[providers.aws]` block (`.mngr/settings.toml:102`, name `aws`, for the repo's image builds), but it only affects mngr when run with cwd inside the repo. minds spawns mngr with `cwd=$HOME`, where there is no `.mngr/` project layer, and bowei's error names `aws-us-east-1` (the host-profile block), not `aws`. Different instance of the same bug class; not what hit bowei.

---

## 5. Root cause: a writer/reader packaging split-brain

The minds desktop process (the **writer**) and the bundled mngr toolchain (the **reader**) are two separate Python environments that were shipped out of sync.

- The **writer** ran from the app bundle (`/Applications/Minds.app/Contents/Resources/pyproject/imbue/minds/…`). Its minds code includes `_write_aws_provider_blocks`, added 2026-06-14 in commit `9c9515d98` ("minds: add AWS compute provider; rename CLOUD launch mode to VULTR").
- The **reader** ran from the per-user workspace venv (`/Users/bowei/.minds/.venv/lib/python3.12/site-packages/imbue/…`, visible in the shutdown traceback at log lines 805-828). That venv's mngr had **no `aws` backend plugin**.

The defect is that commit `9c9515d98` added the block-writer but **did not** add the `imbue-mngr-aws` plugin to the bundled mngr toolchain. Verified directly: `9c9515d98` touched only `apps/minds/imbue/minds/bootstrap.py` and `apps/minds/imbue/minds/primitives.py`. It did not touch any of the four bundled-workspace-package lists (`scripts/build.js`, `electron/env-setup.js`, `scripts/build_test.py`, `electron/pyproject/pyproject.toml`) that determine which mngr plugins are installed into the toolchain that reads the settings file.

This is corroborated by the post-mortem in `apps/minds/changelog/wz-fix-macos-launch-welcome-selectors.md`:

> The AWS-provider work added `imbue-mngr-aws` as a minds dependency but did not add it to the four bundled-workspace-package lists … Without a local-wheel source override, the build's `uv lock` resolved `imbue-mngr-aws` from PyPI, where the packaged app's 14-day dependency cooldown (`exclude-newer = "14 days"`) rejected the freshly-published `v0.1.1` — failing `pnpm dist` on every build.

### On "the app predates the aws work"

The intuition is half-right and the discrepancy *is* the bug:

- The **bundled mngr toolchain** does predate the `aws` plugin — that's why it can't parse the block.
- The **minds desktop layer** does **not** predate the aws work — it must contain the 2026-06-14 `_write_aws_provider_blocks`, because that is the only thing that could have written `aws-us-east-1` into his profile.

So bowei's app is from the AWS-transition window with the two halves bundled inconsistently (or a newer-desktop build wrote the block into his persistent `~/.minds` profile, after which an older-toolchain run could no longer parse it — `~/.minds` persists across app versions). Either way, the failure is the writer/reader version skew, not a pure pre-aws build.

---

## 6. Why it was unrecoverable

- The block is **enabled**, so the parser cannot skip it (the `is_enabled=false` escape at `loader.py:601` doesn't apply).
- Config parse is a hard prerequisite for `mngr forward`, so forwarding can't start.
- The user's attempt to disable providers via the panel made it worse: each toggle SIGHUP'd the forward, and the reload re-hit the parse failure, so the supervisor never came back.
- The block lives in the persistent `~/.minds` profile, so restarting the app re-reads the same poison and fails again immediately.

---

## 7. Fix status

Fixed on `main` on 2026-06-15 (after bowei's session):

- `55205e6f5` — "minds build: bundle imbue-mngr-aws so the packaged uv lock resolves" — adds `imbue-mngr-aws` to all four bundled-package lists, enforced by the `test_workspace_package_lists_are_consistent` drift guard. A build from this point forward bundles a mngr that *can* parse the AWS provider blocks the desktop writes.
- Merged via PR #2125 (`c72d8c390`), which also fixed a related cwd variant in CI's `launch_to_msg` cross-check (`Provider 'aws' references unknown backend 'aws'` when mngr ran from the repo root).

bowei's build predates `55205e6f5`, so upgrading to a current build resolves it.

---

## 8. Remediation

**Immediate unblock (no upgrade required):** edit `~/.minds/mngr/profiles/*/settings.toml` and set `is_enabled = false` on each `aws-*` block (`aws-us-east-1`, `aws-us-east-2`, `aws-us-west-1`, `aws-us-west-2`), or delete the blocks. With them disabled, `_parse_providers` skips them (`loader.py:601`) and forwarding/creation recover. Upgrading to a post-`55205e6f5` build is the durable fix.

**Confirmation checks on the affected machine:**
1. `cat ~/.minds/mngr/profiles/*/settings.toml` — expect enabled `[providers.aws-us-east-1]` (+ the other three regions) with `backend = "aws"`.
2. `~/.minds/.venv/bin/python -c "import imbue.mngr_aws"` (or `pip list | grep mngr-aws` in that venv) — expect ImportError / absent. The pair proves the writer/reader skew.

---

## 9. Recommended hardening (defense in depth)

The 2026-06-15 fix repairs *this* instance by keeping the package lists in sync, but the underlying fragility — minds writing an enabled provider block for a backend the bundled mngr may not have — remains a footgun for any future backend added the same way. Recommended:

- **Gate `_write_aws_provider_blocks` on the backend actually being registered** in the mngr that minds will spawn. If the `aws` backend isn't resolvable, write the blocks with `is_enabled = false` (or skip them), so a toolchain skew degrades gracefully instead of bricking the client.
- **Never let one unresolvable provider abort the whole config load for a long-lived daemon.** Consider parsing the forward/observe config non-strictly (warn-and-skip unknown-backend providers) so a single bad block can't kill forwarding. Keep strict parsing for explicit create operations where failing loudly is correct.
- **Add a regression test** asserting that minds does not emit an *enabled* provider block for a backend absent from the bundled toolchain — catching this class of split-brain regardless of whether the four package lists happen to agree.
