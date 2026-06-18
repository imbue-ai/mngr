"""End-to-end release test for the Modal provider.

This test provisions and destroys a real Modal sandbox (plus its app, volume,
and environment). It costs real money and is double-gated:

- Modal credentials must be present: either ``~/.modal.toml`` exists (the
  autouse ``_load_modal_test_credentials`` fixture loads it into
  ``MODAL_TOKEN_ID`` / ``MODAL_TOKEN_SECRET``) or those two env vars are
  already set.
- ``MNGR_MODAL_RELEASE_TESTS=1`` must be set explicitly.

It plugs into the shared provider-release harness
(:func:`imbue.mngr.providers.provider_release_testing.run_provider_release_trip1`),
which drives the real ``mngr`` CLI as a subprocess against a temp
``settings.toml`` and probes the Modal API in-process to assert the cloud
state at each step. Modal cannot stop a host's compute (only terminate it), so
``supports_shutdown_hosts`` is False and the harness asserts ``mngr stop
--stop-host`` is refused with ``HostShutdownNotSupportedError``. Modal has no
``IsolationMode`` concept, so there is a single (unparametrized) test.

Environment / app / user_id alignment: the in-process probe provider and the
CLI subprocess must observe the *same* Modal sandbox. The Modal backend derives
the environment name as ``f"{prefix}{user_id}"`` (see
``ModalProviderBackend._derive_modal_names``), so this test pins both halves:
``MNGR_PREFIX`` (a timestamped ``mngr_test-...-`` prefix) is set into the
process environment so the subprocess -- which copies ``os.environ`` -- inherits
it, and ``user_id`` + ``app_name`` are written into the ``[providers.modal]``
settings block that the subprocess loads. The probe provider is built from a
``MngrContext`` carrying the same prefix and a ``ModalProviderConfig`` carrying
the same ``user_id`` + ``app_name``, so it lands in the identical Modal
environment + app.

Run manually:

    MNGR_MODAL_RELEASE_TESTS=1 \\
        just test libs/mngr_modal/imbue/mngr_modal/test_release_modal.py
"""

import os
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

import pluggy
import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import UserId
from imbue.mngr.providers.provider_release_testing import ProviderReleaseProfile
from imbue.mngr.providers.provider_release_testing import run_provider_release_trip1
from imbue.mngr.providers.provider_release_testing import run_provider_release_trip2
from imbue.mngr.providers.provider_release_testing import run_provider_release_trip3
from imbue.mngr.providers.provider_release_testing import run_provider_release_trip4
from imbue.mngr.utils.testing import delete_modal_apps_in_environment
from imbue.mngr.utils.testing import delete_modal_environment
from imbue.mngr.utils.testing import delete_modal_volumes_in_environment
from imbue.mngr.utils.testing import generate_test_environment_name
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr.utils.testing import make_mngr_ctx
from imbue.mngr.utils.testing import register_modal_test_app
from imbue.mngr.utils.testing import register_modal_test_environment
from imbue.mngr.utils.testing import register_modal_test_volume
from imbue.mngr_modal.backend import ModalProviderBackend
from imbue.mngr_modal.backend import STATE_VOLUME_SUFFIX
from imbue.mngr_modal.config import ModalProviderConfig
from imbue.mngr_modal.constants import MODAL_TEST_APP_PREFIX
from imbue.mngr_modal.instance import ModalProviderInstance
from imbue.mngr_modal.testing import MODAL_RELEASE_TESTS_OPT_IN

# The Modal provider-instance name (and thus the ``[providers.modal]`` settings
# block name) the CLI uses when ``--provider modal`` is passed.
_MODAL_PROVIDER_NAME = "modal"

# Host-name prefix for the trip. Short so the full host name stays well under
# Modal's tag-value limits; purely cosmetic for identifying test-owned hosts.
_MODAL_TEST_NAME_PREFIX = "test-modal-"

# Trip 2's sandbox lifetime cap (Modal's auto-shutdown analog). Modal has no idle watcher, so the
# sandbox just expires after its ``--timeout``; this is a hard max-lifetime, not idle-triggered, so
# it must be long enough for ``mngr create`` to finish connecting before the sandbox is terminated.
_MODAL_AUTO_SHUTDOWN_TIMEOUT_SECONDS = 120


