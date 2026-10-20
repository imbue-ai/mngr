Fixed mngr leaving a claude agent permanently stuck in Claude Code's shell mode after a bare `!`.

Claude draws a dim hint inside an empty input box: `Try "how do I log an error?"` on a fresh
agent, `Press up to edit queued messages` while a message is queued, or the model's suggested
next prompt in normal mode. mngr read the hint as typed text, so it mistook the stranded
shell-mode box for a human's unsubmitted command and refused to touch it.

mngr now reads Claude's pane with its styling and drops the input box's dim hint text before
deciding what is typed there. The strand is recovered whichever hint is showing, and the
pre-send check no longer warns about a hint as leftover text. A human's draft is never taken
for a hint, even one that reads `Try "how do I log an error?"` word for word.

With colour suppressed for the agent (`NO_COLOR`, `FORCE_COLOR=0`, `TERM=dumb`), the hint is
not dim and reads as typed text: mngr refuses the send rather than backspace, and the pre-send
check warns about it.

A new test fails when the claude version `dialogs.py` was read from differs from the one the
mngr Dockerfile installs, so the binary drift checks cannot silently skip in CI.
