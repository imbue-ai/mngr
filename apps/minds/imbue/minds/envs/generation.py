"""Per-tier generation id lifecycle.

The "generation id" is a uuid stored at ``secrets/minds/<tier>/generation``
in HCP Vault, used to detect "the tier server got destroyed + redeployed
since I last activated it".

* ``minds env deploy --yes-i-mean-<tier>`` calls :func:`ensure_generation_id`,
  which mints a fresh uuid and writes it to Vault when one doesn't already
  exist. The same uuid then gets pushed to Modal as part of the
  per-tier Modal Secret push so the connector sees it as
  ``MINDS_TIER_GENERATION_ID`` at runtime.
* The connector exposes ``/generation`` returning the embedded uuid.
* ``minds env destroy --yes-i-mean-<tier>`` calls :func:`delete_generation_id`,
  which removes the Vault entry. The next ``deploy`` therefore mints a
  *new* uuid -- subsequent activations across all developers' shells
  see the changed uuid and know their local state is stale.
* ``minds env activate <tier>`` (on the dev's machine) fetches
  ``<connector_url>/generation``, compares against the per-env
  ``last_seen_generation`` marker under the env root, and wipes the
  env's mngr profile + auth state on mismatch so the dev starts clean.

Why a separate Vault entry and not a field in an existing one: the
generation id is the only value here whose lifecycle is owned by the
deploy / destroy flow itself (every other Vault entry is operator-
populated and survives a destroy/deploy cycle). Keeping it isolated
makes the lifecycle invariants ("created by deploy when missing,
deleted by destroy unconditionally") easy to read off the code.
"""

from typing import Final
from uuid import uuid4

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.envs.primitives import VaultSecretNotFoundError
from imbue.minds.envs.vault_reader import VAULT_BINARY
from imbue.minds.envs.vault_reader import VaultPath
from imbue.minds.envs.vault_reader import delete_vault_kv
from imbue.minds.envs.vault_reader import read_vault_kv
from imbue.minds.envs.vault_reader import write_vault_kv

# Vault key inside the generation entry. Also the env var name the
# deployed connector reads at startup.
GENERATION_ID_KEY: Final[str] = "MINDS_TIER_GENERATION_ID"
# Trailing path segment under the tier's vault prefix.
_GENERATION_VAULT_LEAF: Final[str] = "generation"


def _generation_vault_path(tier_vault_prefix: str) -> VaultPath:
    """Return ``secrets/minds/<tier>/generation`` for the given tier prefix."""
    return VaultPath(f"{tier_vault_prefix.rstrip('/')}/{_GENERATION_VAULT_LEAF}")


def read_generation_id(
    tier_vault_prefix: str,
    *,
    parent_concurrency_group: ConcurrencyGroup,
    vault_binary: str = VAULT_BINARY,
) -> str | None:
    """Read the tier's generation id from Vault. Returns ``None`` if the entry doesn't exist.

    Used by :func:`ensure_generation_id` to decide whether to mint a
    new one, and could be used by external read paths (e.g. a doctor
    command) without going through ``ensure``.
    """
    try:
        values = read_vault_kv(
            _generation_vault_path(tier_vault_prefix),
            parent_concurrency_group=parent_concurrency_group,
            vault_binary=vault_binary,
        )
    except VaultSecretNotFoundError:
        # Treat a genuinely-absent entry as "no id yet"; any other Vault
        # failure propagates so the operator notices problems early.
        return None
    return values.get(GENERATION_ID_KEY)


def ensure_generation_id(
    tier_vault_prefix: str,
    *,
    parent_concurrency_group: ConcurrencyGroup,
    vault_binary: str = VAULT_BINARY,
) -> str:
    """Return the tier's generation id, minting + writing a new one if missing.

    Called by ``deploy_tier_env`` so every successful deploy emits a
    generation id that the connector then exposes via ``/generation``.
    Idempotent: re-running ``deploy`` does NOT mint a new id when one
    already exists -- the id only rolls when destroy removes the
    entry.
    """
    existing = read_generation_id(
        tier_vault_prefix,
        parent_concurrency_group=parent_concurrency_group,
        vault_binary=vault_binary,
    )
    if existing is not None:
        return existing
    new_id = uuid4().hex
    write_vault_kv(
        _generation_vault_path(tier_vault_prefix),
        {GENERATION_ID_KEY: new_id},
        parent_concurrency_group=parent_concurrency_group,
        vault_binary=vault_binary,
    )
    return new_id


def delete_generation_id(
    tier_vault_prefix: str,
    *,
    parent_concurrency_group: ConcurrencyGroup,
    vault_binary: str = VAULT_BINARY,
) -> None:
    """Remove the tier's generation entry from Vault. Idempotent.

    Called by ``destroy_tier_env`` so the next deploy mints a fresh id
    (which all developers' next activation against the tier will see
    as a mismatch + trigger local-state wipe).
    """
    delete_vault_kv(
        _generation_vault_path(tier_vault_prefix),
        parent_concurrency_group=parent_concurrency_group,
        vault_binary=vault_binary,
    )
