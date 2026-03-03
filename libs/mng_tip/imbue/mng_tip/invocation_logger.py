import json
import os
import sys
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any


def get_tip_data_dir() -> Path:
    """Return the directory where tip data is stored.

    Uses MNG_HOST_DIR if set, otherwise defaults to ~/.mng.
    """
    env_host_dir = os.environ.get("MNG_HOST_DIR")
    base_dir = Path(env_host_dir) if env_host_dir else Path("~/.mng")
    return base_dir.expanduser() / "tip"


def log_invocation(command_name: str, command_params: dict[str, Any]) -> None:
    """Append an invocation record to the JSONL log file.

    Each record includes a UTC timestamp, the canonical command name,
    and the raw sys.argv for context.
    """
    tip_dir = get_tip_data_dir()
    tip_dir.mkdir(parents=True, exist_ok=True)

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "command": command_name,
        "argv": sys.argv,
    }

    invocations_path = tip_dir / "invocations.jsonl"
    with open(invocations_path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def read_recent_invocations(max_lines: int = 200) -> list[dict[str, Any]]:
    """Read the most recent invocation records from the log file.

    Returns up to max_lines records, most recent last.
    """
    invocations_path = get_tip_data_dir() / "invocations.jsonl"
    if not invocations_path.exists():
        return []

    lines = invocations_path.read_text().strip().splitlines()
    recent_lines = lines[-max_lines:]

    records: list[dict[str, Any]] = []
    for line in recent_lines:
        try:
            records.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue
    return records
