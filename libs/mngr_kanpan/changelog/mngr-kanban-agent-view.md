Added agent-view style interaction to the kanpan board, so you can work with an agent without leaving it:

- Attach to the focused agent's session with `Enter` or `→`. The board suspends while attached and restores when you detach, returning you to the board instead of a bare shell.

- Peek at an agent with `Space`: a live panel below the board shows the agent's recent output, refreshed every couple of seconds, with the board still visible above. `↑`/`↓` switch the peeked agent and `Esc` closes the panel.

- Reply from the peek panel: type into the `reply>` input and press `Enter` to send a message to that agent. The send runs in the background so the panel stays live, and failures (e.g. sending to a stopped agent) are shown inline.
