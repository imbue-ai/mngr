Added `specs/minds-auth-witnesses/audit.md`, recording the Phase 1 witness audit of the
minds authentication behavioral-spec corpus: the before/after coverage table, the per-unit
witness map, and the finding that the workspace-bridge units are witnessed only in
`libs/mngr_forward` (which the default minds spec matrix does not scan).

Added `specs/minds-auth-witnesses/reflection.md`, the Phase 3 reflection: the quality of
the (hand-generated) witnesses, that witnessing surfaced no implementation bugs, and the
tmr-specs machinery findings from two Modal fleet runs and a local pilot — chiefly that
the mapper prompt's `uv run mngr specs matrix` coverage command hangs on the Modal host
(stalling every agent) and the `local` provider is unusable headless due to Claude Code's
per-directory trust dialog. Also gitignores the `tmr-specs-*_<timestamp>/` run output dirs.
