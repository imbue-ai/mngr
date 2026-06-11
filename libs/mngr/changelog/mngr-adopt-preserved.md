Updated the auto-generated `mngr create` docs for the `--adopt-session` option: a bare session ID is now searched in the current and user-scope Claude config dirs, every live local mngr agent, and preserved sessions from destroyed agents.

Internal: added `get_agents_root_dir(host_dir)` as the single source of truth for the agents-state root directory, and consolidated the previously hand-written `host_dir / "agents"` path constructions in `host.py` to use it (and `get_agent_state_dir_path`). No behavior change.
