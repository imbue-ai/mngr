#!/usr/bin/env python3
"""Propagate macOS keychain credentials to all per-agent Claude Code entries.

This script runs as a Notification:auth_success hook. When a user authenticates
in one Claude Code session, it copies the new credential to every other per-agent
keychain entry so all sessions pick up the change.

Standalone: no mngr imports, uses only Python stdlib.
"""

import getpass
import hashlib
import os
import platform
import re
import subprocess
import sys


def _compute_keychain_label_suffix(config_dir: str) -> str:
    """Compute the keychain label suffix Claude Code uses for a given CLAUDE_CONFIG_DIR.

    Claude Code appends -<sha256(config_dir)[:8]> to keychain labels when
    CLAUDE_CONFIG_DIR is set, to avoid collisions between config dirs.
    """
    normalized = os.path.normpath(config_dir)
    return "-" + hashlib.sha256(normalized.encode()).hexdigest()[:8]


def _read_keychain(label: str) -> str | None:
    """Read a credential from the macOS keychain by service name."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", label, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _write_keychain(label: str, value: str, account: str) -> bool:
    """Write a credential to the macOS keychain, replacing any existing entry.

    Returns True on success, False on failure.
    """
    try:
        subprocess.run(
            ["security", "delete-generic-password", "-s", label, "-a", account],
            capture_output=True,
            text=True,
            timeout=5,
        )
        result = subprocess.run(
            ["security", "add-generic-password", "-s", label, "-a", account, "-l", label, "-w", value],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            print(f"sync_keychain_credentials: failed to write {label!r}: {result.stderr}", file=sys.stderr)
            return False
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"sync_keychain_credentials: error writing {label!r}: {e}", file=sys.stderr)
        return False


def _find_per_agent_labels(prefix: str) -> list[str]:
    """Find all per-agent keychain labels matching the given prefix.

    Per-agent labels look like "Claude Code-<8 hex chars>" or
    "Claude Code-credentials-<8 hex chars>".
    """
    try:
        result = subprocess.run(
            ["security", "dump-keychain", "login.keychain-db"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    if result.returncode != 0:
        return []
    if prefix == "Claude Code-credentials":
        pattern = r'"Claude Code-credentials-[a-f0-9]{8}"'
    else:
        pattern = r'"Claude Code-[a-f0-9]{8}"'
    # Deduplicate: each entry appears twice in the dump (hex attr + named attr)
    return list({m.strip('"') for m in re.findall(pattern, result.stdout)})


def _sync_entries(prefix: str, suffix: str, account: str) -> None:
    """Sync a credential type from the current agent to all other per-agent entries."""
    current_label = f"{prefix}{suffix}"
    new_value = _read_keychain(current_label)
    if not new_value:
        return

    # Update the default (unsuffixed) entry
    _write_keychain(prefix, new_value, account)

    # Update all other per-agent entries
    for label in _find_per_agent_labels(prefix):
        if label != current_label:
            _write_keychain(label, new_value, account)


def main() -> None:
    if platform.system() != "Darwin":
        return
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if not config_dir:
        return

    suffix = _compute_keychain_label_suffix(config_dir)
    account = getpass.getuser()

    _sync_entries("Claude Code-credentials", suffix, account)
    _sync_entries("Claude Code", suffix, account)


if __name__ == "__main__":
    main()
