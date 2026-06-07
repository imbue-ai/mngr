Fixed duplicated paragraphs in `mngr robinhood`'s live streaming output (`--stream-plain-text` and `--include-partial-messages`).

- When Claude's TUI reflowed already-rendered text as later text streamed in -- most visibly collapsing a blank line around a markdown horizontal rule (`---`) as the following paragraph arrived -- the stream-buffer body was no longer a clean prefix-extension of what had already been emitted. The delta computation then re-emitted everything past the (character-level) divergence point, and because plain-text output cannot be unprinted, the already-printed region appeared a second time.
- `compute_stream_delta`'s divergence branch now recognizes already-emitted content across whitespace reflow (treating whitespace runs as equivalent and absorbing collapsed/added blank lines), so only genuinely new content is emitted. At worst a little already-printed whitespace is left stale; no visible content is duplicated.

Added tmux window-sizing flags to `mngr robinhood`: `--tmux-width`, `--tmux-height`, and `--tmux-window-size` (`manual|latest|largest|smallest`).

- The spawned agent's tmux window now defaults to a large, pinned size (`2048` columns x `256` rows, `manual`) so the live-streamed response -- reverse-mapped from the rendered tmux pane -- is no longer chopped into hard line wraps at a narrow pane width.
- All three flags are consumed by the wrapper (not forwarded to claude); invalid values exit with code 2.
