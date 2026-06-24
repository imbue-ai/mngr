"""Thin wrapper around the ``vault`` CLI.

Secrets use a "split" KV-v2 layout: each logical service entry is a Vault
*directory* whose children are one-field leaf secrets. A service like
``secrets/minds/ci/litellm`` is laid out as::

    secrets/minds/ci/litellm/ANTHROPIC_API_KEY   -> {"value": "sk-ant-..."}
    secrets/minds/ci/litellm/DATABASE_URL        -> {"value": "postgres://..."}

Deploy scripts call ``read_vault_kv(path)`` with the *directory* path
(``secrets/minds/ci/litellm``) and get back the flat ``{key: value}`` dict
reconstructed from its leaf children. We shell out to the locally-installed
``vault`` CLI (``vault kv list`` + ``vault kv get``) rather than using hvac so
authentication piggybacks on whatever the operator already set up (``vault
login``, ``VAULT_ADDR``, ``VAULT_NAMESPACE``, etc.) -- no token plumbing inside
this codebase.

The returned values are kept in process memory only; no file is written.
"""

import contextlib
import json
import os
import shutil
from collections.abc import Iterator
from collections.abc import Mapping
from typing import Final

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.minds.envs.primitives import VaultReadError
from imbue.minds.envs.primitives import VaultSecretNotFoundError

VAULT_BINARY: Final[str] = "vault"
# `vault kv get` / `vault kv list` exit 2 when the path holds no secret (vs 1 /
# other for auth, connectivity, permission failures). Callers use this to tell
# "not found" (safe to treat as absent) from a transient error (must not be
# swallowed).
_VAULT_NOT_FOUND_EXIT_CODE: Final[int] = 2
_DEFAULT_MOUNT: Final[str] = "secrets"
_KV_PATH_PREFIX: Final[str] = "secrets/"
_DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0
_DEFAULT_VAULT_ADDR: Final[str] = "https://vault-cluster-public-vault-df29b16f.9b573ab7.z1.hashicorp.cloud:8200"
_DEFAULT_VAULT_NAMESPACE: Final[str] = "admin"
# In the split layout every logical key is its own KV-v2 leaf holding a single
# field named ``value``.
_SECRET_VALUE_FIELD: Final[str] = "value"


class VaultPath(str):
    """A Vault KV directory path of the form ``secrets/...`` (mount/service-dir).

    Subclassed from str so we can pass it directly as a CLI arg without an
    explicit cast, while still type-tagging the input contract.
    """


def read_vault_kv(
    path: VaultPath,
    *,
    parent_concurrency_group: ConcurrencyGroup | None = None,
    vault_binary: str = VAULT_BINARY,
) -> dict[str, str]:
    """Return the ``{key: value}`` dict reconstructed from the leaf children of ``path``.

    ``path`` is the service *directory*, e.g.
    ``secrets/minds/production/cloudflare``. We list its leaf children and
    read each child's single ``value`` field, returning a flat
    ``{child_name: value}`` dict so the user's existing ``VAULT_ADDR`` /
    ``VAULT_NAMESPACE`` / token configuration is honored.

    The ``vault`` invocations are routed through a :class:`ConcurrencyGroup`
    so the spawned subprocesses are bracketed by managed cleanup. When the
    caller does not have a CG of their own (e.g. a one-shot deploy script
    or test fixture), we synthesize a fresh CG for the duration of the
    call.

    Raises :class:`VaultSecretNotFoundError` when the directory itself is
    absent, and :class:`VaultReadError` for any other failure (CLI missing,
    command failed, output not parseable, a leaf missing its ``value``
    field).
    """
    _check_vault_binary(vault_binary)
    relative = _relative_kv_path(path)
    keys = _list_leaf_keys(
        relative, path=path, parent=parent_concurrency_group, vault_binary=vault_binary, allow_missing=False
    )
    result_map: dict[str, str] = {}
    for key in keys:
        result_map[key] = _read_leaf_value(
            relative, key, path=path, parent=parent_concurrency_group, vault_binary=vault_binary
        )
    return result_map


