The board's `Refreshed <n> ago` stamp now reports the age of what is actually on screen.

Coming back from an attach used to cost a whole refresh interval. The attach blocks the board's
event loop for as long as the session is open, and on return the board asks for a local read;
the periodic full refresh then came due against that read and was dropped rather than held, so
the remote columns and the stamp stayed on the last full refresh for up to another interval. A
tick displaced by a local read is now owed, and runs as soon as that read finishes. A tick that
lands while a full refresh is already running is still skipped, as before.

A refresh that failed used to reset the stamp to `Refreshed just now` even though the board was
still showing the previous fetch. Only a refresh that landed renews it now, so a board whose
fetches are failing reports how old its rows really are. When it is the very first fetch that
fails there are no rows to age and no stamp to show, so the footer reports the failure itself.
The failure retry keeps its cooldown, which now counts from the attempt rather than from the
stamp.

The board also measures the terminal again when an attach returns. A terminal narrowed while the
board was suspended reached no resize handler, so the board came back drawn to its old width: the
footer belt ran past the right edge onto the line below, which scrolled the screen once per
repaint and left a copy of the belt behind each time -- a stack of `Refreshing` rows growing for
as long as the refresh ran.

A command that repaints the board (`refresh_afterwards`) holds its outcome until the repaint
lands. If another notification claimed the footer in the meantime -- muting an agent, say -- the
held outcome used to overwrite it, answering the keypress just made with the result of an earlier
command against an agent the user had not touched. The outcome now waits for that notification to
finish and reports after it, so neither message is lost. The command's in-progress label is
retired as soon as it finishes, rather than spinning until its outcome reaches the footer.
