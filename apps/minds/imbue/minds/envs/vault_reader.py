"""Thin wrapper around the ``vault`` CLI.

Deploy scripts call ``read_vault_kv(path)`` to fetch a Vault KV-v2 secret as
a flat ``{key: value}`` dict. We shell out to the locally-installed ``vault``
CLI (``vault kv get -format=json -mount=secrets kv/...``) rather than using
hvac so authentication piggybacks on whatever the operator already set up
(``vault login``, ``VAULT_ADDR``, ``VAULT_NAMESPACE``, etc.) -- no token
plumbing inside this codebase.

The returned values are kept in process memory only; no file is written.
"""

import json
import shutil
import subprocess
from typing import Final

from imbue.minds.envs.primitives import VaultReadError

VAULT_BINARY: Final[str] = "vault"
_DEFAULT_MOUNT: Final[str] = "secrets"
_KV_PATH_PREFIX: Final[str] = "secrets/kv/"


class VaultPath(str):
    """A Vault KV path of the form ``secrets/kv/...`` (mount/secret-key).

    Subclassed from str so we can pass it directly as a CLI arg without an
    explicit cast, while still type-tagging the input contract.
    """


def read_vault_kv(path: VaultPath, *, vault_binary: str = VAULT_BINARY) -> dict[str, str]:
    """Return the ``data.data`` dict of the KV-v2 secret at ``path``.

    ``path`` looks like ``secrets/kv/minds/production/cloudflare``. We strip
    the ``secrets/`` mount prefix off the front and pass the rest to
    ``vault kv get -format=json -mount=secrets <rest>`` so the user's existing
    ``VAULT_ADDR`` / ``VAULT_NAMESPACE`` / token configuration is honored.

    Raises :class:`VaultReadError` for any failure (CLI missing, command
    failed, output not parseable, no ``data.data`` field, value not a
    string). The error message includes the path so the operator can fix
    the right Vault entry; secret values are never quoted.
    """
    if shutil.which(vault_binary) is None:
        raise VaultReadError(
            f"`{vault_binary}` CLI not found on PATH. Install it from "
            "https://developer.hashicorp.com/vault/install and run `vault login` first."
        )

    if not path.startswith(_KV_PATH_PREFIX):
        raise VaultReadError(
            f"Vault path {path!r} must start with {_KV_PATH_PREFIX!r}. "
            "Use the layout `secrets/kv/minds/<tier>/<service>`."
        )
    relative = path[len(_KV_PATH_PREFIX) :].lstrip("/")
    if not relative:
        raise VaultReadError(f"Vault path {path!r} has no trailing key after the mount prefix.")

    command = [vault_binary, "kv", "get", "-format=json", f"-mount={_DEFAULT_MOUNT}", relative]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise VaultReadError(f"Failed to invoke {vault_binary}: {exc}") from exc

    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise VaultReadError(
            f"`{vault_binary} kv get {relative}` failed (exit {result.returncode}): {stderr}. "
            f"Check that `vault login` succeeded and that the path exists at mount '{_DEFAULT_MOUNT}/'."
        )

    try:
        parsed = json.loads(result.stdout)
    except ValueError as exc:
        raise VaultReadError(f"`{vault_binary} kv get {relative}` returned non-JSON output: {exc}") from exc

    data = parsed.get("data") if isinstance(parsed, dict) else None
    inner = data.get("data") if isinstance(data, dict) else None
    if not isinstance(inner, dict):
        raise VaultReadError(
            f"`{vault_binary} kv get {relative}` returned no data.data dict; payload shape: {type(parsed).__name__}"
        )

    result_map: dict[str, str] = {}
    for key, value in inner.items():
        if not isinstance(key, str):
            raise VaultReadError(f"Vault entry {path!r} has a non-string key: {key!r}")
        if not isinstance(value, str):
            raise VaultReadError(
                f"Vault entry {path!r} key {key!r} has a non-string value of type {type(value).__name__}. "
                "Every value in a minds-deploy-secret Vault entry must be a string."
            )
        result_map[key] = value
    return result_map
