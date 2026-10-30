Gave every e2e command that reaches a remote provider one shared timeout budget,
sized from the per-test cap it has to fit inside.

The e2e harness runs each command under a time limit, defaulting to one sized for
local commands. Commands that provision a container, snapshot a host or list a
provider are network round-trips and need far longer. Fifty-one of them never said
so, running against the 30s local default, while the rest carried hand-written
budgets of 60, 90, 120, 180 or 240 seconds for the same operation -- raised a site
at a time as individual tests went red. Whichever remote command was slowest on a
given run was the one that failed, so a single cause read as unrelated flakiness.

All of them now use `REMOTE_COMMAND_TIMEOUT_SECONDS`, which replaces three
identical copies of the old budget declared separately in three test modules. It
is derived from the release marker class's own per-test cap rather than written
out, so a command's budget can no longer quietly exceed the time its test is
allowed in total. Measured across the 2026-10-22 and 2026-10-28 release runs
(1246 observations), this class of test has a p99 of 92s, which the lowest of the
old budgets did not clear.

Four commands keep a budget of their own, each declaring a matching per-test
timeout where it needs one: a custom image build, a create that waits for the
agent to be fully ready, one already bounded to a second by `time_bounded`, and
one create short-circuited before it provisions anything. The genuinely local
commands keep the local default.
