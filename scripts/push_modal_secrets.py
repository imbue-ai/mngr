#!/usr/bin/env python3
"""Upsert Modal secrets from local ``.minds/<env>/<service>.sh`` files.

Each ``.sh`` file is shell-style: one ``export KEY=value`` (or plain
``KEY=value``) line per variable. Lines starting with ``#`` and blank lines
are ignored. The script sources each file via ``bash`` so quoting, escapes,
and ``export`` prefixes behave exactly as they do when you source the file
yourself.

``.minds/template/`` is the schema: every file there defines the expected
keys for a service. Per-env files (e.g. ``.minds/production/``) must declare
every key the template declares (empty values are fine and just mean "unset
on the server"). The push aborts with a diagnostic if a per-env file drifts
from the template.

Given ``.minds/production/cloudflare.sh`` and ``.minds/production/supertokens.sh``,
this pushes two Modal secrets: ``cloudflare-production`` and
``supertokens-production``. Existing secrets are overwritten (``--force``).

Usage:
    uv run scripts/push_modal_secrets.py <env-name>
    uv run scripts/push_modal_secrets.py <env-name> --dir <dir>   # override .minds

Examples:
    uv run scripts/push_modal_secrets.py production
    uv run scripts/push_modal_secrets.py staging
"""

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

_TEMPLATE_DIR_NAME = "template"


def _parse_env_file(path: Path) -> dict[str, str]:
    """Return every ``KEY=value`` pair declared in a shell-style env file.

    Uses ``bash`` to source the file with ``set -a`` and dump the resulting
    environment as a null-delimited list, so quoting, escapes, ``export``
    prefixes, and variable interpolation all behave exactly as they do in a
    real shell. Empty values are kept (they signal "declared but unset").

    Runs both the source step and the baseline comparison inside ``env -i``
    so the deployer's own shell env cannot either shadow keys set by the
    file (baseline-value-equals-file-value => silently dropped) or leak
    into interpolated values (``SUPERTOKENS_API_KEY=${OTHER}`` pulling
    from the deployer's env).
    """
    if not path.is_file():
        raise FileNotFoundError(f"env file not found: {path}")
    script = f"set -a; . {shlex.quote(str(path))}; env -0"
    result = subprocess.run(
        ["env", "-i", "bash", "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    baseline = subprocess.run(
        ["env", "-i", "bash", "-c", "env -0"],
        capture_output=True,
        text=True,
        check=True,
    )
    before = _parse_env_dump(baseline.stdout)
    after = _parse_env_dump(result.stdout)
    # Keep every key whose value changed vs. the clean-shell baseline. That
    # covers both "newly declared" (not in baseline) and "overwritten to a
    # different value" (in baseline but different). Empty-string values are
    # retained -- they signal "declared but intentionally unset".
    return {k: v for k, v in after.items() if before.get(k) != v}


def _parse_env_dump(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for entry in raw.split("\0"):
        if not entry:
            continue
        key, sep, value = entry.partition("=")
        if not sep:
            continue
        result[key] = value
    return result


def _validate_against_template(
    template_dir: Path,
    env_dir: Path,
) -> list[str]:
    """Return a list of human-readable error messages if the env drifts from the template.

    Each template ``<service>.sh`` must have a matching ``<service>.sh`` in the
    env directory, and that env file must declare (value optional) every key
    the template declares. Extra keys in the env file are allowed.
    """
    if not template_dir.is_dir():
        return [f"template directory not found: {template_dir}"]

    errors: list[str] = []
    template_files = sorted(template_dir.glob("*.sh"))
    if not template_files:
        return [f"template directory has no .sh files: {template_dir}"]

    for template_file in template_files:
        env_file = env_dir / template_file.name
        template_keys = set(_parse_env_file(template_file).keys())
        if not env_file.is_file():
            errors.append(
                f"{env_dir.name}/{template_file.name} is missing; template declares keys {sorted(template_keys)}"
            )
            continue
        env_keys = set(_parse_env_file(env_file).keys())
        missing = template_keys - env_keys
        if missing:
            errors.append(
                f"{env_dir.name}/{template_file.name} is missing keys declared in "
                f"template/{template_file.name}: {sorted(missing)}. "
                f"Add `export <KEY>=` lines (empty is fine)."
            )
        extras = env_keys - template_keys
        if extras:
            # Not an error -- extra keys are allowed. Warn so drift is visible.
            print(
                f"[warn] {env_dir.name}/{template_file.name} has extra keys "
                f"not declared in template/{template_file.name}: {sorted(extras)}",
                file=sys.stderr,
            )
    return errors


def _upsert_modal_secret(name: str, values: dict[str, str], is_dry_run: bool) -> None:
    """Create or overwrite a Modal secret with the given non-empty key/value pairs."""
    non_empty = {k: v for k, v in values.items() if v}
    if not non_empty:
        print(f"[skip] {name}: no non-empty KEY=VALUE pairs found", file=sys.stderr)
        return
    args = ["uv", "run", "modal", "secret", "create", "--force", name]
    for key, value in non_empty.items():
        args.append(f"{key}={value}")
    printable = [args[i] if "=" not in args[i] else f"{args[i].split('=', 1)[0]}=***" for i in range(len(args))]
    print(f"[push] {name}: {len(non_empty)} key(s)")
    print(f"       {' '.join(shlex.quote(p) for p in printable)}")
    if is_dry_run:
        return
    subprocess.run(args, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("env_name", help="Environment name (e.g. production, staging)")
    parser.add_argument(
        "--dir",
        default=".minds",
        help="Root directory holding template/ and <env>/ subdirectories (default: .minds)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the Modal commands without executing them",
    )
    args = parser.parse_args()

    if args.env_name == _TEMPLATE_DIR_NAME:
        print(
            f"error: '{_TEMPLATE_DIR_NAME}' is reserved for the committed schema; "
            f"use a concrete env name like 'production'",
            file=sys.stderr,
        )
        return 2

    root = Path(args.dir)
    template_dir = root / _TEMPLATE_DIR_NAME
    env_dir = root / args.env_name

    if not env_dir.is_dir():
        print(f"error: directory not found: {env_dir}", file=sys.stderr)
        return 2

    errors = _validate_against_template(template_dir, env_dir)
    if errors:
        print("error: per-env files are out of sync with the template:", file=sys.stderr)
        for msg in errors:
            print(f"  - {msg}", file=sys.stderr)
        return 1

    template_files = sorted(template_dir.glob("*.sh"))
    for template_file in template_files:
        service_name = template_file.stem
        secret_name = f"{service_name}-{args.env_name}"
        env_file = env_dir / template_file.name
        values = _parse_env_file(env_file)
        _upsert_modal_secret(secret_name, values, is_dry_run=args.dry_run)

    return 0


if __name__ == "__main__":
    sys.exit(main())
