#!/usr/bin/env python3
"""Remove the *old* flat minds Vault secrets once they've been mirrored to the split layout.

Background: minds secrets used to live as one flat KV-v2 entry per service
holding many fields::

    secrets/minds/<tier>/<service>      # { "A": "...", "B": "..." }

They have since been mirrored to the "split" layout, where every key is its
own single-`value` leaf::

    secrets/minds/<tier>/<service>/A    # { "value": "..." }
    secrets/minds/<tier>/<service>/B    # { "value": "..." }

This script deletes the *old flat* entry at ``secrets/minds/<tier>/<service>``
for every service in a tier, leaving the split children untouched (a Vault
KV-v2 ``metadata delete`` of the parent path removes only that path's data,
not its children). With the old paths gone, any code that still reads them
fails loudly instead of silently succeeding -- which is the whole point of
running this before a verification deploy.

Safety:

* Defaults to a dry-run. Pass ``--yes`` to actually delete.
* Only deletes a flat entry when its full contents (every key AND value)
  are already present in the split children. Any service whose flat entry
  is missing a split mirror, or whose mirror disagrees, is SKIPPED and
  reported -- never deleted -- and makes the script exit non-zero.

Usage::

    uv run scripts/remove_old_flat_vault_secrets.py dev            # dry-run
    uv run scripts/remove_old_flat_vault_secrets.py dev --yes      # delete

``VAULT_NAMESPACE`` / ``VAULT_ADDR`` default to the imbue HCP cluster values
if the operator did not already export them. Run ``vault login`` first.
"""

import argparse
import json
import os
import subprocess
import sys
from typing import Final

_DEFAULT_VAULT_ADDR: Final[str] = "https://vault-cluster-public-vault-df29b16f.9b573ab7.z1.hashicorp.cloud:8200"
_DEFAULT_VAULT_NAMESPACE: Final[str] = "admin"
_MOUNT: Final[str] = "secrets"
# `vault kv get` / `kv list` exit 2 when the path holds no secret.
_NOT_FOUND_EXIT_CODE: Final[int] = 2
_VALUE_FIELD: Final[str] = "value"


def _run_vault(args: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["vault", *args], capture_output=True, text=True, env=env)


def _list_keys(path: str, *, env: dict[str, str]) -> list[str] | None:
    """Return the child key names under ``path``, or None if the path has no listing."""
    result = _run_vault(["kv", "list", "-format=json", f"-mount={_MOUNT}", path], env=env)
    if result.returncode == _NOT_FOUND_EXIT_CODE:
        return None
    if result.returncode != 0:
        raise RuntimeError(f"`vault kv list {path}` failed (exit {result.returncode}): {result.stderr.strip()}")
    parsed = json.loads(result.stdout)
    if not isinstance(parsed, list):
        raise RuntimeError(f"`vault kv list {path}` returned {type(parsed).__name__}, expected a JSON array")
    return [str(entry) for entry in parsed]


def _get_data(path: str, *, env: dict[str, str]) -> dict[str, str] | None:
    """Return the ``data.data`` dict of the secret at ``path``, or None if absent."""
    result = _run_vault(["kv", "get", "-format=json", f"-mount={_MOUNT}", path], env=env)
    if result.returncode == _NOT_FOUND_EXIT_CODE:
        return None
    if result.returncode != 0:
        raise RuntimeError(f"`vault kv get {path}` failed (exit {result.returncode}): {result.stderr.strip()}")
    inner = json.loads(result.stdout).get("data", {}).get("data")
    if not isinstance(inner, dict):
        raise RuntimeError(f"`vault kv get {path}` returned no data.data dict")
    return {str(k): str(v) for k, v in inner.items()}


def _read_split_contents(service_path: str, *, env: dict[str, str]) -> dict[str, str]:
    """Reconstruct the ``{key: value}`` dict from the split leaves under ``service_path``."""
    child_keys = _list_keys(service_path, env=env)
    if not child_keys:
        return {}
    contents: dict[str, str] = {}
    for key in child_keys:
        # A nested directory (trailing slash) is not a flat `value` leaf.
        if key.endswith("/"):
            raise RuntimeError(f"{service_path}/{key} is a nested directory, not a single-`value` leaf")
        leaf = _get_data(f"{service_path}/{key}", env=env)
        if leaf is None or _VALUE_FIELD not in leaf:
            raise RuntimeError(f"{service_path}/{key} has no `{_VALUE_FIELD}` field")
        contents[key] = leaf[_VALUE_FIELD]
    return contents


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tier", help="Tier whose old flat secrets to remove (e.g. dev, staging, production)")
    parser.add_argument("--yes", action="store_true", help="Actually delete (default is a dry-run)")
    args = parser.parse_args()

    env = os.environ.copy()
    env.setdefault("VAULT_NAMESPACE", _DEFAULT_VAULT_NAMESPACE)
    env.setdefault("VAULT_ADDR", _DEFAULT_VAULT_ADDR)

    tier_path = f"minds/{args.tier}"
    listing = _list_keys(tier_path, env=env)
    if not listing:
        print(f"error: nothing listed at secrets/{tier_path} (wrong tier, or not logged in?)", file=sys.stderr)
        return 2

    # Derive candidate service names from the whole listing. In Vault KV-v2 a
    # path that holds a secret AND has children is listed only once, with a
    # trailing slash, so the bare name never appears for a mirrored service.
    # Normalize away the trailing slash and probe each candidate directly to
    # decide whether it has a flat entry and/or a split mirror.
    candidate_services = sorted({entry.rstrip("/") for entry in listing})

    to_delete: list[str] = []
    skipped: list[str] = []
    for service in candidate_services:
        service_path = f"{tier_path}/{service}"
        flat_contents = _get_data(service_path, env=env)
        if flat_contents is None:
            # No flat secret lives at this path (it is purely a split directory
            # or has nothing readable) -- nothing to remove.
            continue
        split_contents = _read_split_contents(service_path, env=env)
        if not split_contents:
            print(f"  SKIP  {service_path}: no split mirror exists (flat entry left in place)")
            skipped.append(service)
            continue
        if flat_contents != split_contents:
            only_flat = sorted(set(flat_contents) - set(split_contents))
            only_split = sorted(set(split_contents) - set(flat_contents))
            mismatched = sorted(
                k for k in flat_contents.keys() & split_contents.keys() if flat_contents[k] != split_contents[k]
            )
            print(
                f"  SKIP  {service_path}: split mirror does not match the flat entry "
                f"(only-flat={only_flat}, only-split={only_split}, value-mismatch={mismatched})"
            )
            skipped.append(service)
            continue
        to_delete.append(service)
        print(f"  DELETE {service_path}: fully mirrored ({len(flat_contents)} key(s)); flat entry will be removed")

    print()
    print(f"Plan for tier {args.tier!r}: delete {len(to_delete)} flat entr(y/ies), skip {len(skipped)}.")
    if not args.yes:
        print("Dry-run only. Re-run with --yes to delete the flat entries above.")
        return 1 if skipped else 0

    for service in to_delete:
        service_path = f"{tier_path}/{service}"
        result = _run_vault(["kv", "metadata", "delete", f"-mount={_MOUNT}", service_path], env=env)
        if result.returncode != 0:
            print(
                f"error: failed to delete {service_path} (exit {result.returncode}): {result.stderr.strip()}",
                file=sys.stderr,
            )
            return 1
        print(f"  deleted secrets/{service_path}")

    print()
    print(f"Done. Removed {len(to_delete)} old flat entr(y/ies) from tier {args.tier!r}.")
    return 1 if skipped else 0


if __name__ == "__main__":
    sys.exit(main())
