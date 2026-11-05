# Sentry issues are filed under our code again, not the stdlib

Sentry decides which stack frames are the application's own, derives an issue's culprit
from the last of them, and shows only those frames until a reader expands the rest. Left
to guess, it never counted our code as the application's in a shipped desktop build: the
build installs our code into a venv, and the SDK rules out anything under `site-packages`
as third-party.

What took its place depended on the process. The `mngr latchkey forward` daemon is
launched from the directory that also holds the bundled interpreter, so the SDK took the
standard library for the project and filed the daemon's issues under it -- `logging in
handle`, `threading in run`, `concurrent.futures._base in __get_result`. The minds backend
counted no frame at all, so its issues fell back to whichever frame happened to be deepest.
Either way our own frames were collapsed out of the default view.

The classification is now stated outright rather than inferred from the build layout:
frames under `imbue` are ours, frames in the standard library are not. This affects both
desktop-side Python processes that report to hosted Sentry. Because Sentry's grouping
weighs application frames, existing issues from both may regroup as new ones once a build
carrying this ships.
