# Consistent agent address resolution across single-agent subcommands

Refactored how single-agent subcommands turn an `AgentAddress` into the live
interfaces they operate on. The "find" stage (discovery + matching against
the address) is now strictly separate from the "ensure live" stage (bringing
the host online, looking up the live agent, optionally starting it).

Two new helpers in `imbue.mngr.api.find` replace the previous
`is_start_desired` / `skip_agent_state_check` flags on
`find_one_agent` / `find_agent_for_command`:

- `resolve_to_started_host_and_agent`: bring the host online and resolve
  the agent ref to an `AgentInterface` without checking the agent's
  lifecycle state. Used by `push`, `pull`, `provision`, and `rename`.
- `resolve_to_started_host_and_running_agent`: as above, but also
  require / auto-start the agent process. Used by `connect` and `capture`.

Both helpers take a single `allow_auto_start` flag (driven by `--start`).

User-visible changes:

- `push`, `pull`, and `provision` no longer require the agent to be
  running. Previously they failed when targeting a stopped agent on an
  online host; now they operate on stopped agents directly.
- `push`, `pull`, `provision`, and `rename` gain a `--start/--no-start`
  flag (default `--start`) that controls whether an offline host is
  started automatically.
- The `--start` help text on `connect`, `capture`, and `exec` has been
  reworded to reflect what `--start` actually starts in each command.
- `mngr connect` no longer falls back to "most recently created agent"
  when run non-interactively without an explicit agent. It now matches
  every other single-agent command: pass an agent name, or run it from
  an interactive terminal to use the selector.
- Cancelling the interactive agent selector now exits cleanly via
  `click.Abort` instead of printing nothing and returning silently.
