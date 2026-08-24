Recovery restart failures are no longer a black box, from the `mngr start` slowness investigation (`specs/mngr-start-slowness-investigation.md`):

- An `mngr` subprocess that fails now carries bounded tails of its captured stdout/stderr on `MngrCommandError`, and the restart-failure error record includes them -- naming the step the command died in. Previously the killed subprocess's output was discarded entirely. This applies to every `mngr` minds runs, not just the recovery restart: the same failure policy covers the label / destroy / gc / listing commands too.

- The recovery argv now passes `-v` instead of `--quiet`, so the captured stderr contains mngr's step-by-step DEBUG timeline (the output goes to the capture pipe, not a terminal). That timeline stays on the tail: the error *message* is narrowed to mngr's own verdict, so the restart-failure text the user reads stays short, and the checks that read it -- "this provider cannot stop hosts", and whether the command was rejected at the machine's backend -- see only what mngr actually failed on, never a provider it skipped and carried on past.

- The bug-report attachment sweep now uploads the per-command mngr CLI event log (`<mngr_host_dir>/events/logs/mngr/events.jsonl`). The investigation found the bundle only carried the latchkey forward daemon's separate log file, so the hanging starts' timelines never reached Sentry.
