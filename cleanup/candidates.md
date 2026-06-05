# wz/minds_onboard cleanup tracker

Baseline (rollback point): mngr `1b91eb590` × FCT v0.2.35 = pilot `fb96b1b3` × ToDesktop `260605t4i0qqmlg`. ci.yml + launch-to-msg both verified green; users have the shipped 0.2.35 binary.

Inventory produced by six parallel read-only subagents on 2026-06-05. Reports were narrower than expected because `f794b3e9` (revert libs/ changes) and `a28f5a14` (drop PR #1869 cred-bridge) already removed two large classes of branch-only hacks earlier in the iteration.

Green gate per candidate = launch-to-msg.yml + ci.yml both green.

## Tier 1 — high-confidence (test first)

### todowrite-cleanup-pilot (PILOT)

Bundles two pilot-side dead-code deletions into one CI cycle.

- Delete `scripts/claude_block_todowrite.sh` (17-line PreToolUse hook, not referenced anywhere — `grep -rn "claude_block_todowrite"` only matches itself).
- Drop `permissions.deny: ["TodoWrite"]` block from `.claude/settings.json` (no-op under `--dangerously-skip-permissions`, which `.mngr/settings.toml:54` sets).

FCT main already removed both in `c90b1dfb` ("Replace permissions.deny and PreToolUse hooks with --disallowed-tools") with rationale that `permissions.deny` never fires under `--dangerously-skip-permissions`. Pilot's `cli_args` already contains `--disallowed-tools ...,TodoWrite,...`, so functional coverage is intact.

- **status**: **verified_stale**
- **test path**: pilot test branch → tag `v0.2.36-rc1-todowrite-cleanup` → launch-to-msg with that template_ref
- **expected blast radius**: zero (both items are dead/no-op today)
- **landed**: pilot `749e234d` (fast-forward of fb96b1b3 -> 749e234d). v0.2.35 tag unchanged at fb96b1b3.

### restore-supply-chain-cooldowns (MNGR)

Two sibling reverts:

- Restore `apps/minds/pnpm-workspace.yaml`'s `minimumReleaseAge: 20160` and `minimumReleaseAgeExclude: [latchkey, "@imbue-ai/detent"]` (main's targeted exempt-list shape, not pre-disable wholesale).
- Restore `apps/minds/electron/pyproject/pyproject.toml`'s `[tool.uv] exclude-newer = "2026-05-23T00:00:00Z"` (or a more recent fixed timestamp).

Cited blocker `modal 1.4.3` published 2026-05-18 — now 17 days old, comfortably outside the 14-day cooldown. Both gates were disabled "temporarily while CI churns"; that period is over.

- **status**: **verified_stale**
- **test path**: mngr candidate branch → launch-to-msg with template_ref=v0.2.35 + ci.yml on the candidate SHA
- **expected blast radius**: ToDesktop `pnpm install` + `uv lock` regeneration both re-enforce cooldowns. If any transitive PyPI dep has only published a >cutoff release with no older one, build breaks. Mitigation: pick a recent fixed `exclude-newer` and audit if it fails.
- **landed**: cherry-picked to wz/minds_onboard (c930b05ed). First launch-to-msg verify hit a Lima boot SIGTERM flake (`limactl start failed exit code -15` at ~10min in `CREATING_WORKSPACE`); rerun greened. ci.yml green on both attempts. Cooldowns are no-op under --frozen-lockfile so they couldn't have caused lima boot failures.

## Tier 2 — medium-confidence (test after Tier 1)

### restore-stop-hook-enabled-when (PILOT)

Restore `.reviewer/settings.json`'s `enabled_when` from `""` (always-disabled) to main's `test -n "${MNGR_AGENT_STATE_DIR:-}" || test -n "${SCULPTOR_API_PORT:-}"`.

Original disable rationale (commit `654ccc8a` 2026-04-28) cited two root causes: (a) `.reviewer/logs/` not gitignored, causing the stop-hook's "repo must be clean" precondition to fail on its own log file and trigger a 16-min commit/gitignore loop; (b) general orchestrator latency in the imbue-code-guardian plugin. Main fixed (a) ~7h later in `4bcf74ca` ("Prevent silly files"); (b) is in plugin scope and can't be verified from FCT alone.

- **status**: **verified_stale**
- **test path**: pilot test branch → tag `v0.2.36-rc2-stop-hook` → launch-to-msg with that template_ref
- **expected blast radius**: if (b) is still live, first-message latency spikes back to ~16 minutes. launch-to-msg's end-to-end timing will surface it.
- **landed**: pilot ff-merged to c135679e. v0.2.35 tag unchanged. Both ci.yml and launch-to-msg verify green first try.

## Tier 3 — low-confidence (defer; revisit if Tier 1+2 leave runner time)

### bundled-lima-pinned-at-2.0.3 (MNGR)

