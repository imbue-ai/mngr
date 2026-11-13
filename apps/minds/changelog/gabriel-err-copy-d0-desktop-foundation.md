Shared pieces for plainer error messages in the desktop app, which later changes adopt as they rewrite their messages (no message changes here):

- An error display: a tinted box holding a red alert mark, our sentence beside it in ordinary black text, a grey "See details" trailing that sentence which opens the status, server text or output in place, and then the buttons that fit the error. Amber in place of red for a condition the user fixes themselves or that clears on its own, and amber with a quiet "Still trying..." while the app is retrying on its own, since nothing has failed yet. The box can carry longer content such as steps or a list in place of the sentence, and an error drawn inside another error's box (why an "Ask the agent to fix it" couldn't start, under that error's buttons) takes no tint of its own, so one failure reads as one box. It comes as an in-place notice and as a styled dialog that any screen can raise in place of a native alert.

- An error's buttons are the fixes for it: the first is its main one, drawn as the primary button, unless the sentence asks the user to do something and that button only confirms it afterwards (Retry, Try again, Check again), in which case it waits as a secondary. A way out (Dismiss, Discard) follows the fixes as a ghost and is never the main button, and "Report a bug" comes last, a ghost beside other buttons and a button of its own when it is alone. A button can also be drawn in the danger color (a Deny), show a spinner while it runs, or be shown disabled with a tooltip saying why. They are drawn smaller than the surface's own buttons, which stay in the surface's own footer below the box.

- Error sentences are written in a small markup the app renders: a command in `backticks` is set as code, and *the ask* -- the instruction the user is to follow -- is set apart in semibold italic, so it stands out from the generic "Try again. If this persists, report a bug." around it. A bug report, a native alert and the log carry the sentence with the markup taken off.

- An inline error for what is too small to be a notice -- under a field, or in a card's meta line -- keeps red text at helper size with the same mark.

- "Report a bug" on an error opens the bug-report form with the error's sentence and details attached; the form shows them under "Details attached to this report", and the report sent to Imbue carries them. If the user switches the form to having an agent help instead, they show under "Details the agent will see" and go into the agent's chat.

- When the styled dialog cannot be shown, the native alert fallback gives the sentence plus "See the log for details." and writes the details to the log.

- A fallback for a failed request with no usable reason ("<what failed>. Try again. If this persists, report a bug.", or "Imbue Studio didn't respond. Try again in a moment." when no response came back); every use states whether the user can try again, and one that can't drops "Try again", keeping the raw response for the details.

- An "Ask the agent" button that starts a chat in a workspace seeded with a description, through the same route as the help page's "Start agent". It can sit among an error's buttons, with a failed start shown under them. When the workspace refuses the start (for example, it has no agent-assist skill), the button is withdrawn and the refusal shows without "Report a bug".

- One set of remedies (restart Imbue Studio, update Imbue Studio, ask the agent) that a route can name beside its error and the app draws as the same button on every screen, whether the route named it or the screen picked it for an error it words itself.

- An "Update Imbue Studio" button, for an error that refuses this version as too old, that opens the app's Updates settings, and on macOS a "Reopen Imbue Studio" button that quits the app through its usual quit prompt and starts it again, for a fix (like a Full Disk Access grant) that applies only after a relaunch.

- A rule for a panel's own buttons beside an error on the same panel: while the error leads its row with a main button, the panel's primary button is drawn secondary, so the error's fix is the one dark button there.

- Every error dialog is titled by what failed ("Couldn't start \"alpha\""), with the mark on that title and the sentence flush under it saying why and what to do -- and that sentence drops a lead the title already says, so the dialog never says the same thing twice. A caller with no title of its own gets "Hit a snag" rather than an untitled dialog. The dialog's x is the only way to dismiss it -- there is no Close button beside it, and a dialog with nothing to press has no button row at all.
