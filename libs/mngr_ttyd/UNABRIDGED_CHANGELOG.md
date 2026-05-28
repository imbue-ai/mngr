# Unabridged Changelog - mngr_ttyd

Full, unedited changelog entries for the `mngr_ttyd` project, consolidated nightly from individual files in `libs/mngr_ttyd/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-05-26

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

Switched `resources/ttyd_agent.sh` to use exact-session matching when attaching
to a named agent via URL arg. The previous `tmux attach -t "$_SESSION:0"` form could
silently route to a sibling session whose name starts with the requested one, e.g.
attaching by name `gemini` when `mngr-gemini` is gone but `mngr-gemini-foo` is alive
would land the browser ttyd window on the wrong agent. The script now passes
`=$_SESSION:0` so tmux refuses to misroute.

To prevent recurrences, adopted the `PREVENT_BARE_TMUX_TARGETS` ratchet rule
(added in `imbue_common`) via `rc.check_bare_tmux_targets(_DIR, snapshot(0))` in
this project's `test_ratchets.py`. The ratchet flags new occurrences of
`tmux <subcmd> -t '<bare-name>'` -- targets without a leading `=` exact-match
prefix, which can silently route commands to a sibling session whose name shares
a prefix with the intended one. The adopting test starts at a baseline of zero
violations.

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.
