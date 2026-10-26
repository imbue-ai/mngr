Live agents no longer read as STOPPED or DONE when their host is too loaded to answer in time. Chats used to flip to paused and "come back" a few minutes later, and a message sent in that window could restart a running agent.

- **Before:** each agent ran its own tmux and `ps` probe, one after another, under the host's shared 20s read budget. Once the budget ran out, every later probe was capped at 1s. A probe killed at that cap read as STOPPED (no tmux answer) or DONE (no `ps` answer).

- **Now:** a listing reads the host's tmux panes (`tmux list-panes -a`) and then its process table (`ps -e`) once per host, each read with its own 5s timeout. A single-agent probe uses the same two reads. A host with 8 agents and a 1.5s tmux call now lists in about 2s instead of about 25s, with every agent correct.

- **When the tmux read fails or times out, agents read UNKNOWN,** never STOPPED or DONE. When only the `ps` read fails, tmux still settles what it can show on its own: a missing session reads STOPPED, a dead pane DONE, and a pane whose foreground is the agent's own process RUNNING or WAITING (without a PID); anything else reads UNKNOWN. A host with no tmux server (or no tmux installed) still reads its agents STOPPED. `mngr observe` emits UNKNOWN as is and keeps watching the agent's process, as it also does for a RUNNING or WAITING reading without a PID. The remote providers' batched listing (modal, vps, imbue_cloud) is unchanged: it already read once per host, and a tmux or `ps` failure inside it reads as it did before (a missing tmux answer still reads STOPPED).

- **How commands treat an UNKNOWN agent:**

  - `mngr message`, and a chat send, fail with a "could not determine whether the agent is running" error of the new kind `state_unknown`, instead of delivering, starting or reviving. Trying again helps once the host answers; a restart (`mngr start --restart`) does not depend on reading the state.

  - `mngr destroy` without `--force` refuses.

  - `mngr archive` treats the agent as running: it skips it, or with `--force` stops it before archiving it.

  - `mngr exec`, `mngr connect`, `mngr capture` and `mngr create --reuse` go ahead as before: when they may start an agent they run the start, which leaves a running agent's session untouched, and otherwise they proceed without one. A start whose `tmux has-session` check fails without saying the session is absent (e.g. a lost tmux server) now fails, instead of reading as "no session" and killing the agent's processes before relaunching it.

- **A laptop-side `mngr list` of a remote host (modal, vps, imbue_cloud) no longer reads live codex, antigravity, pi or opencode agents as DONE.** That listing knows only each agent's type, and looked its process name up in a table that only had claude. For other types it fell back to the first word of the launch command (for codex, `(`), which never runs. Agent classes now declare a `fixed_process_name`, which both the listing and a loaded agent read. Agent types built on claude's agent class under their own type name (code-guardian, fixme-fairy, witness) inherit `claude`, so that listing also stops misreading them as REPLACED or DONE. An agent type with no declared name now gets the same fallback in that listing as when loaded: the command's first word past any leading subshell `(`.
