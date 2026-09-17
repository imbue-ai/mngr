"""Manual verification script for warm_cli. Not a pytest test -- run directly."""
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_CLICK_SCRIPT = '''
import click
from imbue.imbue_common.warm_cli import warm_cli

@click.command()
@click.argument("name")
def hello(name):
    click.echo(f"Hello, {name}!")

if __name__ == "__main__":
    warm_cli(hello)
'''


def main() -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(_CLICK_SCRIPT)
        script_path = f.name

    try:
        # Cold path: first invocation, no warm process exists
        print("=== Cold path invocation ===")
        start = time.monotonic()
        result = subprocess.run(
            [sys.executable, script_path, "World"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        cold_elapsed = time.monotonic() - start
        print(f"stdout: {result.stdout.strip()}")
        print(f"stderr: {result.stderr.strip()}" if result.stderr.strip() else "stderr: (empty)")
        print(f"exit code: {result.returncode}")
        print(f"elapsed: {cold_elapsed:.3f}s")
        assert result.returncode == 0, f"Cold path failed with exit code {result.returncode}"
        assert "Hello, World!" in result.stdout, f"Expected 'Hello, World!' in output, got: {result.stdout}"
        print("PASS: cold path")

        # Give the warm successor a moment to bind
        time.sleep(0.5)

        # Warm path: second invocation should connect to the pre-warmed process
        print("\n=== Warm path invocation ===")
        start = time.monotonic()
        result = subprocess.run(
            [sys.executable, script_path, "Warm"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        warm_elapsed = time.monotonic() - start
        print(f"stdout: {result.stdout.strip()}")
        print(f"stderr: {result.stderr.strip()}" if result.stderr.strip() else "stderr: (empty)")
        print(f"exit code: {result.returncode}")
        print(f"elapsed: {warm_elapsed:.3f}s")
        assert result.returncode == 0, f"Warm path failed with exit code {result.returncode}"
        assert "Hello, Warm!" in result.stdout, f"Expected 'Hello, Warm!' in output, got: {result.stdout}"
        print("PASS: warm path")

        # Third invocation (the successor of the warm path)
        time.sleep(0.5)
        print("\n=== Second warm path invocation ===")
        start = time.monotonic()
        result = subprocess.run(
            [sys.executable, script_path, "Again"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        warm2_elapsed = time.monotonic() - start
        print(f"stdout: {result.stdout.strip()}")
        print(f"stderr: {result.stderr.strip()}" if result.stderr.strip() else "stderr: (empty)")
        print(f"exit code: {result.returncode}")
        print(f"elapsed: {warm2_elapsed:.3f}s")
        assert result.returncode == 0, f"Second warm path failed with exit code {result.returncode}"
        assert "Hello, Again!" in result.stdout, f"Expected 'Hello, Again!' in output, got: {result.stdout}"
        print("PASS: second warm path")

        print(f"\n=== Summary ===")
        print(f"Cold: {cold_elapsed:.3f}s")
        print(f"Warm: {warm_elapsed:.3f}s")
        print(f"Warm2: {warm2_elapsed:.3f}s")
        print("ALL PASS")
    finally:
        Path(script_path).unlink(missing_ok=True)
        # Clean up socket file
        import glob
        for sock in glob.glob("/tmp/warm_cli_*"):
            Path(sock).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