def write_vault_kv(
    path: VaultPath,
    values: Mapping[str, str],
    *,
    parent_concurrency_group: ConcurrencyGroup | None = None,
    vault_binary: str = VAULT_BINARY,
) -> None:
    """Write each ``{key: value}`` pair as a single-``value`` leaf under ``path``.

    Used by :mod:`imbue.minds.envs.generation` to write the tier
    generation ID. Each key becomes its own leaf ``path/<key>`` holding
    ``{"value": <value>}``. Refuses to pass any value containing the ``@``
    sigil since ``vault kv put`` would interpret it as a file-path
    reference -- callers that need to write such values should add a
    JSON-stdin variant (see ``scripts/push_vault_from_file.py``).
    """
    _check_vault_binary(vault_binary)
    relative = _relative_kv_path(path)
    for key, value in values.items():
        if value.startswith("@"):
            raise VaultReadError(
                f"write_vault_kv cannot write the value for {key!r}: it begins with the `@` "
                "sigil that `vault kv put` interprets as a file-path reference. Use a "
                "JSON-stdin variant or strip the leading `@`."
            )
    for key, value in values.items():
        leaf_relative = f"{relative}/{key}"
        command = [
            vault_binary,
            "kv",
            "put",
            "-format=json",
            f"-mount={_DEFAULT_MOUNT}",
            leaf_relative,
            f"{_SECRET_VALUE_FIELD}={value}",
        ]
        result = _run_vault_command(command, parent=parent_concurrency_group, vault_binary=vault_binary)
        if result.returncode != 0:
            stderr = result.stderr.strip() or result.stdout.strip()
            raise VaultReadError(
                f"`{vault_binary} kv put {leaf_relative}` failed (exit {result.returncode}): {stderr}"
            )


def delete_vault_kv(
    path: VaultPath,
    *,
    parent_concurrency_group: ConcurrencyGroup | None = None,
    vault_binary: str = VAULT_BINARY,
) -> None:
    """Delete every leaf child under ``path``.

    Idempotent: a missing directory (or a leaf that 404s mid-loop) is
    treated as success so re-running destroy after a partial failure is
    safe.
    """
    _check_vault_binary(vault_binary)
    relative = _relative_kv_path(path)
    keys = _list_leaf_keys(
        relative, path=path, parent=parent_concurrency_group, vault_binary=vault_binary, allow_missing=True
    )
    for key in keys:
        leaf_relative = f"{relative}/{key}"
        command = [vault_binary, "kv", "metadata", "delete", f"-mount={_DEFAULT_MOUNT}", leaf_relative]
        result = _run_vault_command(command, parent=parent_concurrency_group, vault_binary=vault_binary)
        if result.returncode == 0:
            continue
        message = (result.stderr + result.stdout).lower()
        if "not found" in message or "no value found" in message or "404" in message:
            continue
        stderr = result.stderr.strip() or result.stdout.strip()
        raise VaultReadError(
            f"`{vault_binary} kv metadata delete {leaf_relative}` failed (exit {result.returncode}): {stderr}"
        )


def _check_vault_binary(vault_binary: str) -> None:
    """Raise :class:`VaultReadError` if the ``vault`` CLI is not on PATH."""
    if shutil.which(vault_binary) is None:
        raise VaultReadError(
            f"`{vault_binary}` CLI not found on PATH. Install it from "
            "https://developer.hashicorp.com/vault/install and run `vault login` first."
        )


def _relative_kv_path(path: VaultPath) -> str:
    """Strip the ``secrets/`` mount prefix off ``path``, validating its shape."""
    if not path.startswith(_KV_PATH_PREFIX):
        raise VaultReadError(
            f"Vault path {path!r} must start with {_KV_PATH_PREFIX!r}. "
            "Use the layout `secrets/minds/<tier>/<service>`."
        )
    relative = path[len(_KV_PATH_PREFIX) :].lstrip("/")
    if not relative:
        raise VaultReadError(f"Vault path {path!r} has no trailing key after the mount prefix.")
    return relative


def _vault_subprocess_env() -> dict[str, str]:
    """Return the process env with the imbue HCP cluster defaults filled in.

    Operator overrides via env take precedence so the helper works in a
    shell that hasn't exported ``VAULT_ADDR`` / ``VAULT_NAMESPACE``.
    """
    subprocess_env = dict(os.environ)
    subprocess_env.setdefault("VAULT_ADDR", _DEFAULT_VAULT_ADDR)
    subprocess_env.setdefault("VAULT_NAMESPACE", _DEFAULT_VAULT_NAMESPACE)
    return subprocess_env


@contextlib.contextmanager
def _vault_concurrency_group(parent: ConcurrencyGroup | None, *, name: str) -> Iterator[ConcurrencyGroup]:
    """Yield a CG to run vault subprocesses in: a child of ``parent``, or a fresh one."""
    cg = parent.make_concurrency_group(name=name) if parent is not None else ConcurrencyGroup(name=name)
    with cg:
        yield cg


