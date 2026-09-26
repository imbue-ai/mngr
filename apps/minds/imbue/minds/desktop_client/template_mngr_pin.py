"""The template's mngr pin, and the credential a build from a private pin needs.

A default-workspace-template checkout installs mngr from the repo and commit its root
``pyproject.toml`` pins under ``[tool.uv.sources]``: the public mirror for every release,
or the private ``mngr-internal`` repo on a branch iterating on a paired mngr change. A
build from the private repo can only fetch it with a credential, which every creator
delivers the same way: ``MNGR_INTERNAL_GIT_TOKEN`` in the ``mngr create`` subprocess
environment (a docker build reads it as a BuildKit secret; an outer-box build gets it
forwarded by mngr_vps) and, for the providers that provision over SSH instead of a
Dockerfile, an uploaded file at ``/run/secrets/mngr_internal_git_token``.

Whether a credential is needed is read off the pin, never a flag, so a create that would
fail minutes into a build fails here first with the remedy. Design:
``specs/internal-mngr-pin/spec.md`` in the mngr repo.
"""

import os
import re
import tempfile
import tomllib
from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import contextmanager
from enum import auto
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds.envs.primitives import VaultReadError
from imbue.minds.envs.vault_reader import VaultPath
from imbue.minds.envs.vault_reader import read_vault_kv
from imbue.minds.errors import MindError
from imbue.minds.primitives import GitCommitHash
from imbue.minds.primitives import LaunchMode

PUBLIC_MNGR_REPO_URL: Final[str] = "https://github.com/imbue-ai/mngr"
INTERNAL_MNGR_REPO_URL: Final[str] = "https://github.com/imbue-ai/mngr-internal"
# The env var every creator delivers the credential through.
INTERNAL_GIT_TOKEN_ENV_VAR: Final[str] = "MNGR_INTERNAL_GIT_TOKEN"
# Where the shared read-only credential lives for anyone with a Vault login: the KV
# directory (split layout, see ``vault_reader``) and the leaf under it.
INTERNAL_GIT_TOKEN_VAULT_PATH: Final[VaultPath] = VaultPath("secrets/minds/dev/mngr-internal-git")
INTERNAL_GIT_TOKEN_VAULT_KEY: Final[str] = "MNGR_INTERNAL_GIT_TOKEN"
# Where the template's build scripts read the credential (``_mngr_git_auth.sh``): the
# BuildKit secret mount path on docker, the uploaded file on lima and modal.
INTERNAL_GIT_TOKEN_REMOTE_PATH: Final[str] = "/run/secrets/mngr_internal_git_token"
# The launch modes whose template provisions over SSH from the synced tree rather than
# through a Dockerfile, so the credential has to be uploaded as a file.
UPLOADED_CREDENTIAL_LAUNCH_MODES: Final[frozenset[LaunchMode]] = frozenset({LaunchMode.LIMA, LaunchMode.MODAL})
_FULL_SHA: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{40}")


class TemplateMngrPinError(MindError):
    """The template's mngr pin has a shape its own build cannot install from."""


class InternalMngrCredentialError(MindError):
    """The template pins the private mngr repo and no credential for it could be resolved."""


class MngrPinKind(UpperCaseStrEnum):
    """Which mngr repo a template pin names."""

    PUBLIC = auto()
    INTERNAL = auto()


class TemplateMngrPin(FrozenModel):
    """The mngr commit a template checkout installs from."""

    kind: MngrPinKind = Field(description="Which mngr repo the pin names")
    rev: GitCommitHash = Field(description="The full commit the template installs mngr from")


class InternalMngrCredential(FrozenModel):
    """What one ``mngr create`` gets so its build can fetch a private mngr pin."""

    create_args: tuple[str, ...] = Field(description="Extra ``mngr create`` arguments (the credential upload, if any)")
    token: SecretStr | None = Field(
        description="The credential the build fetches the pin with, or None when it needs none"
    )

    def subprocess_env(self) -> dict[str, str]:
        """Extra environment for the ``mngr create`` subprocess: the token under its env var, if any."""
        if self.token is None:
            return {}
        return {INTERNAL_GIT_TOKEN_ENV_VAR: self.token.get_secret_value()}


NO_INTERNAL_MNGR_CREDENTIAL: Final[InternalMngrCredential] = InternalMngrCredential(create_args=(), token=None)


@pure
def parse_template_mngr_pin(pyproject_text: str) -> TemplateMngrPin | None:
    """The pin a template's ``pyproject.toml`` carries, or None when it pins neither mngr repo.

    A checkout without an ``imbue-mngr`` source, or one that takes it from somewhere other
    than the two mngr repos (a user's own template on a fork or a path), is not this app's
    concern: it needs no credential from us, and its build is its own. A pin on one of the
    two repos at anything but a full commit cannot build, and says so.
    """
    try:
        pyproject = tomllib.loads(pyproject_text)
    except tomllib.TOMLDecodeError as e:
        raise TemplateMngrPinError(f"the template's pyproject.toml is not valid TOML: {e}") from e
    sources = pyproject.get("tool", {}).get("uv", {}).get("sources", {})
    source = sources.get("imbue-mngr")
    if not isinstance(source, dict):
        return None
    repo_url = source.get("git")
    rev = source.get("rev")
    if repo_url == PUBLIC_MNGR_REPO_URL:
        kind = MngrPinKind.PUBLIC
    elif repo_url == INTERNAL_MNGR_REPO_URL:
        kind = MngrPinKind.INTERNAL
    else:
        return None
    if not isinstance(rev, str) or not _FULL_SHA.fullmatch(rev):
        raise TemplateMngrPinError(f"the template pins imbue-mngr at {rev!r}, not a full 40-hex commit")
    return TemplateMngrPin(kind=kind, rev=GitCommitHash(rev))


