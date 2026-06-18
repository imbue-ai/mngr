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
import os
import shutil
from typing import Final

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.envs.primitives import VaultReadError
from imbue.minds.envs.primitives import VaultSecretNotFoundError

VAULT_BINARY: Final[str] = "vault"
# `vault kv get` exits 2 when the path holds no secret (vs 1 / other for auth,
# connectivity, permission failures). Callers use this to tell "not found"
# (safe to treat as absent) from a transient error (must not be swallowed).
_VAULT_NOT_FOUND_EXIT_CODE: Final[int] = 2
_DEFAULT_MOUNT: Final[str] = "secrets"
_KV_PATH_PREFIX: Final[str] = "secrets/"
_DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0
_DEFAULT_VAULT_ADDR: Final[str] = "https://vault-cluster-public-vault-df29b16f.9b573ab7.z1.hashicorp.cloud:8200"
_DEFAULT_VAULT_NAMESPACE: Final[str] = "admin"


class VaultPath(str):
    """A Vault KV path of the form ``secrets/...`` (mount/secret-key).

    Subclassed from str so we can pass it directly as a CLI arg without an
    explicit cast, while still type-tagging the input contract.
    """


def read_vault_kv(
    path: VaultPath,
    *,
    parent_concurrency_group: ConcurrencyGroup | None = None,
    vault_binary: str = VAULT_BINARY,
) -> dict[str, str]:
    """Return the ``data.data`` dict of the KV-v2 secret at ``path``.

    ``path`` looks like ``secrets/minds/production/cloudflare``. We strip
    the ``secrets/`` mount prefix off the front and pass the rest to
    ``vault kv get -format=json -mount=secrets <rest>`` so the user's existing
    ``VAULT_ADDR`` / ``VAULT_NAMESPACE`` / token configuration is honored.

    The ``vault`` invocation is routed through a :class:`ConcurrencyGroup`
    so the spawned subprocess is bracketed by managed cleanup. When the
    caller does not have a CG of their own (e.g. a one-shot deploy script
    or test fixture), we synthesize a fresh CG for the duration of the
    call.

    Raises :class:`VaultReadError` for any failure (CLI missing, command
    failed, output not parseable, no ``data.data`` field, value not a
    string).
    """
    if shutil.which(vault_binary) is None:
        raise VaultReadError(
            f"`{vault_binary}` CLI not found on PATH. Install it from "
            "https://developer.hashicorp.com/vault/install and run `vault login` first."
        )

    if not path.startswith(_KV_PATH_PREFIX):
        raise VaultReadError(
            f"Vault path {path!r} must start with {_KV_PATH_PREFIX!r}. "
            "Use the layout `secrets/minds/<tier>/<service>`."
        )
    relative = path[len(_KV_PATH_PREFIX) :].lstrip("/")
    if not relative:
        raise VaultReadError(f"Vault path {path!r} has no trailing key after the mount prefix.")

    command = [vault_binary, "kv", "get", "-format=json", f"-mount={_DEFAULT_MOUNT}", relative]
    # Default VAULT_ADDR and VAULT_NAMESPACE to the imbue HCP cluster values
    # so the helper works in a shell that hasn't exported them. Operator
    # overrides via env take precedence.
    subprocess_env = dict(os.environ)
    subprocess_env.setdefault("VAULT_ADDR", _DEFAULT_VAULT_ADDR)
    subprocess_env.setdefault("VAULT_NAMESPACE", _DEFAULT_VAULT_NAMESPACE)
    parent_cg = (
        parent_concurrency_group if parent_concurrency_group is not None else ConcurrencyGroup(name="vault-kv-get")
    )
    cg = (
        parent_cg.make_concurrency_group(name="vault-kv-get-child")
        if parent_concurrency_group is not None
        else parent_cg
    )
    try:
        with cg:
            result = cg.run_process_to_completion(
                command=command,
                timeout=_DEFAULT_TIMEOUT_SECONDS,
                is_checked_after=False,
                env=subprocess_env,
            )
    except OSError as exc:
        raise VaultReadError(f"Failed to invoke {vault_binary}: {exc}") from exc

    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        if result.returncode == _VAULT_NOT_FOUND_EXIT_CODE:
            # The path genuinely has no secret. Distinct error type so callers
            # can treat "absent" as empty without also swallowing transient /
            # auth failures (which use other exit codes).
            raise VaultSecretNotFoundError(
                f"No secret found at Vault path {path!r} (exit {result.returncode}): {stderr}."
            )
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


