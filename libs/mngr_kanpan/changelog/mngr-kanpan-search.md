Added `/` search to the kanpan board, for finding a row on a board with too many agents to scan.

Pressing `/` opens a prompt in the footer; as you type, the selection jumps to the best matching row. The query is matched case-insensitively against every visible column, so you can jump by agent name, PR number, CI status, repo, or any custom column -- `/kanpan` by name, `/#2531` by PR. Matches rank by name prefix, then name substring, then any other cell, with ties keeping board order.

`↑`/`↓` step through matches, `Enter` closes the prompt leaving the match selected, and `Esc` returns to the row you started from -- or to no selection at all, if a refresh has since taken that row off the board. Backspace retraces what you typed: erasing the query rewinds the selection and leaves the prompt open, and the next backspace erases the `/` and cancels. `Ctrl-U` kills back to the start of the query, for a retype without leaving. Clicking a row closes the prompt and selects what you clicked. `Enter` only selects; attaching stays a separate keystroke afterwards.

The prompt opens in the footer's status slot, in place of the refresh stamp rather than as an extra row, so the board does not shift when it opens. The belt beside it carries the match count (`2/6`, or `no match`) and the prompt's keys in place of the board's, which cannot fire until the prompt closes.

The board is never filtered or reordered -- search only moves the selection, so nothing is hidden and no state is left behind when the prompt closes.

`/` is a builtin command, so it appears in the footer and the `?` overlay, and like any builtin it can be overridden by a custom command on the same key or disabled via `[plugins.kanpan.commands]`.

The footer legend no longer clips mid-binding on a narrow terminal, where half of `r: refresh` used to render as `resh`. Bindings are dropped whole, from the left, and re-fitted whenever the terminal is resized or the status text beside them changes width. `?: more keys` is last to go, being the way to find whatever was dropped.

`Ctrl-T` (readline's transpose) no longer takes the board down. It crashed on an empty input, and scrambled a short one instead of transposing, so it is unbound in both the search prompt and the peek reply input.
