# Split `mngr destroy --force` into `--force` and `--yes`

`mngr destroy` now accepts a separate `-y` / `--yes` flag that just skips the
interactive confirmation prompt. The existing `-f` / `--force` flag continues
to bypass safety checks (it permits destroying a running agent and continues
past `AgentNotFoundError`) and now implies `--yes`, so scripts using
`--force` keep working unchanged.

Scripted callers that only need to suppress the prompt on a known
non-running agent can now pass `--yes` and keep the running-agent safety
check in place, instead of having to use `--force` (which is more
permissive than the script needs).
