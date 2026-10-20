Replying to an agent from the kanpan peek panel now refreshes the board once the reply is delivered.

Accepting a reply puts a `WAITING` agent back to work, but the board had no refresh trigger for that path, so the row kept rendering its pre-reply `STATE` until the next manual `r` or the 10-minute timer. Answering the question an agent was waiting on left it reading `WAITING` long after it had resumed. A failed send still only drops its optimistic echo and shows the error.

Every action that repaints the board now goes through one request, and a request made while a refresh is already running is held rather than dropped. The fetch in flight was started before the action, so it cannot show what the action changed; a second one now runs as soon as it lands. Previously such a request was lost outright, so push, delete, attach, a `refresh_afterwards` command, or a reply could all leave the board showing pre-action state until the next manual `r`. An outcome message held for its repaint waits for that second refresh too, so the message and the rows it describes still arrive together.

The periodic full refresh no longer stops for the rest of the session when its timer happens to fire while another refresh is in flight. Only a full refresh re-armed that timer on completion, so a firing that landed on a local refresh previously ended the chain and left the board with no periodic refresh at all; the interval is now skipped and the timer resumes on the next one.

The local refresh -- the one that already runs after push, delete, attach and `refresh_afterwards` commands -- now also runs on a timer, every 30 seconds by default. `STATE`, `commits_ahead` and label columns therefore describe the fleet as it is, rather than as the last full refresh found it up to ten minutes earlier. This changes what an untouched board costs. It already read the agent list on a timer -- the ten-minute full refresh does -- but at roughly a second per few dozen agents, doing so every thirty seconds instead spends a low double-digit percentage of an idle board's time on it rather than about one percent.

```toml
[plugins.kanpan]
# Shorten it to watch a count move in close to real time, or set 0 to refresh
# only in response to an action, as before.
local_refresh_interval_seconds = 5.0
```

That makes a header count like `Running: {state == "RUNNING"}` track reality rather than the last ten-minute cycle. Remote sources sit these refreshes out, so PR and CI keep the full refresh's cadence; a count over those columns keeps it too, and so does the `Errors:` block, which would otherwise lose whatever a remote source reported on the first tick after it landed.

Zero is how the timer is turned off, since TOML has no null and a setting that is on by default needs a value that means off. It arms no alarm at all; a negative interval is refused.

An interval of zero or less on `refresh_interval_seconds`, which has no such off switch, is refused at startup. Both timers re-arm when they fire, so an alarm that is always due leaves the event loop no idle moment to repaint in: the board would peg a core and freeze rather than merely refresh too often. The bound declared on the field cannot catch this on its own, because plugin config is built without validation, so the board checks both intervals itself before it takes the screen.

The interval is a floor rather than a promise. A tick that lands while the previous refresh is still running is skipped rather than queued, so an interval shorter than a refresh takes settles into back-to-back refreshes instead of a growing backlog, and a slow data source stretches the effective period instead of piling work up. These refreshes use their own worker, so a slow one delays neither the periodic full refresh nor an action's repaint.

A tick that goes wrong leaves the board exactly as it was -- no error row, no spinner, nothing at all -- because at a few seconds an interval anything else would be noise. Trouble that persists still reaches the board within `refresh_interval_seconds`, since the full refresh does report the fetch errors it runs into.