def read_template_mngr_pin(template_dir: Path) -> TemplateMngrPin | None:
    """The pin of the template checkout at ``template_dir``, or None when it has no pyproject or no pin."""
    pyproject = template_dir / "pyproject.toml"
    if not pyproject.is_file():
        return None
    return parse_template_mngr_pin(pyproject.read_text())


def _vault_token(parent_concurrency_group: ConcurrencyGroup | None) -> SecretStr | None:
    """The shared read-only credential from Vault, or None when there is no login, no CLI, or no entry.

    The Vault reader runs its subprocesses under a child group of its own, so the caller
    can resolve a credential outside any concurrency group and raise on its absence unwrapped.
    """
    try:
        entry = read_vault_kv(INTERNAL_GIT_TOKEN_VAULT_PATH, parent_concurrency_group=parent_concurrency_group)
    except VaultReadError as e:
        logger.debug("Vault yields no mngr-internal credential: {}", e)
        return None
    token = entry.get(INTERNAL_GIT_TOKEN_VAULT_KEY, "").strip()
    if not token:
        logger.debug("Vault entry {} has no {} leaf", INTERNAL_GIT_TOKEN_VAULT_PATH, INTERNAL_GIT_TOKEN_VAULT_KEY)
        return None
    return SecretStr(token)


def resolve_internal_git_token(
    environ: Mapping[str, str],
    is_vault_fallback_allowed: bool,
    parent_concurrency_group: ConcurrencyGroup | None,
) -> SecretStr | None:
    """The credential for the private repo: the env var, else (where allowed) the shared Vault entry.

    The fallback is for operator contexts only; a packaged end-user app never reads Vault,
    and never meets a private pin either since no release carries one.
    """
    token = environ.get(INTERNAL_GIT_TOKEN_ENV_VAR, "").strip()
    if token:
        return SecretStr(token)
    if is_vault_fallback_allowed:
        return _vault_token(parent_concurrency_group)
    return None


@contextmanager
def internal_mngr_credential(
    launch_mode: LaunchMode,
    # The template checkout the create builds from; None when the create has no local
    # checkout (an imbue-cloud lease), where the pin cannot be read.
    template_dir: Path | None,
    environ: Mapping[str, str],
    is_vault_fallback_allowed: bool,
    parent_concurrency_group: ConcurrencyGroup | None,
) -> Iterator[InternalMngrCredential]:
    """What one ``mngr create`` needs so its build can fetch the template's mngr pin.

    A public pin (or a template with none) gets nothing. A private pin gets the token in the
    subprocess env and, for the SSH-provisioned modes, an upload of it to the path the build
    scripts read; the uploaded file is written 0600 to a temp dir that is removed when the
    create finishes. A private pin with no resolvable token raises before anything is built.
    Enter it outside the create's own concurrency group: the group would wrap that error in
    a ``ConcurrencyExceptionGroup`` that no caller matches.

    Without a checkout to read, the credential is forwarded when one resolves and its absence
    is not an error: the imbue-cloud fast path adopts a baked image and needs none.
    """
    if template_dir is None:
        token = resolve_internal_git_token(environ, is_vault_fallback_allowed, parent_concurrency_group)
        yield InternalMngrCredential(create_args=(), token=token)
        return
    pin = read_template_mngr_pin(template_dir)
    if pin is None or pin.kind is MngrPinKind.PUBLIC:
        yield NO_INTERNAL_MNGR_CREDENTIAL
        return
    token = resolve_internal_git_token(environ, is_vault_fallback_allowed, parent_concurrency_group)
    if token is None:
        raise InternalMngrCredentialError(
            f"the template at {template_dir} pins mngr from {INTERNAL_MNGR_REPO_URL} at {pin.rev[:12]}, "
            f"which the workspace build cannot fetch without a credential; export "
            f"{INTERNAL_GIT_TOKEN_ENV_VAR} (a read-only fine-grained token for that repo) or "
            f"`vault login -method=oidc` for the shared one at {INTERNAL_GIT_TOKEN_VAULT_PATH}, then "
            "create again; or pin the template to the public mirror (`just dwt-mngr-pin-export`)"
        )
    if launch_mode not in UPLOADED_CREDENTIAL_LAUNCH_MODES:
        yield InternalMngrCredential(create_args=(), token=token)
        return
    with tempfile.TemporaryDirectory(prefix="minds-mngr-internal-credential-") as staging:
        token_file = Path(staging) / "mngr_internal_git_token"
        token_file.write_text(token.get_secret_value())
        os.chmod(token_file, 0o600)
        yield InternalMngrCredential(
            create_args=("--upload-file", f"{token_file}:{INTERNAL_GIT_TOKEN_REMOTE_PATH}"),
            token=token,
        )
