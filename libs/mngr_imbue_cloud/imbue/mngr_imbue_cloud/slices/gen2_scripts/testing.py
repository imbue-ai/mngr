import subprocess


def assert_valid_bash(script: str) -> None:
    """Fail when ``bash -n`` rejects the rendered script (a syntax error in a box-side script)."""
    result = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)
    assert result.returncode == 0, f"bash -n rejected the rendered script: {result.stderr}"
