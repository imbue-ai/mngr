import importlib.resources
import shlex
from collections.abc import Callable
from collections.abc import MutableMapping
from enum import auto
from pathlib import Path
from typing import Any
from typing import Final
from typing import assert_never

import click
import tomlkit
from loguru import logger
from pydantic import Field
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import InlineTable

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
from imbue.mngr.errors import PluginMngrError
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr_grok import resources as grok_resources

_GROK_TRUST_KEY_SCRIPT_NAME: Final[str] = "grok_trust_key.sh"

TRUSTED_FOLDERS_FILENAME: Final[str] = "trusted_folders.toml"

_FOLDERS_TABLE_NAME: Final[str] = "folders"

_TRUSTED_FIELD_NAME: Final[str] = "trusted"

_TRUST_STORE_FILE_MODE: Final[str] = "0600"

_HOST_COMMAND_TIMEOUT_SECONDS: Final[float] = 30.0

_TRUST_KEY_STATUS_PREFIX: Final[str] = "grok-trust-key-status="

_TRUST_KEY_RECORDABLE_STATUS: Final[str] = "0"

_TRUST_KEY_MISSING_WORK_DIR_STATUS: Final[str] = "1"

_TRUST_KEY_PROVISIONAL_STATUS: Final[str] = "2"


class GrokTrustError(PluginMngrError):
    """Raised when grok's folder trust cannot be resolved on the agent's host."""


class GrokTrustKey(FrozenModel):
    """grok's folder-trust key for a work dir, as resolved on the agent's host."""

    path: Path = Field(description="Canonical absolute folder that grok records trust against")
    is_provisional: bool = Field(
        description="Whether the key is $HOME or /, which grok trusts provisionally without recording anything"
    )


class GrokFolderTrustOutcome(UpperCaseStrEnum):
    """How provisioning settled grok's folder trust for a work dir."""

    PROVISIONAL = auto()
    ALREADY_TRUSTED = auto()
    GRANTED = auto()


class GrokFolderTrustResult(FrozenModel):
    """The trust key of an agent's work dir and how provisioning settled it."""

    trust_key: GrokTrustKey = Field(description="The work dir's trust key")
    outcome: GrokFolderTrustOutcome = Field(description="How the key's trust was settled")


class GrokTrustConsent(UpperCaseStrEnum):
    """What the consent matrix allows for a trust key that grok would ask about."""

    ALREADY_GRANTED = auto()
    PRE_APPROVED = auto()
    ASK_USER = auto()
    REFUSE = auto()


@pure
def decide_grok_trust_consent(
    is_trusted_by_user_store: bool,
    is_auto_dismiss_dialogs_at_startup: bool,
    is_auto_approve: bool,
    is_interactive: bool,
) -> GrokTrustConsent:
    if is_trusted_by_user_store:
        return GrokTrustConsent.ALREADY_GRANTED
    elif is_auto_dismiss_dialogs_at_startup or is_auto_approve:
        return GrokTrustConsent.PRE_APPROVED
    elif is_interactive:
        return GrokTrustConsent.ASK_USER
    else:
        return GrokTrustConsent.REFUSE


def _load_grok_trust_key_script() -> str:
    return importlib.resources.files(grok_resources).joinpath(_GROK_TRUST_KEY_SCRIPT_NAME).read_text()


@pure
def _build_grok_trust_key_command(trust_key_script: str, work_dir: Path, env_prefix: str) -> str:
    """Shell command that runs the trust-key script on the host and appends its exit status as the last line."""
    return (
        f"{env_prefix}env -u BASH_ENV bash -c {shlex.quote(trust_key_script)} grok_trust_key "
        f"{shlex.quote(str(work_dir))}; printf '{_TRUST_KEY_STATUS_PREFIX}%s' \"$?\""
    )


@pure
def parse_grok_trust_key_output(command_stdout: str, work_dir: Path) -> GrokTrustKey:
    key_text, _, status_line = command_stdout.rpartition("\n")
    if not status_line.startswith(_TRUST_KEY_STATUS_PREFIX):
        raise GrokTrustError(
            f"Could not compute grok's trust key for {work_dir}: unexpected output {command_stdout!r}"
        )
    status = status_line.removeprefix(_TRUST_KEY_STATUS_PREFIX)
    if status in (_TRUST_KEY_RECORDABLE_STATUS, _TRUST_KEY_PROVISIONAL_STATUS):
        key_path = Path(key_text)
    elif status == _TRUST_KEY_MISSING_WORK_DIR_STATUS:
        raise GrokTrustError(f"Could not compute grok's trust key: work dir {work_dir} does not exist on the host")
    else:
        raise GrokTrustError(
            f"Could not compute grok's trust key for {work_dir}: the key script exited with status {status}"
        )
    if not key_path.is_absolute():
        raise GrokTrustError(f"Could not compute grok's trust key for {work_dir}: {key_text!r} is not absolute")
    return GrokTrustKey(path=key_path, is_provisional=status == _TRUST_KEY_PROVISIONAL_STATUS)


