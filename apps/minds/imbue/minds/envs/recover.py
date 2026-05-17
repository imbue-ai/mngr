"""Recover-target file + ``minds env recover`` reversal logic.

Every successful + every failed deploy follows the same protocol:

1. After preflight succeeds and after the Neon restore-point is created,
   write a recover-target file atomically (tempfile + rename) at the
   monorepo root.
2. Run the rest of the deploy (push secrets, run migrations, modal
   deploy, health check).
3. On full success: delete the recover-target file.
4. On any failure between steps 1 and 3: leave the file in place +
   print operator-facing guidance to run ``minds env recover``.

``minds env recover`` reads the file and runs every reversal step in
order regardless of which deploy stage was reached. Each reversal step
is individually idempotent, so re-running ``recover`` after a partial
recovery converges. The file is deleted only after every reversal step
has been attempted.

The file lives at the *monorepo root* (not the activated env's data
root) so a stray recovery from any tier is visible to every command
the operator might invoke next -- ``activate``, ``deploy``,
``destroy``, etc. all refuse to run while the file exists.
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Final
from typing import Self

from loguru import logger
from pydantic import Field
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import info_span
from imbue.minds.envs.per_env_deploy import ModalDeployError
from imbue.minds.envs.providers.neon_db import NeonProviderError
from imbue.minds.envs.providers.neon_db import restore_branch_from_snapshot
from imbue.minds.envs.secret_lifecycle import DeployId
from imbue.minds.errors import MindError

# Filename at the monorepo root. Gitignored via the ``.minds-deploy-*.json``
# glob in the repo's .gitignore.
RECOVER_TARGET_FILENAME: Final[str] = ".minds-deploy-recover-target.json"

# Marker file used by ``_find_monorepo_root``: every monorepo checkout
# carries an ``apps/`` directory at its top, so walking up from CWD until
# we find one is the canonical "are we in the monorepo" probe.
_MONOREPO_MARKER_SUBDIR: Final[str] = "apps"


class RecoverTargetMissingError(MindError, FileNotFoundError):
    """Raised when ``minds env recover`` runs but no recover-target file exists."""


class RecoverTargetAlreadyExistsError(MindError, FileExistsError):
    """Raised when a new deploy tries to write a recover-target file while one already exists."""


class NotInMonorepoError(MindError):
    """Raised when ``minds env deploy`` / ``recover`` is run from outside the monorepo."""


class RecoverFailedError(MindError):
    """Raised when one or more reversal steps in ``recover_env`` failed.

    The recover-target file is left in place so the operator can re-run
    ``minds env recover`` after addressing the underlying issue.
    """


class RecoverTarget(FrozenModel):
    """Captured "where to get back to" state, written atomically at deploy start.

    Carries only the information ``recover_env`` needs to converge the
    cloud back to the pre-deploy state -- nothing more. ``app_versions_
    to_restore`` is ``None`` for an app that had no prior deploy
    (first-ever deploy of that env / tier); ``recover_env`` skips Modal
    rollback for those + logs a warning.
    """

    deploy_id: DeployId = Field(description="The deploy id this recover-target was minted FOR.")
    env_name: str = Field(description="The activated env name (e.g. ``dev-josh-1`` or ``staging``).")
    tier: str
    modal_env: str = Field(description="The Modal env the deploy targeted.")
    modal_workspace: str
    vault_path_prefix: str
    neon_project_id: str | None = Field(
        default=None,
        description=(
            "Neon project id the snapshot branch was created in. For dev (creates_resources=true) "
            "it's the just-provisioned per-env project; for shared tiers (creates_resources=false) "
            "it's the operator-managed project id from "
            "``secrets/minds/<tier>/neon-admin.NEON_PROJECT_ID``."
        ),
    )
    neon_branch_id: str | None = Field(
        default=None,
        description="Default branch id on the Neon project (parent of the snapshot branch).",
    )
    neon_snapshot_branch_id: str | None = Field(
        default=None,
        description=(
            "Branch id of the snapshot branch created at deploy start (off the default branch, "
            "named ``pre-deploy-<deploy_id>``). Recover restores the default branch from this "
            "snapshot via Neon's ``POST .../branches/{main}/restore`` with ``source_branch_id``. "
            "``None`` if the deploy is for a tier without Neon-restore configuration (missing "
            "``NEON_PROJECT_ID`` in Vault for a shared tier)."
        ),
    )
    app_versions_to_restore: dict[str, str | None] = Field(
        description=(
            "Modal app name -> captured pre-deploy version id, or None for first-deploy. "
            "Recover runs ``modal app rollback`` to the captured version for each entry."
        ),
    )

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> Self:
        try:
            data = json.loads(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            raise MindError(f"Recover-target file is not valid JSON: {exc}") from exc
        return cls.model_validate(data)

    def to_json_bytes(self) -> bytes:
        return json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True).encode("utf-8")


def find_monorepo_root(*, cwd: Path | None = None) -> Path:
    """Walk up from ``cwd`` looking for the monorepo's ``apps/`` marker.

    Raises :class:`NotInMonorepoError` if no marker is found before
    hitting the filesystem root.
    """
    start = Path(cwd if cwd is not None else os.getcwd()).resolve()
    for candidate in [start, *start.parents]:
        if (candidate / _MONOREPO_MARKER_SUBDIR).is_dir():
            return candidate
    raise NotInMonorepoError(
        f"Could not find monorepo root (looking for an ``{_MONOREPO_MARKER_SUBDIR}/`` directory) "
        f"walking up from {start}. `minds env deploy` / `recover` must be run from inside the monorepo."
    )


def recover_target_path(*, repo_root: Path) -> Path:
    return repo_root / RECOVER_TARGET_FILENAME


def recover_target_exists(*, repo_root: Path) -> bool:
    return recover_target_path(repo_root=repo_root).is_file()


def write_recover_target_atomic(target: RecoverTarget, *, repo_root: Path) -> Path:
    """Write the recover-target file atomically (tempfile + fsync + rename).

    Refuses if a recover-target file already exists; deploy is supposed
    to call this AFTER preflight has confirmed there isn't one, but the
    extra check here defends against a race where two deploys both pass
    preflight + race to write.
    """
    final_path = recover_target_path(repo_root=repo_root)
    if final_path.exists():
        raise RecoverTargetAlreadyExistsError(
            f"Recover-target file already exists at {final_path}; refusing to overwrite. "
            "Run `minds env recover` (or delete the file manually if it's known-stale) before retrying."
        )
    # NamedTemporaryFile with delete=False so we control the lifetime;
    # fsync the file descriptor before close so a power loss between
    # write + rename doesn't leave a partial file under the final name.
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=repo_root,
        prefix=f".{RECOVER_TARGET_FILENAME}.tmp.",
        delete=False,
    ) as tmp:
        tmp.write(target.to_json_bytes())
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, final_path)
    return final_path


def read_recover_target(*, repo_root: Path) -> RecoverTarget:
    final_path = recover_target_path(repo_root=repo_root)
    if not final_path.is_file():
        raise RecoverTargetMissingError(
            f"No recover-target file at {final_path}; nothing to recover from. "
            "(Recover is only meaningful after a failed `minds env deploy`.)"
        )
    return RecoverTarget.from_json_bytes(final_path.read_bytes())


def delete_recover_target(*, repo_root: Path) -> None:
    final_path = recover_target_path(repo_root=repo_root)
    if final_path.exists():
        final_path.unlink()


def recover_env(
    *,
    repo_root: Path,
    providers,
    credentials,
    parent_cg: ConcurrencyGroup,
) -> None:
    """Read the recover-target file and idempotently restore the cloud to it.

    Runs every reversal step in order, regardless of how far the failed
    deploy got. Each step is individually idempotent. Per-step failures
    are logged but do not abort subsequent steps; only after every step
    has been attempted does the file get deleted (or, on at least one
    failure, ``RecoverFailedError`` is raised and the file stays).

    Reversal order (matches the deploy order in reverse):

    1. ``modal app rollback`` each app to its captured pre-deploy version.
    2. Neon ``restore_branch_to_named_restore_point`` to the captured
       restore-point name (only if neon_restore_point_name is set).
    3. ``modal secret delete <svc>-<tier>-<deploy_id>`` for every
       service in ``deploy_config.secrets.services``.
    4. Delete the recover-target file.
    """
    target = read_recover_target(repo_root=repo_root)
    logger.info("Recovering env {!r} (deploy id was {})", target.env_name, target.deploy_id)
    logger.info("Recover target: {}", target.model_dump_json(indent=2))

    errors: list[str] = []

    # Step 1: modal app rollback for each captured app version.
    for app_name, version in target.app_versions_to_restore.items():
        if version is None:
            logger.warning(
                "Recover: skipping `modal app rollback {}` -- no captured pre-deploy version "
                "(first-ever deploy of this app). Leaving the app at its current state.",
                app_name,
            )
            continue
        try:
            with info_span("Rolling back Modal app {!r} to version {!r}", app_name, version):
                providers.rollback_modal_app(
                    app_name=app_name, version=version, modal_env=target.modal_env, parent_cg=parent_cg
                )
        except (ModalDeployError, MindError) as exc:
            logger.warning("Recover: rollback of {!r} failed: {}", app_name, exc)
            errors.append(f"modal app rollback {app_name} {version}: {exc}")

    # Step 2: Neon instant restore from the captured snapshot branch.
    if target.neon_snapshot_branch_id and target.neon_project_id and target.neon_branch_id:
        try:
            with info_span(
                "Restoring Neon branch {!r} from snapshot branch {!r}",
                target.neon_branch_id,
                target.neon_snapshot_branch_id,
            ):
                _restore_neon(target=target, credentials=credentials)
        except NeonProviderError as exc:
            logger.warning("Recover: Neon restore failed: {}", exc)
            errors.append(f"neon restore_branch_from_snapshot: {exc}")
    else:
        logger.info("Recover: no Neon snapshot branch captured; skipping Neon restore step.")

    # Step 3: delete every <svc>-<tier>-<deploy_id> Modal Secret pushed
    # by the failed deploy. We can derive the exact set from the deploy
    # config's services list, but the recover target doesn't carry the
    # services list -- so we walk the Modal env and delete any name
    # that ends with -<tier>-<deploy_id>.
    try:
        with info_span("Cleaning up orphan Modal Secrets from failed deploy id {!r}", str(target.deploy_id)):
            _cleanup_orphan_secrets(target=target, providers=providers, parent_cg=parent_cg)
    except ModalDeployError as exc:
        logger.warning("Recover: orphan-secret cleanup failed: {}", exc)
        errors.append(f"orphan secret cleanup: {exc}")

    # Step 4: delete the recover-target file -- only if every prior
    # step succeeded. On partial failure we keep it so the operator can
    # re-run recover after fixing the underlying issue.
    if errors:
        raise RecoverFailedError(
            f"Recover for env {target.env_name!r} hit {len(errors)} error(s):\n  - "
            + "\n  - ".join(errors)
            + f"\nThe recover-target file at {recover_target_path(repo_root=repo_root)} has been left "
            "in place; re-run `minds env recover` after addressing the underlying issue."
        )
    delete_recover_target(repo_root=repo_root)
    logger.info(
        "Recover complete. Deleted recover-target file. Env {!r} is back to the pre-deploy state.", target.env_name
    )


def _restore_neon(*, target: RecoverTarget, credentials) -> None:
    """Adapter that calls ``restore_branch_from_snapshot`` with credential lookup.

    The ``preserve_under_name`` argument captures the broken pre-restore
    state under ``pre-rollback-<deploy_id>`` so the operator can inspect
    it later via the Neon console.
    """
    assert target.neon_project_id is not None
    assert target.neon_branch_id is not None
    assert target.neon_snapshot_branch_id is not None
    restore_branch_from_snapshot(
        target.neon_project_id,
        target.neon_branch_id,
        target.neon_snapshot_branch_id,
        preserve_under_name=f"pre-rollback-{target.deploy_id}",
        api_token=credentials.neon_api_token,
    )


def _cleanup_orphan_secrets(*, target: RecoverTarget, providers, parent_cg: ConcurrencyGroup) -> None:
    """Delete every Modal Secret whose name ends with ``-<tier>-<deploy_id>`` in the target Modal env."""
    suffix = f"-{target.tier}-{target.deploy_id}"
    all_secrets = providers.list_modal_secrets(target.modal_env, parent_cg)
    orphans = [name for name in all_secrets if name.endswith(suffix)]
    for orphan in orphans:
        providers.delete_modal_secret(orphan, target.modal_env, parent_cg)


def make_neon_snapshot_branch_name(deploy_id: DeployId) -> str:
    """Canonical name for the snapshot branch a deploy creates.

    Used both by deploy (to name the branch) and by the operator (to
    recognize the branch in the Neon console). Lives in this module
    because recover is the only consumer that needs to derive it from
    a captured deploy_id.
    """
    return f"pre-deploy-{deploy_id}"


__all__ = [
    "NotInMonorepoError",
    "RECOVER_TARGET_FILENAME",
    "RecoverFailedError",
    "RecoverTarget",
    "RecoverTargetAlreadyExistsError",
    "RecoverTargetMissingError",
    "delete_recover_target",
    "find_monorepo_root",
    "make_neon_snapshot_branch_name",
    "read_recover_target",
    "recover_env",
    "recover_target_exists",
    "recover_target_path",
    "write_recover_target_atomic",
]


# `SecretStr` is imported because it appears in `credentials.neon_api_token`'s
# type via the runtime caller, even though we don't reference it directly in
# this module. Linters won't see that without an explicit reference, so keep
# this assertion to anchor the import + signal intent.
assert SecretStr is not None
