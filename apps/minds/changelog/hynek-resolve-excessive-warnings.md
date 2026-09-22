# No more "supervisor has died" warning floods on startup

Starting Minds no longer logs dozens of `Could not list pending permission
requests from the gateway: The `mngr latchkey forward` supervisor we spawned
has died before binding its gateway port` warnings in the first seconds of a
startup that then works fine.

Minds terminates and respawns the `mngr latchkey forward` supervisor on every
start, and the terminated forward's on-disk record -- bound gateway port and
all -- survives until the new forward claims the latchkey directory an `mngr`
cold start later. The gateway client read that record directly and treated
"the forward it names is not the one holding the directory" as fatal, so every
gateway read landing in the restart window failed at once; the chrome publisher
reads pending requests twice per tick, which turned the window into a burst of
warnings.

The client now waits on the record published by the *live* forward, so an
unowned latchkey directory is waited out like any other not-yet-bound state and
only the existing 30s deadline is fatal (its message now names the supervisor
log to check).