def compute_grok_trust_key(host: OnlineHostInterface, work_dir: Path, env_prefix: str) -> GrokTrustKey:
    """Compute grok's trust key for work_dir in one host command, with env_prefix supplying the agent's HOME."""
    command = _build_grok_trust_key_command(_load_grok_trust_key_script(), work_dir, env_prefix)
    result = host.execute_idempotent_command(command, timeout_seconds=_HOST_COMMAND_TIMEOUT_SECONDS)
    if not result.success:
        raise GrokTrustError(
            f"Could not compute grok's trust key for {work_dir} on the host (stdout={result.stdout!r}, "
            f"stderr={result.stderr!r})"
        )
    return parse_grok_trust_key_output(result.stdout, work_dir)


@pure
def _parse_trust_store(content: str, store_path: Path) -> tomlkit.TOMLDocument:
    try:
        return tomlkit.parse(content)
    except TOMLKitError as e:
        raise UserInputError(
            f"grok's trust store at {store_path} is not valid TOML ({e}); refusing to overwrite it. "
            "Fix or remove the file by hand, then re-run."
        ) from e


@pure
def _find_folder_entry(
    document: tomlkit.TOMLDocument, trust_key: Path, store_path: Path
) -> MutableMapping[str, Any] | None:
    folders = document.get(_FOLDERS_TABLE_NAME)
    if folders is None:
        return None
    if not isinstance(folders, MutableMapping):
        raise UserInputError(
            f"grok's trust store at {store_path} has a non-table `{_FOLDERS_TABLE_NAME}` value; refusing to "
            "overwrite it. Fix or remove the file by hand, then re-run."
        )
    entry = folders.get(str(trust_key))
    if entry is None:
        return None
    if not isinstance(entry, MutableMapping) or not isinstance(entry.get(_TRUSTED_FIELD_NAME, False), bool):
        raise UserInputError(
            f"grok's trust store at {store_path} has a malformed entry for {str(trust_key)!r}; refusing to "
            "overwrite it. Fix or remove the entry by hand, then re-run."
        )
    return entry


@pure
def is_grok_folder_trusted(store_content: str, trust_key: Path, store_path: Path) -> bool:
    """Whether a trust store's TOML records exactly trust_key with ``trusted = true``."""
    entry = _find_folder_entry(_parse_trust_store(store_content, store_path), trust_key, store_path)
    return entry is not None and entry.get(_TRUSTED_FIELD_NAME) is True


@pure
def merge_grok_folder_grant(store_content: str, trust_key: Path, store_path: Path) -> str | None:
    """Add ``[folders."<trust_key>"] trusted = true`` to a trust store's TOML; None when the grant is already there."""
    document = _parse_trust_store(store_content, store_path)
    entry = _find_folder_entry(document, trust_key, store_path)
    if entry is not None and entry.get(_TRUSTED_FIELD_NAME) is True:
        return None
    if entry is not None:
        entry[_TRUSTED_FIELD_NAME] = True
    else:
        existing_folders = document.get(_FOLDERS_TABLE_NAME)
        grant = tomlkit.inline_table() if isinstance(existing_folders, InlineTable) else tomlkit.table()
        grant[_TRUSTED_FIELD_NAME] = True
        if isinstance(existing_folders, MutableMapping):
            existing_folders[str(trust_key)] = grant
        else:
            new_folders = tomlkit.table(is_super_table=True)
            new_folders[str(trust_key)] = grant
            document[_FOLDERS_TABLE_NAME] = new_folders
    return tomlkit.dumps(document)


def _read_trust_store(host: OnlineHostInterface, store_path: Path) -> str | None:
    try:
        return host.read_text_file(store_path)
    except FileNotFoundError:
        return None


