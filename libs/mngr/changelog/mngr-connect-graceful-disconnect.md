`mngr connect` (and `mngr create`/`mngr start` with `--connect`) recovers when its SSH session to a remote agent drops, for example after the laptop sleeps.

- The terminal modes the remote session switched on are switched off again, the same set tmux's client resets when it releases a terminal: the scroll region, mouse reporting, bracketed paste, focus reporting, keypad mode, the alternate screen, text attributes and a hidden cursor. Before, a dropped session could leave the shell receiving every mouse movement as typed text.

- If the host paused while disconnected (idle timeout or lifetime cap), it is restored and the agent started again before reattaching, at the host's new address. `mngr connect --no-start` leaves a paused host alone. A host that was stopped, destroyed, failed or crashed is not restored; the command stops with an error instead.

- A drop that spans a sleep reconnects at once without using a retry (`connect_retry_times`).

- When the session cannot be reattached, the command now exits non-zero instead of returning as if the session had ended normally, and a remote exit status other than a lost connection or a Ctrl-q/Ctrl-t action is reported as an error rather than retried.