def _modal_credentials_available() -> bool:
    """Return True iff Modal credentials are resolvable for this test session.

    The autouse ``_load_modal_test_credentials`` fixture loads the developer's
    ``~/.modal.toml`` into ``MODAL_TOKEN_ID`` / ``MODAL_TOKEN_SECRET`` before any
    test runs, so this gate is satisfied either by that file or by those env
    vars being set directly (e.g. in CI). Checked at call time (not import) so
    the fixture has already run.
    """
    if Path(os.path.expanduser("~/.modal.toml")).exists():
        return True
    return bool(os.environ.get("MODAL_TOKEN_ID") and os.environ.get("MODAL_TOKEN_SECRET"))


def _write_release_settings(settings_dir: Path, *, user_id: str, app_name: str) -> None:
    """Write the release-test ``settings.toml`` selecting the Modal provider into ``settings_dir``.

    ``is_allowed_in_pytest = true`` is required because the subprocess inherits
    ``PYTEST_CURRENT_TEST`` and mngr refuses to load any config that does not opt
    in. ``user_id`` and ``app_name`` are written explicitly so the subprocess and
    the in-process probe provider resolve the identical Modal environment
    (``f"{prefix}{user_id}"``) and app -- the prefix half is supplied via
    ``MNGR_PREFIX`` in the process environment. Other remote providers are
    disabled so the create-host preflight (and ``mngr list``) doesn't trip on
    them looking for credentials.

    ``MNGR_PROJECT_CONFIG_DIR`` is the literal directory containing
    ``settings.toml``, so the file is written directly into ``settings_dir``.
    """
    (settings_dir / "settings.toml").write_text(
        # Opt this config past the pytest guard. Top-level key, so it must
        # precede the first table.
        "is_allowed_in_pytest = true\n"
        "\n[providers.modal]\n"
        "is_enabled = true\n"
        'backend = "modal"\n'
        f'user_id = "{user_id}"\n'
        f'app_name = "{app_name}"\n'
        # Disable other remote providers so the create-host preflight (and
        # ``mngr list``) doesn't trip on them looking for credentials.
        "\n[providers.aws]\nis_enabled = false\n"
        "\n[providers.azure]\nis_enabled = false\n"
        "\n[providers.gcp]\nis_enabled = false\n"
        "\n[providers.vultr]\nis_enabled = false\n"
        "\n[providers.ovh]\nis_enabled = false\n"
        "\n[providers.imbue_cloud]\nis_enabled = false\n"
    )