def _write_folder_grant(
    host: OnlineHostInterface, store_path: Path, existing_content: str | None, trust_key: Path, is_user_store: bool
) -> None:
    merged = merge_grok_folder_grant(existing_content or "", trust_key, store_path)
    if merged is None:
        logger.debug("grok trust store {} already trusts {}", store_path, trust_key)
        return
    with log_span("Recording grok folder trust for {} in {}", trust_key, store_path):
        # The user's own store is written in place, so a dotfiles symlink stays one and an existing mode is kept.
        host.write_text_file(
            store_path,
            merged,
            mode=None if is_user_store and existing_content is not None else _TRUST_STORE_FILE_MODE,
            is_atomic=not is_user_store,
        )


def prompt_user_to_trust_grok_folder(trust_key: Path, user_store_path: Path) -> bool:
    """Ask on the terminal whether grok may trust trust_key; the default answer is no."""
    logger.info(
        "\nFolder {} is not yet trusted by grok.\n"
        "Trusting it lets grok load this folder's MCP servers, LSP config, .envrc, project hooks and plugins,\n"
        "which run commands taken from files in the folder.\n"
        "mngr records the grant in {} so later agents for this folder do not ask again.\n",
        trust_key,
        user_store_path,
    )
    return click.confirm(f"Trust {trust_key} for grok?", default=False)


def ensure_grok_folder_trust(
    host: OnlineHostInterface,
    work_dir: Path,
    # Sources the agent's env files (``host.build_source_env_prefix(agent)``), so HOME matches the agent's.
    env_prefix: str,
    # The user's own grok home, whose trust store mngr copies an existing grant from and records a new one in.
    user_grok_home: Path,
    agent_grok_home: Path,
    is_auto_dismiss_dialogs_at_startup: bool,
    is_auto_approve: bool,
    is_interactive: bool,
    # Asked with the trust key and the user's store path; returns whether the user consents.
    prompt_user_to_trust: Callable[[Path, Path], bool],
) -> GrokFolderTrustResult:
    """Seed grok's folder trust for work_dir in the agent's grok home, gated by the consent matrix.

    Raises SystemExit(1) when the user declines, or when no consent is available without a terminal.
    """
    trust_key = compute_grok_trust_key(host, work_dir, env_prefix)
    if trust_key.is_provisional:
        logger.debug("grok trusts {} provisionally; seeding no folder trust", trust_key.path)
        return GrokFolderTrustResult(trust_key=trust_key, outcome=GrokFolderTrustOutcome.PROVISIONAL)

    user_store_path = user_grok_home / TRUSTED_FOLDERS_FILENAME
    user_store_content = _read_trust_store(host, user_store_path)
    agent_store_path = agent_grok_home / TRUSTED_FOLDERS_FILENAME
    consent = decide_grok_trust_consent(
        is_trusted_by_user_store=is_grok_folder_trusted(user_store_content or "", trust_key.path, user_store_path),
        is_auto_dismiss_dialogs_at_startup=is_auto_dismiss_dialogs_at_startup,
        is_auto_approve=is_auto_approve,
        is_interactive=is_interactive,
    )
    match consent:
        case GrokTrustConsent.ALREADY_GRANTED:
            _write_folder_grant(
                host, agent_store_path, _read_trust_store(host, agent_store_path), trust_key.path, is_user_store=False
            )
            return GrokFolderTrustResult(trust_key=trust_key, outcome=GrokFolderTrustOutcome.ALREADY_TRUSTED)
        case GrokTrustConsent.PRE_APPROVED:
            pass
        case GrokTrustConsent.ASK_USER:
            if not prompt_user_to_trust(trust_key.path, user_store_path):
                logger.error("User declined to trust {} for grok. Aborting agent creation.", trust_key.path)
                raise SystemExit(1)
        case GrokTrustConsent.REFUSE:
            logger.error(
                "Folder {} is not trusted by grok, and mngr will not silently run an agent on untrusted code. "
                "Re-run interactively to be prompted, re-run with `--yes`, or set "
                "`auto_dismiss_dialogs_at_startup = true` on the grok agent type.",
                trust_key.path,
            )
            raise SystemExit(1)
        case _ as unreachable:
            assert_never(unreachable)

    _write_folder_grant(
        host, user_store_path, _read_trust_store(host, user_store_path), trust_key.path, is_user_store=True
    )
    _write_folder_grant(
        host, agent_store_path, _read_trust_store(host, agent_store_path), trust_key.path, is_user_store=False
    )
    return GrokFolderTrustResult(trust_key=trust_key, outcome=GrokFolderTrustOutcome.GRANTED)