def _run_vault_command(command: list[str], *, parent: ConcurrencyGroup | None, vault_binary: str) -> FinishedProcess:
    """Run a single ``vault`` subprocess to completion in its own CG scope, wrapping spawn failures.

    Each command gets its own (child or fresh) concurrency group and the
    result is returned *after* the CG exits, so callers can validate the
    output and raise plain domain exceptions without them being wrapped in
    a :class:`ConcurrencyExceptionGroup` by the CG's ``__exit__``.
    """
    try:
        with _vault_concurrency_group(parent, name="vault-kv") as cg:
            result = cg.run_process_to_completion(
                command=command,
                timeout=_DEFAULT_TIMEOUT_SECONDS,
                is_checked_after=False,
                env=_vault_subprocess_env(),
            )
    except OSError as exc:
        raise VaultReadError(f"Failed to invoke {vault_binary}: {exc}") from exc
    return result


def _list_leaf_keys(
    relative: str,
    *,
    path: VaultPath,
    parent: ConcurrencyGroup | None,
    vault_binary: str,
    # When True, a genuinely-absent directory yields an empty list instead of
    # raising (used by the idempotent delete path).
    allow_missing: bool,
) -> list[str]:
    """List the leaf child names under the directory at ``relative``."""
    command = [vault_binary, "kv", "list", "-format=json", f"-mount={_DEFAULT_MOUNT}", relative]
    result = _run_vault_command(command, parent=parent, vault_binary=vault_binary)
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        if result.returncode == _VAULT_NOT_FOUND_EXIT_CODE:
            if allow_missing:
                return []
            raise VaultSecretNotFoundError(
                f"No secret found at Vault path {path!r} (exit {result.returncode}): {stderr}."
            )
        raise VaultReadError(
            f"`{vault_binary} kv list {relative}` failed (exit {result.returncode}): {stderr}. "
            f"Check that `vault login` succeeded and that the path exists at mount '{_DEFAULT_MOUNT}/'."
        )

    try:
        parsed = json.loads(result.stdout)
    except ValueError as exc:
        raise VaultReadError(f"`{vault_binary} kv list {relative}` returned non-JSON output: {exc}") from exc
    if not isinstance(parsed, list):
        raise VaultReadError(
            f"`{vault_binary} kv list {relative}` returned a {type(parsed).__name__}, expected a JSON array of key names."
        )

    keys: list[str] = []
    for entry in parsed:
        if not isinstance(entry, str):
            raise VaultReadError(f"`{vault_binary} kv list {relative}` returned a non-string entry: {entry!r}")
        # A trailing slash marks a nested directory rather than a leaf secret.
        # The split layout is exactly one level deep, so a nested directory
        # means the entry holds no readable `value` here.
        if entry.endswith("/"):
            raise VaultReadError(
                f"Vault path {path!r} contains a nested directory {entry!r}; the split-secret "
                "layout must be flat (one `value` leaf per key)."
            )
        keys.append(entry)
    return keys


def _read_leaf_value(
    relative: str,
    key: str,
    *,
    path: VaultPath,
    parent: ConcurrencyGroup | None,
    vault_binary: str,
) -> str:
    """Read the single ``value`` field of the leaf secret at ``relative/key``."""
    leaf_relative = f"{relative}/{key}"
    command = [vault_binary, "kv", "get", "-format=json", f"-mount={_DEFAULT_MOUNT}", leaf_relative]
    result = _run_vault_command(command, parent=parent, vault_binary=vault_binary)
    if result.returncode != 0:
        # The key was just listed, so a failure reading it now is a real error
        # (race / auth / connectivity), never an expected "absent".
        stderr = result.stderr.strip() or result.stdout.strip()
        raise VaultReadError(f"`{vault_binary} kv get {leaf_relative}` failed (exit {result.returncode}): {stderr}.")

    try:
        parsed = json.loads(result.stdout)
    except ValueError as exc:
        raise VaultReadError(f"`{vault_binary} kv get {leaf_relative}` returned non-JSON output: {exc}") from exc

    data = parsed.get("data") if isinstance(parsed, dict) else None
    inner = data.get("data") if isinstance(data, dict) else None
    if not isinstance(inner, dict):
        raise VaultReadError(
            f"`{vault_binary} kv get {leaf_relative}` returned no data.data dict; payload shape: {type(parsed).__name__}"
        )
    value = inner.get(_SECRET_VALUE_FIELD)
    if not isinstance(value, str):
        raise VaultReadError(
            f"Vault entry {path!r}/{key} has no string {_SECRET_VALUE_FIELD!r} field "
            f"(got {type(value).__name__}). Every key in the split-secret layout must be a "
            "single-`value` leaf."
        )
    return value
