Analysis-only branch (no code changes).

Diagnosed the "Connection lost while pulling branch from agent" errors at the end of `mngr tmr` runs. Root cause is ordering in `libs/mngr_tmr/imbue/mngr_tmr/api.py`: `finalize_agent` stops the agent before `pull_agent_branch` runs, so the git fetch hits a torn-down Modal sandbox. Fix (not applied here) is to call `stop_agent_on_host` after `pull_agent_branch` at both call sites.