def write_vault_kv(
    path: VaultPath,
    values: dict[str, str],
    *,
    parent_concurrency_group: ConcurrencyGroup | None = None,
    vault_binary: str = VAULT_BINARY,
) -> None:
    """Write a flat ``{key: value}`` dict to the KV-v2 entry at ``path``.

    Used by :mod:`imbue.minds.envs.generation` to write the tier
    generation ID. Mirrors :func:`read_vault_kv`'s subprocess wrapping
    + auth inheritance. Refuses to pass any value containing the ``@``
    sigil since ``vault kv put`` would interpret it as a file-path
    reference -- callers that need to write such values should add a
    JSON-stdin variant (see ``scripts/push_vault_from_file.py``).
    """
    if shutil.which(vault_binary) is None:
        raise VaultReadError(
            f"`{vault_binary}` CLI not found on PATH. Install it from "
            "https://developer.hashicorp.com/vault/install and run `vault login` first."
        )
    if not path.startswith(_KV_PATH_PREFIX):
        raise VaultReadError(
            f"Vault path {path!r} must start with {_KV_PATH_PREFIX!r}. "
            "Use the layout `secrets/minds/<tier>/<service>`."
        )
    relative = path[len(_KV_PATH_PREFIX) :].lstrip("/")
    if not relative:
        raise VaultReadError(f"Vault path {path!r} has no trailing key after the mount prefix.")
    for key, value in values.items():
        if value.startswith("@"):
            raise VaultReadError(
                f"write_vault_kv cannot write the value for {key!r}: it begins with the `@` "
                "sigil that `vault kv put` interprets as a file-path reference. Use a "
                "JSON-stdin variant or strip the leading `@`."
            )

    command = [
        vault_binary,
        "kv",
        "put",
        "-format=json",
        f"-mount={_DEFAULT_MOUNT}",
        relative,
        *(f"{k}={v}" for k, v in values.items()),
    ]
    subprocess_env = dict(os.environ)
    subprocess_env.setdefault("VAULT_ADDR", _DEFAULT_VAULT_ADDR)
    subprocess_env.setdefault("VAULT_NAMESPACE", _DEFAULT_VAULT_NAMESPACE)
    parent_cg = (
        parent_concurrency_group if parent_concurrency_group is not None else ConcurrencyGroup(name="vault-kv-put")
    )
    cg = (
        parent_cg.make_concurrency_group(name="vault-kv-put-child")
        if parent_concurrency_group is not None
        else parent_cg
    )
    try:
        with cg:
            result = cg.run_process_to_completion(
                command=command,
                timeout=_DEFAULT_TIMEOUT_SECONDS,
                is_checked_after=False,
                env=subprocess_env,
            )
    except OSError as exc:
        raise VaultReadError(f"Failed to invoke {vault_binary}: {exc}") from exc
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise VaultReadError(f"`{vault_binary} kv put {relative}` failed (exit {result.returncode}): {stderr}")


def delete_vault_kv(
    path: VaultPath,
    *,
    parent_concurrency_group: ConcurrencyGroup | None = None,
    vault_binary: str = VAULT_BINARY,
) -> None:
    """Delete the KV-v2 entry at ``path``.

    Idempotent: a 404 / "not found" is treated as success so re-running
    destroy after a partial failure is safe.
    """
    if shutil.which(vault_binary) is None:
        raise VaultReadError(
            f"`{vault_binary}` CLI not found on PATH. Install it from "
            "https://developer.hashicorp.com/vault/install and run `vault login` first."
        )
    if not path.startswith(_KV_PATH_PREFIX):
        raise VaultReadError(
            f"Vault path {path!r} must start with {_KV_PATH_PREFIX!r}. "
            "Use the layout `secrets/minds/<tier>/<service>`."
        )
    relative = path[len(_KV_PATH_PREFIX) :].lstrip("/")
    if not relative:
        raise VaultReadError(f"Vault path {path!r} has no trailing key after the mount prefix.")

    command = [vault_binary, "kv", "metadata", "delete", f"-mount={_DEFAULT_MOUNT}", relative]
    subprocess_env = dict(os.environ)
    subprocess_env.setdefault("VAULT_ADDR", _DEFAULT_VAULT_ADDR)
    subprocess_env.setdefault("VAULT_NAMESPACE", _DEFAULT_VAULT_NAMESPACE)
    parent_cg = (
        parent_concurrency_group if parent_concurrency_group is not None else ConcurrencyGroup(name="vault-kv-delete")
    )
    cg = (
        parent_cg.make_concurrency_group(name="vault-kv-delete-child")
        if parent_concurrency_group is not None
        else parent_cg
    )
    try:
        with cg:
            result = cg.run_process_to_completion(
                command=command,
                timeout=_DEFAULT_TIMEOUT_SECONDS,
                is_checked_after=False,
                env=subprocess_env,
            )
    except OSError as exc:
        raise VaultReadError(f"Failed to invoke {vault_binary}: {exc}") from exc
    if result.returncode == 0:
        return
    if result.returncode == _VAULT_NOT_FOUND_EXIT_CODE:
        # The entry is already absent. `vault kv metadata delete` returns the
        # same "no value at this path" exit code as `kv get`, so re-running
        # destroy after a partial failure is safe. Branch on the exit code
        # (mirroring read_vault_kv) rather than substring-matching the output,
        # which could mask an unrelated failure whose text happens to contain
        # "not found" / "404".
        return
    stderr = result.stderr.strip() or result.stdout.strip()
    raise VaultReadError(f"`{vault_binary} kv metadata delete {relative}` failed (exit {result.returncode}): {stderr}")
