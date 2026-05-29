`mngr message` (alias `mngr msg`) now supports an `-a` / `--all` / `--all-agents`
flag to send a message to every agent at once, matching the tutorial and the
`--all` convention used by other commands. Previously the only way to message
all agents was to pipe `mngr list --ids` into `mngr msg -`.
