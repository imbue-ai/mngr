Added agent-view style interaction to the kanpan board, so you can work with an agent without leaving it:

- Attach to the focused agent's session with `Enter`. The board suspends while attached and restores when you detach (`Ctrl-b d`), returning you to the board instead of a bare shell. On attach the screen clears to a brief `Connecting to <agent>...` line rather than flashing the shell behind the board.

- Peek at an agent with `Space`: a live panel below the board shows the agent's most recent user/assistant messages (via `mngr transcript`), refreshed every couple of seconds, with the board still visible above. Tool events are not shown, so the peek reads like the conversation; tool-only turns (which have no text) are skipped to reach the last real message. If the recent window has none -- e.g. an agent deep in a run of tool calls -- the panel says `(no recent messages -- attach to watch)`. It shows the newest lines, so a long final message renders its end under a `N earlier lines hidden` marker, rather than being cut off at the top or mirroring the agent's scrolled-up screen. `Esc` closes the panel; to peek a different agent, close it, move on the board, and press `Space` again.

- Reply from the peek panel: type into the `reply>` input and press `Enter` to send a message to that agent (an empty reply does nothing). The send is fire-and-forget -- `mngr message` waits up to ~90s for the agent's submission signal, which a busy agent cannot give until its current turn ends, so the reply is not blocked on that confirmation. The reply shows up in the panel body once the agent submits it, and several replies typed in a row are delivered in order.

- The reply input supports readline-style editing (via the `urwid_readline` library, with the Option/Ctrl+arrow chords added): word movement (`Option`/`Ctrl`+`←`/`→`, `Meta-B`/`F`), word delete (`Option`+`Delete`, `Ctrl-W`, `Meta-D`), jump to start/end (`Ctrl-A`/`Ctrl-E`), and kill to start/end (`Ctrl-U`/`Ctrl-K`).

- Optional `peek_left_returns_to_board` setting (under `[plugins.kanpan]`, off by default): when on, pressing `←` on an empty reply closes the peek panel and returns to the board (mirrors Claude Agent View's back gesture). `←` still moves the cursor when the reply has text.

- Selection menus (e.g. `/login`) are not part of the transcript, so they do not appear in the peek; attach (`Enter`) to make the choice in the real session, since the text reply cannot move a selection cursor.

- The board no longer captures the mouse, so your terminal's own click-drag text selection and copy work normally on the board (previously the board grabbed mouse events, which blocked native selection). The board has no mouse actions of its own.
