# Checking sleep/wake handling on a real laptop

A laptop sleep kills every connection Minds holds to a workspace. How the app
behaves in the minutes after the wake cannot be checked in CI: it depends on
real clocks stopping, real sockets going half-open, and a real network that may
or may not deliver the peer's reset. Two scripts in `scripts/` cover this on a
spare Mac:

- `sleep_wake_drill.py` stages the incident against running Minds apps and
  reports what each app did. This is the one to run after changing the
  forward's tunnels, the health tracker's thresholds, the recovery dispatch, or
  the sleep tracker.
- `sleep_wake_probe.py` measures the platform facts that work relies on
  (which clocks stop, what releases a blocked SSH read, how long a half-open
  tunnel stalls). Run it when a drill result does not make sense.

The design behind both is in
`blueprint/environment-signals/plan-environment-signals.md`.

**Warning:** the drill drops packets to real workspace addresses and puts the
laptop to sleep. Run it on a spare machine against spare workspaces, never on
the laptop you are working from.

## What you need

- A Mac you can leave alone for about fifteen minutes, with `sudo` access. The
  drill takes the password once, up front, and holds root for the run.
- One or more Minds apps running on it, each signed in, each with a window open
  on a workspace for the whole run. The health probe loop only polls a
  workspace that a failed request has enrolled, and those requests come from an
  open window. A workspace nobody is looking at is never probed, never
  convicted, and measures nothing.
- To compare a change against main, run two apps side by side: the build under
  test on one data directory and a main build on another (for example `~/.minds`
  and `~/.minds-staging`). The drill finds every running app in the process
  table and drills them together on one sleep, so both see the same outage.
  Two separate runs could not answer the same question, since they would be two
  different networks and two different sleeps.
- Each app on its own workspace machine, so the report's columns are
  independent.

## Running the drill

Check the setup first. A dry run resolves every app and machine, lists the
connections it would kill, and confirms each machine answers, without installing
a rule or sleeping:

```
uv run --script scripts/sleep_wake_drill.py --dry-run
```

If an app has more than one running machine, name the one to drill with
`--workspace` (prefix it with the app's data directory to address one app, as in
`--workspace .minds-staging=other-box`). `--data-dir` restricts the run to named
apps.

Then the real run:

```
uv run --script scripts/sleep_wake_drill.py --auto-sleep
```

This schedules a wake five minutes out, sleeps the laptop, and at the wake kills
every connection the apps had open to their workspaces, by local port and
nothing else. That is the incident's network: the laptop comes back onto a
working network and only the connections that predate the sleep are dead, as
they are behind a NAT that dropped their mappings. A request on an old
connection hangs; a fresh connection goes straight through. The machines answer
the whole time, so an app that calls one stuck was wrong, and an unattended
start it dispatches restarts a machine that was running.

The drill proves the outage before it measures anything. It connects from a
port it holds and named in the rule, which has to hang, and from a fresh port,
which has to answer. If either check fails it stops and says which app and
endpoint, so a run where nothing was blocked cannot be read as a pass.

The dead connections stay dead for the rest of the run (`--post-wake-wait`,
five minutes by default), then the rule comes out and its removal is confirmed
the same way. Without `--auto-sleep` the drill tells you when to close the lid
and waits.

**Note:** the drill holds an idle-sleep assertion for the run, so the laptop
does not doze off again once the display goes dark. The forced sleep still
goes ahead.

### The other scenario

`--scenario recovery-across-sleep` stages the opposite case: the machines are
taken off the network first, the apps are left to convict them and dispatch
restarts, and the laptop sleeps across those restarts. There the conviction is
right, and what is under test is what the sleep does to a `mngr start` whose
deadlines are measured on a clock the sleep stops.

## Reading the report

The report is printed at the end and appended to the JSONL timeline whose path
is printed with it. For the default scenario, each app gets these steps:

0. **The outage was real.** Which connections were open to the machine at the
   wake and were killed. None listed means nothing was open for that app, so
   its column measures nothing; check a window was open before the sleep.
1. **The app touched the machine after the wake.** Whether any request failed,
   when, and how often the app's log named the workspace's agents. Zero mentions
   means nothing was loading the workspace. No failure at all, with mentions,
   means the app rebuilt its tunnels before sending anything on them, which is
   the fix working rather than a missing outage.
