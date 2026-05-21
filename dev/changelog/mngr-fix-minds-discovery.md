Add `specs/minds-env-activate-split/concise.md`: design for splitting
`minds env activate` into a default use-mode (no `MODAL_PROFILE`) and an
opt-in `--deploy` mode. Fixes the spurious Modal-discovery warnings and
Latchkey breakage hit by users who activated `staging` only to *use* the
deployed tier but had no Modal token for the `minds-staging` workspace.
