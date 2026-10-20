Updated the minds TMR mapper prompt (`apps/minds/tmr/mapper.j2`) for the reworked escalation schema, keeping it in step with the packaged template it mirrors.

Escalation kinds now name the work rather than what happened to the reporting agent: `UNCAUGHT_BUG`, `FIX_DIRECTION_AMBIGUOUS`, `HARNESS_DEFECT`, and `SUITE_DUPLICATION` replace `BLOCKER` and `SHARED_PATTERN`. The minds-specific infrastructure guidance -- a Docker daemon and workspace snapshot, a deployed Modal env, or secrets being absent from the environment -- now sits under `HARNESS_DEFECT`.

Each escalation carries one `description_markdown` (first line a one-sentence summary) plus `locations` naming the paths it concerns, and its prose is capped at 120 words for a suite duplication and 250 otherwise. Agents are told not to withhold escalations to keep the list short: many agents reporting one problem is the signal that surfaces it.
