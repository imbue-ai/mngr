"""Read / write the per-env on-disk state for dev envs.

Per-dev-env state is split into two files under ``~/.minds-<env-name>/``:

* ``client.toml`` (mode 0644) -- non-secret config (connector + LiteLLM
  URLs). Shape is exactly :class:`ClientEnvConfig`, so the same file
  can be passed to ``minds run --config-file`` directly. ``write_client_config``
  refuses to serialize anything other than the URL fields so a dev
  ``client.toml`` is shape-identical to a staging / production one.
* ``secrets.toml`` (mode 0600) -- the values ``minds env deploy``
  generated on this machine (Neon DSN, SuperTokens connection URI,
  SuperTokens API key). Staging / production fetch the same values from
  Vault at deploy time, so they never write or read this file.

The "two files" split is what keeps secrets out of the in-repo
``apps/minds/imbue/minds/config/envs/<tier>/client.toml`` (committed
for staging / production): the public file's type doesn't have a
secrets slot, and the write helpers route public vs secret values to
two different paths.
"""

import shutil
import tomllib
from collections.abc import Mapping
from pathlib import Path

import tomlkit
from pydantic import ConfigDict
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.config.data_types import ClientEnvConfig
from imbue.minds.config.loader import load_client_config
from imbue.minds.envs.paths import client_config_file
from imbue.minds.envs.paths import env_root_dir
from imbue.minds.envs.paths import secrets_file
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.envs.primitives import DevEnvNotFoundError
from imbue.minds.errors import MindError


class InvalidDevEnvSecretsError(MindError):
    """Raised when ``~/.minds-<env-name>/secrets.toml`` is malformed."""


class DevEnvSecretsModel(FrozenModel):
    """The chmod-0600 ``secrets.toml`` under ``~/.minds-<env-name>/``.

    Dev-env-only: staging / production fetch the same values from Vault
    at deploy time. The shape is a flat ``[secrets]`` table so every
    value is a :class:`SecretStr` and never leaks through ``repr``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=False)

    secrets: Mapping[str, SecretStr] = Field(
        default_factory=dict,
        description=(
            "Per-dev-env provider state ``minds env deploy`` needs on re-runs. "
            "Stored in ``~/.minds-<env-name>/secrets.toml`` with mode 0600 so it "
            "stays out of casual reads."
        ),
    )


def write_client_config(
    config: ClientEnvConfig,
    *,
    name: DevEnvName,
) -> Path:
    """Serialize ``config`` to ``~/.minds-<name>/client.toml`` with mode 0o644.

    Overwrites any existing file -- ``minds env deploy`` is idempotent
    by design and re-running it must update the on-disk URLs in place.
    The file shape is exactly :class:`ClientEnvConfig` (no ``[secrets]``
    block) so secrets cannot accidentally land here; the dev-env-only
    ``secrets.toml`` next to it carries those values instead.

    Creates ``~/.minds-<name>/`` if it doesn't exist yet. The directory
    is the env root and houses everything else for the env (mngr
    profile, auth, agents, logs) over its lifetime.
    """
    target = client_config_file(name)
    target.parent.mkdir(parents=True, exist_ok=True)

    doc = tomlkit.document()
    doc["connector_url"] = str(config.connector_url)
    doc["litellm_proxy_url"] = str(config.litellm_proxy_url)

    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(tomlkit.dumps(doc))
    tmp.chmod(0o644)
    tmp.rename(target)
    return target


def write_secrets_file(
    secrets: Mapping[str, SecretStr],
    *,
    name: DevEnvName,
) -> Path:
    """Serialize ``secrets`` to ``~/.minds-<name>/secrets.toml`` with mode 0o600.

    Overwrites any existing file. The empty-mapping case still produces
    a file (with an empty ``[secrets]`` table) so subsequent reads have
    deterministic shape. Creates ``~/.minds-<name>/`` if it doesn't exist
    yet.
    """
    target = secrets_file(name)
    target.parent.mkdir(parents=True, exist_ok=True)

    doc = tomlkit.document()
    secrets_block = tomlkit.table()
    for key, value in secrets.items():
        secrets_block[key] = value.get_secret_value()
    doc["secrets"] = secrets_block

    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(tomlkit.dumps(doc))
    tmp.chmod(0o600)
    tmp.rename(target)
    return target


def read_client_config_file(name: DevEnvName) -> ClientEnvConfig:
    """Read ``~/.minds-<name>/client.toml`` back into a :class:`ClientEnvConfig`.

    Raises :class:`DevEnvNotFoundError` when the file does not exist
    (the caller should ``minds env deploy <name>`` first).
    """
    path = client_config_file(name)
    if not path.is_file():
        raise DevEnvNotFoundError(f"No client.toml found for dev env {name!r}: expected {path}")
    return load_client_config(path)


def read_secrets_file(name: DevEnvName) -> DevEnvSecretsModel:
    """Read ``~/.minds-<name>/secrets.toml`` back into a :class:`DevEnvSecretsModel`.

    Returns an empty-secrets model when the file does not exist, so a
    first-time deploy doesn't have to special-case it. Callers that need
    to assert the file exists check explicitly.
    """
    path = secrets_file(name)
    if not path.is_file():
        return DevEnvSecretsModel(secrets={})
    raw = tomllib.loads(path.read_text())
    raw_secrets = raw.get("secrets", {})
    if not isinstance(raw_secrets, dict):
        raise InvalidDevEnvSecretsError(f"{path} [secrets] is not a table: {type(raw_secrets).__name__}")
    typed: dict[str, SecretStr] = {str(k): SecretStr(str(v)) for k, v in raw_secrets.items()}
    return DevEnvSecretsModel(secrets=typed)


def delete_env_root(name: DevEnvName) -> bool:
    """Remove ``~/.minds-<name>/`` (including its files) recursively.

    Returns ``True`` if anything was removed, ``False`` if the dir did
    not exist. Used by ``minds env destroy`` after the provider teardown
    succeeds so subsequent commands fail fast on the dangling activation
    instead of silently re-creating partial state under a half-torn-down
    env root.
    """
    target = env_root_dir(name)
    if not target.exists():
        return False
    shutil.rmtree(target)
    return True


def env_root_exists(name: DevEnvName) -> bool:
    """Return True iff ``~/.minds-<name>/`` exists on disk.

    Used by ``minds env activate <name>`` to refuse activation for dev
    envs that haven't been deployed yet (``staging`` / ``production``
    bypass this check -- their roots are auto-created on activation
    because the in-repo ``client.toml`` is the source of truth).
    """
    return env_root_dir(name).is_dir()


def client_config_exists(name: DevEnvName) -> bool:
    """Return True iff the per-env ``client.toml`` is present under the env root.

    Used by ``minds env list`` to mark each enumerated env root as
    "deployed" (has a ``client.toml``) vs "local-only" (e.g. an
    activated ``staging`` whose URLs live in-repo) vs "production"
    (the special ``~/.minds/`` row).
    """
    return client_config_file(name).is_file()
