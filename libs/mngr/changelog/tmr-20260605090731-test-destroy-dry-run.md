Corrected the "DESTROYING AGENTS" section of the mega tutorial: the
`mngr list --ids | mngr destroy - --dry-run` example referenced a `--dry-run`
flag that was removed from `mngr destroy` (and the other multi-target commands).
The tutorial now shows how to preview what would be destroyed by running
`mngr destroy my-task` without `--force` and answering "no" at the confirmation
prompt, which lists the targets without destroying anything.

Also fixed the corresponding e2e tutorial test (`test_destroy_dry_run`) so it
exercises this confirmation-preview behavior and gives the agent-creation step a
realistic timeout.
