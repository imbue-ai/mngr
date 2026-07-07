Added agent-view style interaction to the kanpan board, so you can work with an agent without leaving it:

- Attach to the focused agent's session with `Enter` or `→`. The board suspends while attached and restores when you detach (`Ctrl-b d`), returning you to the board instead of a bare shell.

- Peek at an agent with `Space`: a live panel below the board shows the agent's recent output, refreshed every couple of seconds, with the board still visible above. The digest trims the agent's own input box and status line so it does not read as a second reply field. `↑`/`↓` switch the peeked agent (the panel follows the selection) and `Esc` closes it.

- Reply from the peek panel: type into the `reply>` input and press `Enter` to send a message to that agent. The send runs in the background so the panel stays live, and failures are shown inline. A typed reply is discarded when you switch agents so it cannot be sent to the wrong one.

- The panel's footer states exactly what `Enter` does right now: `enter/→ attach` when the reply is empty, `enter send` once you type.

- When the peeked agent is showing a selection menu (e.g. `/login`), the panel says `selection detected — press → to attach and choose`: menus are driven in the real session, since the text reply cannot move a selection cursor.
