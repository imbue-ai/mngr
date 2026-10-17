# Laptop sleep and lost connectivity

mngr usually runs on a laptop, and most of what it does happens over SSH connections to machines elsewhere. A laptop sleeps, wakes for a minute, loses its network, and comes back on another one, and every long-lived connection, timeout, and liveness check in mngr has to survive that. This page collects what is known about how those conditions behave, so that new code does not have to rediscover it. Read it before adding a timeout, a retry, or a check that decides something is stuck.

## Clocks

- `time.monotonic()` stops while the machine is asleep. On macOS CPython implements it with `mach_absolute_time()`, which does not advance during sleep; on Linux it is `CLOCK_MONOTONIC`, which excludes suspended time. Two measured 15-minute sleeps advanced it by 1-3 seconds.
- `time.time()` keeps counting through a sleep.
- So a deadline measured on the monotonic clock is stretched by a sleep rather than spent by it: it cannot expire just because the lid was closed. A deadline measured on the wall clock expires at the first moment after the wake that anything checks it.
- The difference between the two clocks is how long the machine was suspended. `imbue.imbue_common.suspension` turns that into a question you can ask (`read_clocks()` now, `was_suspended_since(reading)` later) without a background thread.
- paramiko measures its timeouts on the wall clock. A channel read blocked across a sleep longer than its timeout fails the first time anything wakes it after the wake, even when the packet that woke it has already arrived; opening a channel behaves the same way. A read interrupted partway through an SFTP reply surfaces as `SFTPError: Garbage packet received`, which mngr treats as a broken connection and retries.

## Dark wakes

A laptop left asleep does not stay asleep. With the lid shut on AC power, macOS alternates roughly 15 minutes of sleep with a 45-180 second "DarkWake" in which processes run and the monotonic clock advances, and a DarkWake has been seen to last 73 minutes. The network may or may not be there during one: a laptop carried in a bag wakes into no network at all. Electron's `resume` event does not fire for a DarkWake.

This matters for anything that counts awake time. A monotonic budget is not spent by the sleeps, but it is spent a minute at a time by the DarkWakes between them, so "300 seconds" can run out an hour of wall-clock time after the laptop was closed, in the middle of a night nobody was watching.

## Connections that outlive a sleep

A sleep kills every TCP connection the laptop holds, but nothing tells the process so:

- The socket, and a paramiko transport on it, keeps reporting itself alive.
- paramiko's keepalive writes a packet but never waits for a reply. Where the peer's reset can reach the laptop, the next write surfaces the death; behind a NAT whose mapping the sleep dropped, nothing ever comes back.
- The kernel gives up on its own only through its retransmission timeout, about 48 seconds after the wake on macOS, and never for a socket with nothing in flight.

Meanwhile a new connection made after the wake works at once. So the right response to a sleep is to throw away every connection made before it and reconnect, not to wait for something to time out.

mngr does this in `SuspensionWatchdog` (`imbue/mngr/utils/suspension_watchdog.py`). Every SSH transport `OuterHost` opens registers with the `MngrContext`'s watchdog, which checks every 5 seconds of awake time and closes any transport established before a suspension. Closing it wakes every channel blocked on it, and mngr's transient-SSH retry reconnects. `mngr forward`'s tunnels do the same for their own connections (`SSHTunnelManager` rebuilds a tunnel whose connection predates a suspension).

## What this means for new code

- To tell that a sleep happened, ask the clocks (`was_suspended_since`), or let the watchdog handle the connection. Do not infer it from how long something took.
- Prefer a signal about state (an error, an exit, a closed connection, a suspension) to a timer that guesses from elapsed time. A timer cannot tell "hung" from "slow" or from "waiting out an outage", and those call for different responses.
- When a timer is unavoidable, measure it on the monotonic clock, expect it to be stretched by sleeps and to accumulate across DarkWakes, and bound it by silence (no bytes for N seconds) rather than by total duration where the operation's length depends on data size or bandwidth.
- A failure that arrives just after a wake is usually a connection the sleep killed. Retry it on a fresh connection rather than reporting it.

## Checking behaviour on a real laptop

None of this can be checked in CI. `scripts/sleep_wake_drill.py` stages a sleep with dead connections against running apps, and `scripts/sleep_wake_probe.py` measures the platform facts above (which clocks stop, what releases a blocked SSH read). See `apps/minds/docs/sleep-wake-drill.md` for how to run them, and `blueprint/environment-signals/plan-environment-signals.md` for the field measurements this page draws on.