class _ModalReleaseProfile(ProviderReleaseProfile):
    """Modal plumbing for the shared provider release trip.

    Modal is not a VpsProvider, so this implements the ``ProviderReleaseProfile``
    ABC directly. The probe methods drive an in-process ``ModalProviderInstance``
    bound to the same environment + app + user_id that the CLI subprocess uses,
    matching sandboxes by the ``mngr_host_name`` tag.
    """

    provider_name = _MODAL_PROVIDER_NAME
    name_prefix = _MODAL_TEST_NAME_PREFIX

    # Modal can terminate a sandbox but cannot stop its compute and resume it,
    # so the harness takes the refusal branch for ``mngr stop --stop-host``.
    supports_shutdown_hosts = False
    supports_snapshots = True
    # Modal snapshots are portable filesystem images that outlive the sandbox, so they survive
    # destroy and can seed a fresh `mngr create --snapshot`.
    snapshot_survives_destroy = True
    # Modal's idle path lets the sandbox expire and be terminated by Modal's own timeout -- there is
    # no resumable stopped state, so Trip 2 asserts the termination only and skips the resume.
    resumes_after_auto_shutdown = False

    # Trip 4 (error classification). On the `mngr create` path Modal's bootstrap now raises the
    # contract ``ProviderUnavailableError`` (the spec's wrong-error-class divergence was fixed in
    # this PR -- see mngr_modal/backend.py), with curated help pointing at ``uvx modal token set``.
    # Modal has its own build-arg parser (no shared ``--vps-*`` migration check), so that scenario
    # is skipped.
    raises_contract_unavailable_error = True
    has_curated_unavailable_help = True
    supports_vps_migration_arg_check = False
    credential_setup_command = "uvx modal token set"
    unavailable_error_substring = "modal is not authorized"

    def __init__(self, provider: ModalProviderInstance, user_id: str, app_name: str) -> None:
        self._provider = provider
        self._user_id = user_id
        self._app_name = app_name

    def unavailable_reason(self) -> str | None:
        if not (_modal_credentials_available() and MODAL_RELEASE_TESTS_OPT_IN):
            return "Modal credentials or MNGR_MODAL_RELEASE_TESTS=1 not set"
        return None

    def write_settings(self, settings_dir: Path) -> None:
        _write_release_settings(settings_dir, user_id=self._user_id, app_name=self._app_name)

    def create_extra_args(self) -> Sequence[str]:
        return ()

    def auto_shutdown_create_args(self) -> Sequence[str]:
        # Modal has no idle watcher; cap the sandbox lifetime so Modal's own timeout terminates it.
        return ("-b", f"--timeout={_MODAL_AUTO_SHUTDOWN_TIMEOUT_SECONDS}")

    def make_credentials_unresolvable_env(self) -> Mapping[str, str | None]:
        # The autouse `_load_modal_test_credentials` fixture loads the token into these env vars
        # (HOME is swapped, so `~/.modal.toml` is already hidden). Blanking them makes Modal's
        # bootstrap auth fail, surfacing the "Modal is not authorized" MngrError.
        return {"MODAL_TOKEN_ID": "", "MODAL_TOKEN_SECRET": ""}

    def _live_sandbox_object_id(self) -> str | None:
        """Return the object id of this trip's single live sandbox, or None.

        The alignment fixture gives the test an exclusive Modal environment + app, so the one
        live sandbox there is this trip's host. mngr tags the sandbox with the (coolname) host
        name it assigns -- not the agent name passed to ``mngr create`` -- so the host is
        identified by that exclusivity rather than by matching a name, which also rides out the
        object-id change when a resumed host comes back as a fresh sandbox. Caches are reset so
        each probe sees fresh Modal state; ``Sandbox.list`` returns only live sandboxes, so a
        terminated host drops to a count of zero -- the reliable running/gone signal.
        """
        self._provider.reset_caches()
        object_ids = [sandbox.get_object_id() for sandbox in self._provider._list_sandboxes()]
        return object_ids[0] if len(object_ids) == 1 else None

    def find_launched_host_handle(self, host_name: str) -> str | None:
        return self._live_sandbox_object_id()

    def is_host_compute_running(self, handle: str) -> bool:
        return self._live_sandbox_object_id() is not None

    def is_host_compute_stopped(self, handle: str) -> bool:
        # Never called for Modal: supports_shutdown_hosts is False, so the
        # harness takes the refusal branch and never polls for a stopped host.
        raise NotImplementedError("Modal cannot stop host compute (supports_shutdown_hosts is False)")

    def force_strand_host(self, handle: str) -> None:
        # Terminate the sandbox out of band, bypassing `mngr destroy`. Idempotent without
        # swallowing errors: if it is already gone (the finally backstop can re-run after gc),
        # there is nothing left to strand, so re-resolve liveness first and let any genuine
        # terminate failure surface.
        object_id = self._live_sandbox_object_id()
        if object_id is None:
            return
        self._provider._modal_interface.sandbox_from_id(object_id).terminate()

    def is_backend_clean(self, handle: str) -> bool:
        # Clean iff no live sandbox remains in the test's exclusive env/app (terminated / gc'd).
        return self._live_sandbox_object_id() is None


