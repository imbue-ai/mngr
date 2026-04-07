"""Standalone script to build a Docker image on Modal (uncached).

Used as a control experiment to compare sandbox vs release image build times.
"""

import os
import signal
import sys
import time
import modal

DOCKERFILE = "libs/mngr/imbue/mngr/resources/Dockerfile.release"
TIMEOUT = 600  # 10 min — generous for fully uncached build with different base layers


def alarm_handler(signum, frame):
    elapsed = time.time() - start
    print(f"\nKILLED after {elapsed:.1f}s (timeout={TIMEOUT}s). "
          f"Dockerfile.release did NOT complete in 2X time.", flush=True)
    sys.exit(1)


print(f"Building image from {DOCKERFILE} (force_build=True)...", flush=True)
print(f"Timeout set to {TIMEOUT}s (2X of sandbox build time)", flush=True)

app = modal.App("danver-modal-release-proving")

image = modal.Image.from_dockerfile(
    DOCKERFILE,
    context_dir=".",
    force_build=True,
)


@app.function(image=image, timeout=3600)
def check():
    import subprocess

    result = subprocess.run(["uname", "-a"], capture_output=True, text=True)
    print(result.stdout)
    print("Image build succeeded and function ran in container!")


if __name__ == "__main__":
    start = time.time()
    signal.signal(signal.SIGALRM, alarm_handler)
    signal.alarm(TIMEOUT)
    print("Launching Modal app...", flush=True)
    try:
        with app.run():
            result = check.remote()
        elapsed = time.time() - start
        print(f"Done! Total time: {elapsed:.1f}s", flush=True)
        sys.exit(0)
    except SystemExit:
        raise
    except Exception as e:
        elapsed = time.time() - start
        print(f"Failed after {elapsed:.1f}s: {e}", flush=True)
        sys.exit(1)
