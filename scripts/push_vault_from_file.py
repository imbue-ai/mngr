#!/usr/bin/env python3
"""Push one Vault entry from a filled-in copy of a `.minds/template/*.sh` file.

Usage:
    uv run scripts/push_vault_from_file.py <tier> <service> <filled-file>

The operator workflow:

    cp .minds/template/litellm.sh /tmp/litellm-dev.sh
    $EDITOR /tmp/litellm-dev.sh        # fill in the values
    uv run scripts/push_vault_from_file.py dev litellm /tmp/litellm-dev.sh
    shred -u /tmp/litellm-dev.sh

The script:

1. Sources the filled file in a clean shell so quoting / `export` prefixes /
   embedded variable interpolation behave exactly like a real shell.
2. Sources the matching template file the same way to discover the expected
   key set.
3. Errors out if the filled file is missing any template key.
4. Pushes every declared key (including empty values, so the Vault layout
   matches the template schema exactly) as its own single-`value` leaf at
   `secrets/minds/<tier>/<service>/<KEY>` via `vault kv put`.

`VAULT_NAMESPACE` and `VAULT_ADDR` default to the imbue HCP cluster values
if the operator did not already export them.
"""

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Final

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATE_DIR = _REPO_ROOT / ".minds" / "template"
_DEFAULT_VAULT_ADDR: Final[str] = "https://vault-cluster-public-vault-df29b16f.9b573ab7.z1.hashicorp.cloud:8200"
_DEFAULT_VAULT_NAMESPACE: Final[str] = "admin"


def _parse_env_file(path: Path) -> dict[str, str]:
    """Return every KEY=value pair declared in a shell-style env file.

    Sourced in a clean `env -i bash` so the deployer's own shell env never
    shadows file-declared keys or contaminates interpolated values.
    """
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
    # Keep every key whose value changed from baseline -- covers both newly
    # declared keys and keys overwritten to a different value. Empty values
    # are retained (they signal "declared but intentionally unset").
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tier", help="Environment tier name (e.g. dev, staging, production)")
    parser.add_argument("service", help="Service name (must match a .minds/template/<service>.sh file)")
    parser.add_argument("filled_file", type=Path, help="Filled-in copy of the template")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the vault command without executing it",
    )
    args = parser.parse_args()

    template = _TEMPLATE_DIR / f"{args.service}.sh"
    if not template.is_file():
        print(f"error: no template at {template}", file=sys.stderr)
        return 2
    if not args.filled_file.is_file():
        print(f"error: filled file not found: {args.filled_file}", file=sys.stderr)
        return 2

    template_keys = set(_parse_env_file(template).keys())
    filled_values = _parse_env_file(args.filled_file)
    missing = sorted(template_keys - set(filled_values))
    extra = sorted(set(filled_values) - template_keys)
    if missing:
        print(
            f"error: filled file is missing keys declared in .minds/template/{args.service}.sh: {missing}",
            file=sys.stderr,
        )
        return 1
    if extra:
        print(
            f"warning: filled file has extra keys not in template: {extra} (will be pushed anyway)",
            file=sys.stderr,
        )
    non_empty = {k: v for k, v in filled_values.items() if v}
    if not non_empty:
        print("error: every value is empty -- nothing meaningful to push", file=sys.stderr)
        return 1

    service_path = f"minds/{args.tier}/{args.service}"
    print(f"[push] secrets/{service_path}/*: {len(filled_values)} key(s), {len(non_empty)} non-empty")

    env = os.environ.copy()
    env.setdefault("VAULT_NAMESPACE", _DEFAULT_VAULT_NAMESPACE)
    env.setdefault("VAULT_ADDR", _DEFAULT_VAULT_ADDR)

    # The split layout stores each key as its own single-`value` leaf at
    # `secrets/minds/<tier>/<service>/<KEY>`. Pipe each value as JSON on stdin
    # instead of a `value=VALUE` positional arg: the vault CLI interprets a
    # leading `@` in a positional value as a "read from file" sigil, which
    # mangles emails / suffix lists. Stdin JSON bypasses that parser entirely.
    for key, value in filled_values.items():
        leaf_path = f"{service_path}/{key}"
        command = ["vault", "kv", "put", "-mount=secrets", leaf_path, "-"]
        printable = " ".join(shlex.quote(p) for p in command)
        print(f"       {printable} < <json on stdin>")
        if args.dry_run:
            continue
        subprocess.run(command, check=True, env=env, input=json.dumps({"value": value}), text=True)

    if args.dry_run:
        return 0

    print()
    print(f"Done. Now shred the filled file: shred -u {args.filled_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
