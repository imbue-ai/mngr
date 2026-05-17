- mngr: hold the host's cooperative lock across `start_agents()` calls so the
  idle shutdown script cannot trigger mid-start and corrupt the new tmux
  session. Three previously-unprotected sites now acquire the lock:
  `provision_agent`'s post-provision restart (in `api/provision.py`),
  `ensure_agent_started` for the find/resume flow (in `api/find.py`), and the
  user-facing `mngr start` command (in `cli/start.py`). Closes #1095.
