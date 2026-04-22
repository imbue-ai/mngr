"""Private launcher that spawns a detached ``latchkey gateway`` and exits.

Invoked by ``LatchkeyGatewayManager`` via ``python -m`` so that the spawned
``latchkey gateway`` process outlives the minds desktop client. The launcher:

1. Opens a log file (creating parent directories as needed).
2. Starts ``latchkey gateway`` with ``start_new_session=True`` so it detaches
   from the caller's process group and is not killed when the caller (or the
   desktop client that launched the caller) exits.
3. Prints the child's PID to stdout so ``LatchkeyGatewayManager`` can record
   it, then exits.

Why this file uses raw ``subprocess.Popen`` (triggering a narrow exclusion
from the ``check_direct_subprocess`` ratchet): ``ConcurrencyGroup.run_process_*``
is built around *managed* subprocesses that must be cleaned up when the group
exits. We need the exact opposite behavior here -- the gateway must outlive
the caller. Using ``ConcurrencyGroup`` and then trying to "detach" a tracked
child would defeat the ratchet's intent just as badly while being more
complex. Keeping the subprocess call confined to this tiny, well-named
launcher makes the exception obvious.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Spawn a detached latchkey gateway.")
    parser.add_argument("--latchkey-binary", required=True, help="Path to the latchkey binary.")
    parser.add_argument("--listen-host", required=True, help="Host the gateway should bind to.")
    parser.add_argument("--listen-port", required=True, type=int, help="Port the gateway should bind to.")
    parser.add_argument("--log-path", required=True, help="Path where gateway stdout/stderr will be appended.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    log_path = Path(args.log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # latchkey reads listen host/port from these environment variables (see
    # the latchkey README). We set them in the child's env rather than on our
    # own process so repeated launches with different ports don't race.
    child_env = dict(os.environ)
    child_env["LATCHKEY_GATEWAY_LISTEN_HOST"] = args.listen_host
    child_env["LATCHKEY_GATEWAY_LISTEN_PORT"] = str(args.listen_port)

    # Append rather than truncate so prior runs' output is preserved.
    log_file = log_path.open("ab")
    try:
        # start_new_session=True calls setsid() in the child so it becomes its
        # own session/process-group leader and survives the parent's death.
        # stdout/stderr redirected to a file keep the gateway producing output
        # after the parent's pipes close.
        process = subprocess.Popen(  # noqa: S603  # see module docstring
            [args.latchkey_binary, "gateway"],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            env=child_env,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        # Our copy of the log fd can be closed; the child inherited its own.
        log_file.close()

    # Report the PID so the caller can persist it.
    sys.stdout.write(f"{process.pid}\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
