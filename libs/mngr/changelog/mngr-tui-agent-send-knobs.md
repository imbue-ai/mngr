Every interactive TUI agent type now declares how its input box looks to `mngr message`, so a TUI that differs from Claude Code's gets the right send behaviour instead of Claude's.

- `PASTE_PLACEHOLDER_PREFIXES` names the text the input box shows in place of a pasted message (for example `[Pasted text #N +M lines]`), so a paste counts as visible before its text is on screen; `()` declares a TUI that shows the pasted text. A concrete `InteractiveTuiAgent` subclass that does not declare it, or declares anything but a tuple of non-empty strings, fails when the class is defined.

- Every concrete `InteractiveTuiAgent` subclass implements `_build_enter_retry_gate`, the pane test that decides whether a swallowed Enter is re-sent during the confirmation window. `tui_utils.build_unsubmitted_message_retry_gate` builds the test Claude Code and agy use: retry while the pane still shows the message's tail or a paste placeholder, which suits a TUI where an Enter on an empty input box does nothing. A TUI that keeps an accepted message on screen as a queued entry supplies its own, because there that test would press Enter again on a message the TUI already took.

- For plugins that call the send helpers directly: `tui_utils.submit_message_and_confirm` and `build_confirmation_command` take `paste_placeholder_prefixes` and `enter_retry_gate` (None re-sends Enter at every retry offset without reading the pane).

Behaviour for every existing agent type is unchanged.
