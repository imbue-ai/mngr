Added the root justfile recipe `tmr-minds-specs`, the canonical spec-witnessing TMR variant: it generates the task file with `minds specs plan --for-tmr` and runs `mngr tmr-tasks` with the minds spec-witnessing prompt pair (`apps/minds/tmr/specs_mapper.j2` / `specs_reducer.j2`) under the `tmr-minds-specs` variant name. Extra map-reduce flags pass through (e.g. `just tmr-minds-specs --provider modal --max-parallel-agents 8`).

Updated the `minds-behavioral-specs` skill's tooling section to list the new `minds specs` subcommands (`export`, `plan --for-tmr`, `check-witnesses`).
