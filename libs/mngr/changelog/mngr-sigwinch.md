Changed: agents now repaint on *every* tmux attach, not just on `mngr connect`. The post-attach `SIGWINCH` redraw nudge moved from `mngr connect`'s SSH wrapper into a per-session tmux `client-attached` hook set when the agent is created, so a plain `tmux attach`, the ttyd agent terminal, or a web-shell attach all trigger a clean redraw too.

This resolves the garbled-pane / failed-send symptom seen when a client attaches at a different terminal size than the agent's session was created at: previously the agent's TUI could be left showing a stale, unpainted frame (which also broke message sending, since the paste-visibility check could not read the corrupted pane).

Note: only newly created agents get the hook. Agents already running when you upgrade will not get the repaint nudge on attach (the old `mngr connect` nudge has been removed) until they are recreated.
