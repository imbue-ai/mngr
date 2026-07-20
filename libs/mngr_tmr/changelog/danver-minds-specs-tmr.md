Added `mngr tmr-tasks`, a task-file sibling of `mngr tmr`: instead of discovering tasks via pytest collection, it reads a JSONL task file -- one packet per line with `schema_version`, `id`, optional `display_id` (used for agent/branch slugs), `kind`, and a free-form `context` object -- validates it up front (line-numbered errors, duplicate ids, unsupported schema versions), and fans out one agent per task through the same `run_mapreduce` framework (branch bundles, reducer, HTML report).

Mapper and reducer prompt templates are required (`--mapper-prompt` / `--reducer-prompt`; there are no packaged defaults because the task semantics live with the task-file producer). The mapper template renders with `task_id`, `kind`, `context_json` (the packet's context as pretty-printed JSON), `outcome_filename`, and `publish_snippet`; the reducer template reuses the integrator render context. Mapper agents must write the same `testing_agent_outcome.json` contract as `mngr tmr` mappers so the shared report can read it.

Extracted the TMR branch-bundle retrieval (bundle application, local-branch check, reducer-branch finalization, and the `integrator_branch` event) from `recipe.py` into a shared `branch_bundles.py` so both recipes use it instead of duplicating.

The report's `ChangeKind` gains `ADD_TEST` (counted as a non-impl change) for witness-test-writing runs.

The canonical producer is the minds behavioral-spec corpus: `minds specs plan --for-tmr` emits the task file, and `apps/minds/tmr/specs_mapper.j2` / `specs_reducer.j2` are the prompt pair (documented in the README's new "Task-file runs" section and wrapped by the root justfile recipe `tmr-minds-specs`).
