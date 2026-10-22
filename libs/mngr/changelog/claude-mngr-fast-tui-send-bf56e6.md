Sending a message to an interactive TUI agent spawns far fewer processes. Each tmux call is a process spawn, and under gVisor (Imbue Cloud workspaces) a spawn costs roughly 70-100 ms, so a send to Claude Code went from 16 spawns to 6 (measured against a local fake Claude TUI):

- The agent's pane ID is looked up once per send and every step addresses that pane, instead of being looked up again by each of the readiness check, the leftover-input check, the typing, the paste wait, and the submit.

- A preflight that already read the pane (Claude's dialog check) hands that reading on, so the readiness check and the leftover-input check no longer capture the same screen again moments later.

- Leaving copy-mode rides in the same tmux invocation as the typing (and, for long messages, as the buffer load and paste). It runs only when the pane is actually in a mode, so tmux versions older than 3.2, which lack `copy-mode -q`, still type the message rather than rejecting the whole invocation.

- Waiting for the typed message to show, taking the evidence baselines, pressing Enter and confirming now run as one host script. A message that never shows still fails with "Timeout waiting for pasted content to appear" and Enter is never sent.

- After Enter, evidence is polled every 0.1 s for the first one to two seconds (then every 0.5 s as before), and the script measures time with bash's `SECONDS` instead of running `date` on every poll.

The pane check that gates Enter retries now normalizes the pane byte-wise under the C locale, so it no longer depends on the host's locale.