@pytest.fixture()
def _modal_release_alignment(
    temp_host_dir: Path,
    temp_profile_dir: Path,
    plugin_manager: pluggy.PluginManager,
    cg: ConcurrencyGroup,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[ModalProviderInstance, str, str]]:
    """Pin one (prefix, user_id, app_name) triple for both the CLI subprocess and the probe.

    Generates a timestamped ``mngr_test-...-`` prefix (so the Modal backend's
    pytest env-name guard accepts it and the CI cleanup script recognizes any
    leak), a random user_id, and a test-prefixed app_name. Sets ``MNGR_PREFIX``
    and ``MNGR_USER_ID`` into the process environment so the harness subprocess
    (which copies ``os.environ``) resolves the same Modal environment + user as
    the in-process probe provider built here.

    Registers the app / volume / environment names for the conftest session-end
    leak detector, and tears down by deleting all three (the harness's gc step
    already terminates the sandbox; this reaps the surrounding Modal resources).
    """
    prefix = f"{generate_test_environment_name()}-"
    user_id = uuid4().hex
    app_name = f"{MODAL_TEST_APP_PREFIX}{get_short_random_string()}"
    environment_name = f"{prefix}{user_id}"
    volume_name = f"{app_name}{STATE_VOLUME_SUFFIX}"

    monkeypatch.setenv("MNGR_PREFIX", prefix)
    monkeypatch.setenv("MNGR_USER_ID", user_id)

    config = MngrConfig(default_host_dir=temp_host_dir, prefix=prefix)
    mngr_ctx = make_mngr_ctx(config, plugin_manager, temp_profile_dir, concurrency_group=cg)
    provider_config = ModalProviderConfig(user_id=UserId(user_id), app_name=app_name, host_dir=Path("/mngr"))
    instance_name = ProviderInstanceName(_MODAL_PROVIDER_NAME)
    # Bootstrap creates the per-user Modal environment if missing (the create
    # path does the same), so the probe provider can list sandboxes in it.
    ModalProviderBackend.bootstrap_for_host_creation(name=instance_name, config=provider_config, mngr_ctx=mngr_ctx)
    provider = ModalProviderBackend.build_provider_instance(
        name=instance_name, config=provider_config, mngr_ctx=mngr_ctx
    )
    assert isinstance(provider, ModalProviderInstance), (
        f"expected ModalProviderInstance, got {type(provider).__name__}"
    )

    register_modal_test_app(app_name)
    register_modal_test_volume(volume_name)
    register_modal_test_environment(environment_name)

    yield provider, user_id, app_name

    delete_modal_apps_in_environment(environment_name)
    delete_modal_volumes_in_environment(environment_name)
    delete_modal_environment(environment_name)


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(1200)
@pytest.mark.skipif(
    not (_modal_credentials_available() and MODAL_RELEASE_TESTS_OPT_IN),
    reason="Modal credentials or MNGR_MODAL_RELEASE_TESTS=1 not set",
)
def test_provider_release_trip1(
    tmp_path: Path,
    temp_git_repo: Path,
    _modal_release_alignment: tuple[ModalProviderInstance, str, str],
) -> None:
    provider, user_id, app_name = _modal_release_alignment
    run_provider_release_trip1(
        _ModalReleaseProfile(provider=provider, user_id=user_id, app_name=app_name),
        tmp_path,
        temp_git_repo,
    )


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(1200)
@pytest.mark.skipif(
    not (_modal_credentials_available() and MODAL_RELEASE_TESTS_OPT_IN),
    reason="Modal credentials or MNGR_MODAL_RELEASE_TESTS=1 not set",
)
def test_provider_release_trip2(
    tmp_path: Path,
    temp_git_repo: Path,
    _modal_release_alignment: tuple[ModalProviderInstance, str, str],
) -> None:
    provider, user_id, app_name = _modal_release_alignment
    run_provider_release_trip2(
        _ModalReleaseProfile(provider=provider, user_id=user_id, app_name=app_name),
        tmp_path,
        temp_git_repo,
    )


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(1200)
@pytest.mark.skipif(
    not (_modal_credentials_available() and MODAL_RELEASE_TESTS_OPT_IN),
    reason="Modal credentials or MNGR_MODAL_RELEASE_TESTS=1 not set",
)
def test_provider_release_trip3(
    tmp_path: Path,
    temp_git_repo: Path,
    _modal_release_alignment: tuple[ModalProviderInstance, str, str],
) -> None:
    provider, user_id, app_name = _modal_release_alignment
    run_provider_release_trip3(
        _ModalReleaseProfile(provider=provider, user_id=user_id, app_name=app_name),
        tmp_path,
        temp_git_repo,
    )


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.skipif(
    not (_modal_credentials_available() and MODAL_RELEASE_TESTS_OPT_IN),
    reason="Modal credentials or MNGR_MODAL_RELEASE_TESTS=1 not set",
)
def test_provider_release_trip4(
    tmp_path: Path,
    temp_git_repo: Path,
    _modal_release_alignment: tuple[ModalProviderInstance, str, str],
) -> None:
    # No-boot CLI error-classification trip: no ``rsync`` mark (it never provisions a sandbox).
    provider, user_id, app_name = _modal_release_alignment
    run_provider_release_trip4(
        _ModalReleaseProfile(provider=provider, user_id=user_id, app_name=app_name),
        tmp_path,
        temp_git_repo,
    )