Bump `LIMA_VERSION` in `apps/minds/scripts/build.js` from `2.0.3` to `2.1.1` (main's value). Upstream issue `lima-vm/lima#5042` is closed but `#4558` (umbrella) still open. Need to verify the 2.1.x changelog confirms the gvisor-tap-vsock fix landed before bumping.

- **status**: **still_needed** (no CI test needed)
- **test path**: mngr candidate branch → launch-to-msg with template_ref=v0.2.35 (lima boot is full end-to-end)
- **deep-dive verdict (2026-06-05)**: lima v2.1.1's `go.mod` pins `containers/gvisor-tap-vsock v0.8.8` -- the exact regressed version the 2.0.3 pin dodges. v2.1.2 bumps to 0.8.9 but 0.8.9's changelog does not mention the SSH-fresh-connection wedge or any systemd-255 fix. Umbrella `lima-vm/lima#4558` is still open with no closing PR; the original audit's `#5042` reference was a different bug (FD leak after ~2048 forwards on Ubuntu 25.10, patched in `pkg/portfwd/client.go` -- doesn't touch the SSH hang). The 2.0.3 pin remains correct.

### revert-lima-start-new-default-timeout (MNGR, found 2nd pass)

The branch's `libs/mngr_lima/imbue/mngr_lima/limactl.py:155` has `timeout: float = 1800.0`; main is at `600.0`. The only caller (`instance.py:467-473`) passes `timeout=self.config.vm_start_timeout_seconds` explicitly, so the function default is dead code. `f794b3e98` ("Revert all libs/ changes; keep branch scoped to apps/minds") missed this one line.

