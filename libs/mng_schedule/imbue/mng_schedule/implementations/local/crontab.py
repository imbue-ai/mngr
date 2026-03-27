"""Crontab utilities for local schedule management.

Pure functions for manipulating crontab content as text, plus thin wrappers
for reading/writing the system crontab via subprocess.
"""

import subprocess

from imbue.imbue_common.pure import pure
from imbue.mng_schedule.errors import ScheduleDeployError


@pure
def build_marker_comment(prefix: str, trigger_name: str) -> str:
    """Build the crontab marker comment for a trigger.

    Uses the configured mng prefix to ensure isolation between different
    mng installations and test environments.
    """
    return f"# {prefix}schedule:{trigger_name}"


@pure
def add_crontab_entry(
    existing_content: str,
    prefix: str,
    trigger_name: str,
    cron_expression: str,
    command: str,
) -> str:
    """Add or replace a crontab entry for the given trigger.

    If an entry with the same trigger name already exists, it is replaced.
    Otherwise, the new entry is appended.

    Each entry consists of two lines:
    1. A marker comment: # {prefix}schedule:{name}
    2. The cron line: <cron_expression> <command>

    Returns the updated crontab content as a string.
    """
    cleaned = remove_crontab_entry(existing_content, prefix, trigger_name)

    marker = build_marker_comment(prefix, trigger_name)
    cron_line = f"{cron_expression} {command}"

    if cleaned and not cleaned.endswith("\n"):
        cleaned += "\n"

    return cleaned + marker + "\n" + cron_line + "\n"


@pure
def remove_crontab_entry(existing_content: str, prefix: str, trigger_name: str) -> str:
    """Remove the crontab entry for the given trigger name.

    Removes the marker comment line and the following cron line.
    Returns the updated crontab content. If no matching entry is found,
    returns the content unchanged.
    """
    marker = build_marker_comment(prefix, trigger_name)
    lines = existing_content.splitlines(keepends=True)
    result_lines: list[str] = []
    skip_next = False
    for line in lines:
        if skip_next:
            skip_next = False
            continue
        if line.rstrip() == marker:
            skip_next = True
            continue
        result_lines.append(line)
    return "".join(result_lines)


@pure
def list_managed_trigger_names(crontab_content: str, prefix: str) -> list[str]:
    """Extract all mng-managed trigger names from crontab content.

    Returns a list of trigger names found in marker comments that match
    the given prefix.
    """
    marker_prefix = f"# {prefix}schedule:"
    names: list[str] = []
    for line in crontab_content.splitlines():
        stripped = line.strip()
        if stripped.startswith(marker_prefix):
            name = stripped[len(marker_prefix) :]
            if name:
                names.append(name)
    return names


def read_system_crontab() -> str:
    """Read the current user's crontab.

    Returns the crontab content as a string, or an empty string if
    no crontab exists. Raises ScheduleDeployError for unexpected errors
    (e.g. permission denied) to prevent silent data loss.
    """
    result = subprocess.run(
        ["crontab", "-l"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        if "no crontab" in result.stderr.lower():
            return ""
        raise ScheduleDeployError(f"Failed to read crontab: {result.stderr.strip()}")
    return result.stdout


def write_system_crontab(content: str) -> None:
    """Write content to the current user's crontab.

    Raises ScheduleDeployError if the write fails.
    """
    result = subprocess.run(
        ["crontab", "-"],
        input=content,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ScheduleDeployError(f"Failed to write crontab: {result.stderr.strip()}")
