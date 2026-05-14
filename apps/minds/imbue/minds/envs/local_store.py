"""Read / write / delete ``~/.<root>/envs/<name>.toml``.

Per-dev-env override files are self-contained snapshots: a full
``ClientEnvConfig`` (connector_url, litellm_proxy_url) plus a ``[secrets]``
subtable carrying any provider state ``minds env create`` needs to remember
locally (Neon DSN, SuperTokens app id, etc.). The file is ``chmod 600`` so
secrets stay tucked away.
"""

from collections.abc import Mapping
from pathlib import Path

import tomlkit
from pydantic import AnyUrl
from pydantic import Field
from pydantic import SecretStr

from imbue.minds.config.data_types import ClientEnvConfig
from imbue.minds.envs.paths import dev_env_file
from imbue.minds.envs.paths import dev_envs_dir
from imbue.minds.envs.primitives import DevEnvAlreadyExistsError
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.envs.primitives import DevEnvNotFoundError


class LocalDevEnvConfig(ClientEnvConfig):
    """Self-contained snapshot of a dynamic dev environment.

    Extends :class:`ClientEnvConfig` (so the URLs sit at the top level of
    the TOML and ``minds run --config-file`` can read the file directly)
    with a ``[secrets]`` subtable for values that are not necessarily
    URLs (Neon connection string, SuperTokens core API key, etc.) and
    that should be ``chmod 600``-protected.

    The dev env's name is the filename stem (``<name>.toml``) and is not
    stored inside the file -- this keeps the on-disk shape a strict
    superset of a tier ``client.toml``.
    """

    secrets: Mapping[str, SecretStr] = Field(
        default_factory=dict,
        description=(
            "Per-dev-env provider state that the operator may need at deploy / debug time. "
            "Stored in the same TOML; the file's mode is 600 so this stays out of casual reads."
        ),
    )


def write_dev_env_file(
    config: LocalDevEnvConfig,
    *,
    name: DevEnvName,
    root_name: str | None = None,
    overwrite: bool = False,
) -> Path:
    """Serialize ``config`` to ``~/.<root>/envs/<name>.toml`` with mode 0o600.

    The file is written as a strict superset of a tier ``client.toml``
    (``connector_url`` and ``litellm_proxy_url`` at the top level, plus
    an optional ``[secrets]`` subtable) so the same file is consumable
    by ``minds run --config-file``.

    Refuses to overwrite an existing file unless ``overwrite=True``; that
    keeps ``minds env create`` from silently clobbering a dev env that was
    already provisioned.
    """
    target = dev_env_file(name, root_name=root_name)
    if target.exists() and not overwrite:
        raise DevEnvAlreadyExistsError(
            f"Dev env file {target} already exists. Run `minds env destroy {name}` first or pass --force to overwrite."
        )
    target.parent.mkdir(parents=True, exist_ok=True)

    doc = tomlkit.document()
    doc["connector_url"] = str(config.connector_url)
    doc["litellm_proxy_url"] = str(config.litellm_proxy_url)
    if config.secrets:
        secrets_block = tomlkit.table()
        for key, secret in config.secrets.items():
            secrets_block[key] = secret.get_secret_value()
        doc["secrets"] = secrets_block

    tmp = target.with_suffix(".tmp")
    tmp.write_text(tomlkit.dumps(doc))
    tmp.chmod(0o600)
    tmp.rename(target)
    return target


def read_dev_env_file(name: DevEnvName, *, root_name: str | None = None) -> LocalDevEnvConfig:
    """Read ``~/.<root>/envs/<name>.toml`` back into a :class:`LocalDevEnvConfig`.

    The dev env's name is taken from the filename stem, not from inside
    the file.

    Raises :class:`DevEnvNotFoundError` when the file does not exist.
    """
    path = dev_env_file(name, root_name=root_name)
    if not path.is_file():
        raise DevEnvNotFoundError(f"No dev env file found for {name!r}: expected {path}")
    raw = tomlkit.loads(path.read_text())

    secrets_section = raw.get("secrets") or {}
    secrets: dict[str, SecretStr] = {}
    for key, value in secrets_section.items():
        secrets[str(key)] = SecretStr(str(value))

    return LocalDevEnvConfig(
        connector_url=AnyUrl(str(raw["connector_url"])),
        litellm_proxy_url=AnyUrl(str(raw["litellm_proxy_url"])),
        secrets=secrets,
    )


def delete_dev_env_file(name: DevEnvName, *, root_name: str | None = None) -> bool:
    """Remove ``~/.<root>/envs/<name>.toml``.

    Returns ``True`` if the file was removed, ``False`` if it did not exist
    (so callers can treat re-destroys as no-ops).
    """
    path = dev_env_file(name, root_name=root_name)
    if not path.is_file():
        return False
    path.unlink()
    return True


def list_dev_env_files(*, root_name: str | None = None) -> tuple[Path, ...]:
    """Return the sorted list of ``*.toml`` files under ``~/.<root>/envs/``."""
    base = dev_envs_dir(root_name=root_name)
    if not base.is_dir():
        return ()
    return tuple(sorted(p for p in base.iterdir() if p.is_file() and p.suffix == ".toml"))
