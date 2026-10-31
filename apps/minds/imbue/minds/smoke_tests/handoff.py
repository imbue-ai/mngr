"""Where one smoke run's handoff is written and read back.

Three media, because the three jobs of a run do not share a process:

* Vault leaves under ``run_handoff_vault_dir`` -- how the CI setup job hands
  the run to the test and teardown jobs, which hold different Vault roles;
* a local JSON state file -- how a local run hands it between
  ``smoke_tests.py setup`` and ``smoke_tests.py teardown``;
* env vars -- how the CI test step receives it, already exported.

:func:`resolve_handoff_or_none` reads them in that order of immediacy (the
environment first, then the state file, then Vault), so a job always uses what
it was handed directly before reaching for a shared store.
"""

import os
from collections.abc import Mapping
from pathlib import Path

from loguru import logger

from imbue.minds.envs.primitives import VaultReadError
from imbue.minds.envs.primitives import VaultSecretNotFoundError
from imbue.minds.envs.vault_reader import delete_vault_kv
from imbue.minds.envs.vault_reader import read_vault_kv
from imbue.minds.envs.vault_reader import write_vault_kv
from imbue.minds.smoke_tests.data_types import SmokeRunHandoff
from imbue.minds.smoke_tests.primitives import SMOKE_HANDOFF_FILE_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_RUN_ID_ENV_VAR
from imbue.minds.smoke_tests.primitives import SmokeRunId
from imbue.minds.smoke_tests.primitives import run_handoff_vault_dir


def write_handoff_to_vault(handoff: SmokeRunHandoff) -> None:
    """Publish the handoff as the per-run Vault leaves the other jobs read."""
    write_vault_kv(run_handoff_vault_dir(handoff.run_id), handoff.to_vault_values())
    logger.info("Published the smoke run handoff to {}", run_handoff_vault_dir(handoff.run_id))


def read_handoff_from_vault_or_none(*, run_id: SmokeRunId, tier: str) -> SmokeRunHandoff | None:
    """The handoff Vault holds for ``run_id``, or None when the run published none."""
    vault_dir = run_handoff_vault_dir(run_id)
    try:
        values = read_vault_kv(vault_dir)
    except VaultSecretNotFoundError:
        return None
    except VaultReadError as exc:
        logger.warning("Could not read the smoke run handoff at {}: {}", vault_dir, exc)
        return None
    if not values:
        return None
    return SmokeRunHandoff.from_vault_values(values, run_id=run_id, tier=tier)


def delete_handoff_from_vault(run_id: SmokeRunId) -> None:
    """Delete the per-run Vault leaves. Idempotent against an already-gone directory."""
    delete_vault_kv(run_handoff_vault_dir(run_id))


def write_handoff_state_file(handoff: SmokeRunHandoff, path: Path) -> None:
    """Write the handoff to a local state file (readable only by its owner -- it holds secrets)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(handoff.to_state_json())
    path.chmod(0o600)


def read_handoff_state_file_or_none(path: Path) -> SmokeRunHandoff | None:
    """The handoff in a state file, or None when the file is not there."""
    if not path.is_file():
        return None
    return SmokeRunHandoff.from_state_json(path.read_text())


def resolve_handoff_or_none(*, run_id: SmokeRunId, tier: str, state_path: Path | None) -> SmokeRunHandoff | None:
    """The run's handoff from the environment, else a state file, else Vault; None when nowhere.

    None is a tolerated outcome, not an error: a teardown may well run for a
    setup job that died before it minted anything, and it still has the rest
    of its cleanup to do.
    """
    from_env = SmokeRunHandoff.from_env_or_none(os.environ)
    if from_env is not None:
        logger.info("Read the smoke run handoff from the environment.")
        return from_env
    if state_path is not None:
        from_state_file = read_handoff_state_file_or_none(state_path)
        if from_state_file is not None:
            logger.info("Read the smoke run handoff from {}", state_path)
            return from_state_file
    from_vault = read_handoff_from_vault_or_none(run_id=run_id, tier=tier)
    if from_vault is not None:
        logger.info("Read the smoke run handoff from {}", run_handoff_vault_dir(run_id))
        return from_vault
    return None


def resolve_test_job_handoff_or_none(environ: Mapping[str, str], *, tier: str) -> SmokeRunHandoff | None:
    """The handoff a test job was given, from whatever ``environ`` names: the handoff itself, a state file, or a run id.

    The CI test job exports the handoff; a local run names the state file the
    setup command wrote (``MINDS_SMOKE_HANDOFF_FILE``); and a run id alone
    (``MINDS_SMOKE_RUN_ID``) reaches for the run's Vault leaves. None when the
    environment names none of them, which is how the signup test learns it has
    no throwaway account to act as.
    """
    from_env = SmokeRunHandoff.from_env_or_none(environ)
    if from_env is not None:
        logger.info("Read the smoke run handoff from the environment.")
        return from_env
    state_file = environ.get(SMOKE_HANDOFF_FILE_ENV_VAR, "")
    if state_file:
        from_state_file = read_handoff_state_file_or_none(Path(state_file))
        if from_state_file is not None:
            logger.info("Read the smoke run handoff from {}", state_file)
            return from_state_file
        logger.warning("{}={!r} names no file; falling back to Vault.", SMOKE_HANDOFF_FILE_ENV_VAR, state_file)
    run_id = environ.get(SMOKE_RUN_ID_ENV_VAR, "")
    if not run_id:
        return None
    from_vault = read_handoff_from_vault_or_none(run_id=SmokeRunId(run_id), tier=tier)
    if from_vault is not None:
        logger.info("Read the smoke run handoff from {}", run_handoff_vault_dir(SmokeRunId(run_id)))
    return from_vault
