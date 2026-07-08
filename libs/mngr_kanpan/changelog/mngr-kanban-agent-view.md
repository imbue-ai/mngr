Added agent-view style interaction to the kanpan board, so you can work with an agent without leaving it:

- Attach to the focused agent's session with `Enter`. The board suspends while attached and restores when you detach (`Ctrl-b d`), returning you to the board instead of a bare shell.

- Peek at an agent with `Space`: a live panel below the board shows the agent's recent output, refreshed every couple of seconds, with the board still visible above. The digest trims the agent's own input box and status line so it does not read as a second reply field. `Esc` closes the panel; to peek a different agent, close it, move on the board, and press `Space` again.

- Reply from the peek panel: type into the `reply>` input and press `Enter` to send a message to that agent (an empty reply does nothing). The send runs in the background so the panel stays live, and failures are shown inline. The sent text is echoed immediately (`sending: ...`) so it is not lost while delivery is in flight.

- The reply input supports readline-style editing: word movement (`Option`/`Ctrl`+`←`/`→`), word delete (`Option`/`Ctrl`+`Delete`, `Ctrl-W`), jump to start/end (`Ctrl-A`/`Ctrl-E`), and kill to start/end (`Ctrl-U`/`Ctrl-K`).

- Optional `peek_left_returns_to_board` setting (under `[plugins.kanpan]`, off by default): when on, pressing `←` on an empty reply closes the peek panel and returns to the board (mirrors Claude Agent View's back gesture).

- When the peeked agent is showing a selection menu (e.g. `/login`), the panel says `selection detected — esc, then enter to attach and choose`: menus are driven in the real session, since the text reply cannot move a selection cursor.