- **status**: **verified_stale**
- **test path**: mngr-rc-lima-timeout-revert (PR #1938) → launch-to-msg with template_ref=v0.2.35 + ci.yml
- **expected blast radius**: zero (dead default)
- **landed**: cherry-picked to wz/minds_onboard (b7628677f). Both launch-to-msg verify and ci.yml green first try. Also removed `libs/mngr_lima/changelog/wz-minds_onboard.md` since the entry no longer describes a branch-only change.

### uv-tool-install-editable-mode (PILOT, found 2nd pass)

Pilot's `.mngr/settings.toml:235-236` uses `uv tool install -e ...` and `--with-editable` for mngr/mngr_claude/mngr_modal; FCT main uses non-editable `uv tool install ...` and `--with`. Original symptom (commit `3684fa01`): "uv tool install emits 'Requirements contain conflicting URLs for package imbue-mngr'" — diagnosed against `a90c78a2`-era vendor/mngr. Subsequent refreshes (`d0d861ab`, `fb96b1b3`, `749e234d`-merge) may have eliminated the URL-form mismatch. Editable mode is a resolver workaround; nothing imports `mngr_modal` so its `--with-editable` buys nothing functionally.

- **status**: **verified_stale**
- **test path**: pilot-rc-uv-tool-non-editable → tag `v0.2.36-rc4-uv-tool-non-editable` → launch-to-msg with that template_ref. Provision script runs inside lima at agent-create time, so a real lima create is the only valid verification.
- **expected blast radius**: high if conflict recurs — provision Phase B fails before `mngr` lands, no agent boots, verify aborts. Zero if conflict has been resolved.
- **landed**: pilot ff-merged to cb2091cf. Both ci.yml and launch-to-msg verify green (uv tool install inside lima Phase B completed without conflict; subsequent vendor/mngr refreshes did resolve the URL-form mismatch). v0.2.35 tag unchanged.

### dead-web-view-script (PILOT, found 3rd pass)

`scripts/web_view.py` + `scripts/web_view_test.py` (283 lines) are superseded by `scripts/layout.py`. Replacement is PR #73 (commit `ed6b1a3b`); `blueprint/agent-layout-ops/plan-agent-layout-ops.md` literally documents the intended rename. The merge in `c2f1b829` brought `layout.py` in but never deleted `web_view.py`. Verification: `grep -rn web_view` across pilot (excluding vendor) finds only the file itself, its test, and the stale plan doc — zero call sites in `.toml`/`.json`/`.sh`/SKILL.md. The workspace-server endpoints `web_view` targeted were also atomically replaced.

- **status**: in_flight (iter 6)
- **test path**: pilot-rc-dead-code / v0.2.36-rc5-dead-web-view → launch-to-msg with that template_ref
- **expected blast radius**: nil (no callers; replacement covers all functions)

### duplicate-port-zero-guard (MNGR, found 3rd pass)

`apps/minds/imbue/minds/cli/run.py:157-163` raises `click.UsageError("--port must be > 0")` as the first statement of `minds run`. mngr_forward already raises a near-identical `click.UsageError("--reverse local port must be > 0, ...")` at the boundary that consumes the value. Neither Electron's backend.js nor any CI/dev script ever passes `--port 0`, so the guard never fires for any real caller. Has its own unit test `test_run_rejects_port_zero` which becomes vestigial.

- **status**: in_flight (iter 7)
- **test path**: mngr-rc-drop-port-zero-guard (PR #1939) → ci.yml + launch-to-msg with template_ref=v0.2.35
- **expected blast radius**: zero in production

### stale-do-something-new-paragraph (PILOT)

`.agents/skills/do-something-new/SKILL.md:103-110` — 8-line block inserted before main's restructured Step 5/6. Main has since substantially restructured the skill (`e63ab69d`, `9b9a05ed`); the paragraph either restates or contradicts main's now-canonical framing.

- **status**: **deferred** (medium confidence; skill semantics have no automated verifier)
- **test path**: no CI test possible; needs human read-through against main's restructured steps
- **recommendation**: drop into a follow-up review pass after the human reads main's Step 5/6

### homebrew-path-augmentation (MNGR)

Drop the explicit `homebrewPaths` prepend in `apps/minds/electron/backend.js:228-242`. Lima is bundled now, so the original symptom (Homebrew limactl lookup) is gone. Risk: lima provider may shell out to other CLIs (`ssh`, etc.) that rely on Homebrew PATH on some hosts.

- **status**: **still_needed** (no CI test; reverting would break docker users)
- **test path**: mngr candidate branch → launch-to-msg + ci.yml
- **deep-dive verdict (2026-06-05)**: `docker` on macOS lives at `/opt/homebrew/bin/docker` (Apple Silicon) or `/usr/local/bin/docker` (Intel) -- not `/usr/bin/`. Dropping the homebrew prepend breaks the docker provider for every minds user on macOS who selects Docker. launch-to-msg only exercises lima (bundled, absolute path) so it would pass while silently regressing docker users. CI can't verify safety here; the augmentation stays.

### laptop-agent-types-seed (MNGR)

Per slice 3, still load-bearing — no main-side fix for the cross-config `[agent_types.main]` mapping. Skip.

- **status**: still_needed (no action)

### create-agent-api-409-duplicate-name-guard (MNGR)

Per slice 3, branch's cross-host check is stricter than main's per-host check. Still meaningful at the API boundary while minds uses the hardcoded `system-services` agent name. Skip.

- **status**: still_needed (no action)

## Out-of-scope but flagged

- `libs/mngr_claude/changelog/wz-fix-claude-credentials-symlink.md` describes the now-removed PR #1869 bridge. Hygiene cleanup; not a test candidate.
- The base64-encoded Phase D cred-bridge mentioned in slice 3 lives in an out-of-date external worktree (`6917c024`), not pilot `fb96b1b3`. Already removed from pilot proper in `b506918`.

## Loop log

Per-iteration: timestamp, candidate, candidate branch / tag, launch-to-msg run id, ci.yml run id, outcome, action taken.

| # | candidate | branch / tag | launch-to-msg | ci.yml | outcome | action |
|---|---|---|---|---|---|---|
| 1 | todowrite-cleanup-pilot | pilot-rc-todowrite-cleanup / v0.2.36-rc1-todowrite-cleanup | 27006016613 success | 27005966487 success | green | ff-merged into pilot (749e234d), v0.2.35 tag unchanged |
| 2 | restore-supply-chain-cooldowns | mngr-rc-restore-cooldowns (PR #1936) | 27007931011 success (rerun after lima -15 flake) | 27007963943 success | green | cherry-picked to wz/minds_onboard (c930b05ed); PR #1936 closed |
| 3 | restore-stop-hook-enabled-when | pilot-rc-stop-hook / v0.2.36-rc2-stop-hook | 27010826323 success | 27010815083 success | green | ff-merged into pilot (c135679e), v0.2.35 tag unchanged |
| 4 | lima-2.0.3-to-2.1.1 | (no CI test) | n/a | n/a | still_needed | deep-dive showed 2.1.1 ships the exact regressed gvisor-tap-vsock 0.8.8; #5042 reference was misidentified; #4558 still open |
| 5 | homebrew-path-augmentation | (no CI test) | n/a | n/a | still_needed | docker on macOS lives at /opt/homebrew/bin/docker or /usr/local/bin/docker; dropping homebrew prepend silently breaks docker users; launch-to-msg only exercises lima so it would not catch the regression |
| 6 | revert-lima-start-new-default-timeout | mngr-rc-lima-timeout-revert (PR #1938) | 27013465788 success | 27013474227 success | green | cherry-picked to wz/minds_onboard (b7628677f); PR #1938 closed |
| 7 | uv-tool-install-editable-mode | pilot-rc-uv-tool-non-editable / v0.2.36-rc4-uv-tool-non-editable | 27014712370 success | 27014677300 success | green | ff-merged into pilot (cb2091cf); v0.2.35 tag unchanged |