2. **The machine was not called stuck.** A STUCK edge here is the defect.
3. **No restart was started for a machine that was running.** A dispatch here
   is the defect.
4. **The sleep signal restarted a failure run that straddled the sleep.** Only
   fires for a run that began before the sleep, so absence is not a failure.

The side-by-side table at the end lists STUCK, dispatch, wake handoff, probe
back, and the card the user was left looking at, per app, timed from the wake.
A pass for the build under test is dashes all the way across. Main, as the
control, should show a STUCK edge around a minute after the wake and a dispatch
right behind it. If main is quiet too, the outage did not reach either app and
the run is unmeasured, whatever the verdicts say.

The pmset sleep/wake log for the run is appended below the table. One `Wake`
line is the normal case. Several gaps mean dark wakes on battery, which the
drill waits out before calling the wake.

## Reading the apps' own logs

The report reads the app's log, but only for the lines it knows. To see the
whole wake window, pull it from each app's `logs/minds-events.jsonl`. The wake
time is in the report; the app logs stamp records in UTC, so adjust the two
timestamps below to bracket it:

```
python3 - <<'EOF'
import json, pathlib
for app in (".minds", ".minds-staging"):
    src = pathlib.Path.home() / app / "logs" / "minds-events.jsonl"
    out = pathlib.Path.home() / "Downloads" / f"wake_window_{app}.txt"
    with out.open("w") as f:
        for line in src.open(errors="replace"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            ts = r.get("timestamp", "")
            if "2026-09-08T22:56:30" <= ts <= "2026-09-08T23:00:00":
                f.write(f"{ts[11:23]} {r.get('level','')[:5]} {r.get('module','')}: {r.get('message','')}\n")
    print(out)
EOF
```

Lines worth finding:

- `Sleep interval recorded` is the app noticing the wake.
- `Rebuilding the tunnel to ... it was established before this machine
  suspended` is the forward retiring a dead tunnel, relayed from its stderr.
  This is the mechanism that keeps a fixed build quiet.
- `Probe-failure run for ... opened Ns after a wake, inside the ... post-wake
  shadow` is the post-wake grace holding a conviction the normal threshold
  would have made. Its presence means the grace was needed, so the tunnel
  retirement alone did not keep every request off a dead connection.
- `Failed to open an SSH channel ... Unable to open channel`, several at once,
  followed by `HEALTHY -> STUCK`, is the incident: the old transport handed
  back, the channel open waiting out its bound, and the probes convicting
  behind it.

## Why the outage is shaped this way

Earlier versions of the drill dropped every packet to the machine for a fixed
window after the wake. That is a real outage, which every build is right to
convict, and it also kills the fresh connection a rebuilt tunnel makes, so it
cannot tell a fix from no fix; two runs read as both builds failing before this
was understood. A window shorter than the app's first post-wake request (about
30 seconds) measured nothing at all and read as both builds holding. Killing
only the connections open at the wake, for the whole run, is what makes main
convict and a fixed build stay quiet on the same sleep.

If a run ever leaves the machine unable to reach its workspaces, clear the rule
by hand with `sudo pfctl -a com.apple/sleep-wake-probe -F rules`. The drill
lifts it on a deadman if the run dies holding it.

## The probe

The probe answers the substrate questions the drill assumes: that
`time.monotonic()` stops during a sleep, that Python deadlines freeze with it,
what releases a blocked SSH read, and how long a cached idle connection stalls
on its first use after the wake. Point it at a workspace's container sshd,
using the "connect over SSH" command from the workspace's recovery card:

```
uv run --script scripts/sleep_wake_probe.py 'ssh -i ~/.minds/... -p 22021 user@host' --auto-sleep --sleep-minutes 4
```

Add `--black-hole` to reproduce the dead-NAT case for the probe's own
connections; without it, a network that delivers the peer's reset makes every
first open fail fast and the 30-second stall never appears.

## Related

- `blueprint/environment-signals/plan-environment-signals.md`: the design and
  the findings from the live log reviews.
- `specs/mngr-start-slowness-investigation.md`: why a `mngr start` can block
  across a sleep.
- `testing-overview.md`: where the automated tests for this area live.
