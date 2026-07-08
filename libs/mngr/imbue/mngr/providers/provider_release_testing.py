"""Shared harness for provider-plugin release (end-to-end) tests.

Every host provider (``mngr_aws``, ``mngr_gcp``, ``mngr_azure``, ``mngr_modal``, ...) ships a
``@pytest.mark.release`` test that drives the real ``mngr`` CLI against the real cloud
backend through one long-lived "trip" -- a single boot amortized across many assertions:

    create -> verify the cloud resource exists + is tagged -> exec a marker file
           -> plain stop (host stays up) -> stop --stop-host (real machine stop, OR a
              loud refusal where the provider does not support it) -> start
           -> the marker survived the stop/start -> snapshot (where supported)
           -> out-of-band "sketchy kill" -> discovery reflects it (CRASHED)
           -> gc -> the cloud backend is clean (nothing leaked)

The trip and its assertions are identical across providers; only the *plumbing* differs
(how the settings.toml selects the provider, how credentials are gated, how the cloud API
is probed, how a resource is force-stranded). This module owns the trip and the shared
assertions; each provider supplies a :class:`ProviderReleaseProfile` that owns its
plumbing. A single ``run_provider_release_trip1(profile, tmp_path, workspace)`` call is then
the whole release test, so the parity the ``specs/provider-release-tests.md`` proposal
describes is enforced executably.

The harness is deliberately isolation-agnostic: it speaks only through capability booleans
the profile declares (so it never imports ``mngr_vps``, which depends on ``mngr``). The
provider's own test file owns the ``IsolationMode`` parametrization and the settings.toml
shape, constructing one profile per (provider, isolation) pair.

Future trips (not yet implemented). This module ships Trip 1, Trip 2, Trip 3, and Trip 4. Still
owed, per ``specs/provider-release-tests.md``: Trip 1b (N agents per host), AND a dedicated
**offline host_dir** trip -- create with ``is_offline_host_dir_enabled=True``, write
a file into the host_dir, take the host offline (``stop --stop-host``), and assert that file is
readable *through the offline host_dir mirror* (the state bucket / metadata), not by reaching
the live host. Trip 1's ``stop-host`` -> ``start`` path already reads the offline host *record*
incidentally (that is what surfaced the Azure operator blob-RBAC gap), but nothing yet verifies
an offline host_dir *file* read end to end, which is the actual user-visible promise of the
offline-host_dir feature.

These tests are not run in CI (release-marked) and cost real money; run a provider's test
manually with credentials present, e.g.::

    MNGR_AWS_RELEASE_TESTS=1 just test \\
        libs/mngr_aws/imbue/mngr_aws/test_release_aws.py
"""

from __future__ import annotations

import abc
import os
import subprocess
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import pytest
from loguru import logger

from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr.utils.testing import run_mngr_subprocess

# Generous bounds for real provisioning. The trip is one test, so a profile widens these via
# its own ``@pytest.mark.timeout``; these only bound the individual subprocess calls / polls.
_CREATE_TIMEOUT_SECONDS: Final[float] = 600.0
_EXEC_TIMEOUT_SECONDS: Final[float] = 120.0
_LIFECYCLE_TIMEOUT_SECONDS: Final[float] = 180.0
_DESTROY_TIMEOUT_SECONDS: Final[float] = 180.0
# Cloud state transitions (stop -> HALTED, force-kill -> CRASHED, gc -> gone) are slow and
# eventually-consistent, so they are polled rather than asserted once.
_CLOUD_TRANSITION_TIMEOUT_SECONDS: Final[float] = 360.0
_CLOUD_POLL_INTERVAL_SECONDS: Final[float] = 10.0

# The marker is written under the mngr host_dir mount (``/mngr``), which is the one path that
# survives a host stop/start (and a container re-realize) across every provider shape -- a
# file in the container's own root would not survive a bare-vs-container or stop/start cycle.
_MARKER_HOST_PATH: Final[str] = "/mngr/trip1-marker.txt"
# host_dir-relative form of the Trip 1 marker (``_MARKER_HOST_PATH`` minus the ``/mngr`` mount),
# used by the opt-in offline read (`mngr file get <host> <relpath>`).
_MARKER_HOSTDIR_RELPATH: Final[str] = "trip1-marker.txt"
# Opt-in gate for Trip 1's offline-host_dir read. Off by default so Trip 1 stays the lean
# happy-path lifecycle; set to "1" to additionally assert that a stopped host's host_dir is
# readable from the offline mirror.
_OFFLINE_HOST_DIR_ENV_VAR: Final[str] = "MNGR_RELEASE_TEST_OFFLINE_HOST_DIR"

# The Trip 2 marker lives under ``/mngr`` (the host_dir mount) so it survives the auto-shutdown
# stop and the subsequent ``mngr start`` resume -- the same rationale as the Trip 1 marker.
_TRIP2_MARKER_HOST_PATH: Final[str] = "/mngr/trip2-marker.txt"

# The bare host store. On a bare (no-container) host the agent shell is the VM's own root, so this
# directory exists and ``/.dockerenv`` does not -- the signature Trip 1 checks for ``is_bare_host``
# profiles to prove the shape did not silently fall back to a container.
_BARE_HOST_STORE_PATH: Final[str] = "/var/lib/mngr-host"


class ProviderReleaseProfile(abc.ABC):
    """Per-provider plumbing for the shared provider release trip.

    Concrete profiles live in their own plugin's test module (so credential gates stay
    per-provider and the packages do not couple). One profile instance corresponds to one
    (provider, isolation-shape) pair; the provider's test file constructs the right one and
    sets the capability booleans accordingly.
    """

    # The ``mngr create --provider <provider_name>`` selector, also the settings.toml block name.
    provider_name: str
    # Prefix for the generated host name. Keeps each provider's orphan-scanner tag filter and
    # name-collision avoidance consistent with its existing release tests.
    name_prefix: str

    # Capability booleans the harness branches Trip 1 on. ``supports_shutdown_hosts`` decides
    # whether ``mngr stop --stop-host`` is expected to really stop the machine or to refuse
    # loudly. ``supports_snapshots`` is the *effective* value for this profile's shape (the
    # provider sets it False for a bare/no-container shape even when the container shape
    # supports snapshots). ``snapshot_survives_destroy`` is whether a snapshot taken before
    # ``destroy`` is still usable afterward (a portable snapshot) -- True only where snapshots
    # outlive the host (Modal); the container shape's ``docker commit`` snapshot dies with the
    # VPS, so it is False there. Only read by Trip 3, and only when ``supports_snapshots``.
    supports_shutdown_hosts: bool
    supports_snapshots: bool
    snapshot_survives_destroy: bool
    # Whether a stopped host's host_dir is readable from the offline mirror (captured to the state
    # bucket at ``mngr stop``). True for clouds with a real host_dir backend (AWS S3 / Azure Blob /
    # GCP GCS); Modal has no ``--stop-host`` window, so it stays False. Read only by Trip 1's opt-in
    # offline-host_dir step (gated by ``_OFFLINE_HOST_DIR_ENV_VAR``).
    supports_offline_host_dir: bool = False
    # Whether this profile's shape is a *bare* host -- the agent shell is the VM's own OS, with no
    # container. When True, Trip 1 asserts the no-container shape end to end (the bare host store
    # exists and there is no ``/.dockerenv``), the coverage the retired per-provider bare lifecycle
    # tests used to own. The VPS family sets it from its ``IsolationMode`` (NONE -> bare); Modal has
    # no bare shape, so it stays False.
    is_bare_host: bool = False

    # Capability boolean the harness branches Trip 2 (idle auto-shutdown) on.
    # ``resumes_after_auto_shutdown`` is whether the provider comes back from its idle
    # auto-shutdown state via ``mngr start`` (the cloud trio idle-stop into a resumable
    # state -- AWS stop, GCP TERMINATED, Azure deallocated -- so it is True there; Modal's
    # idle path lets the sandbox expire and be terminated by Modal's own timeout, with no
    # resume, so it is False). When False, Trip 2 asserts only the auto-shutdown (the
    # billing stop / sandbox gone) and skips the resume.
    resumes_after_auto_shutdown: bool

    # Capability booleans the harness branches Trip 4 (error classification) on.
    # ``has_curated_unavailable_help`` is whether the provider's missing-credential error carries
    # *curated*, provider-correct help text (mentioning ``credential_setup_command``); only Azure
    # does today, so the rest assert the documented divergence (the generic "start Docker" text, or
    # Modal's wrong error class) instead. ``raises_contract_unavailable_error`` is whether the
    # missing-credential error is the contract ``ProviderUnavailableError`` ("is not available")
    # rather than a provider-specific class (Modal raises ``ModalAuthError``); the divergent
    # providers assert their own surfaced message via ``unavailable_error_substring``.
    # ``supports_vps_migration_arg_check`` is whether a ``--vps-*`` build arg is rejected with the
    # shared migration error (the VPS family does; Modal has its own arg parser, so it skips that
    # scenario). ``credential_setup_command`` is the provider-correct setup command the curated
    # help text should point at (e.g. ``"az login"``); only meaningful when curated.
    has_curated_unavailable_help: bool
    raises_contract_unavailable_error: bool
    supports_vps_migration_arg_check: bool
    credential_setup_command: str
    # The stable user-facing substring the missing-credential error surfaces through the CLI.
    unavailable_error_substring: str

    @abc.abstractmethod
    def make_credentials_unresolvable_env(self) -> Mapping[str, str | None]:
        """Return env overrides that make this provider's credentials unresolvable (None removes a var)."""

    def write_credentials_unresolvable_settings(self, settings_dir: Path) -> None:
        """Write the Trip 4 missing-credential settings.toml; defaults to the normal settings.

        Most providers make credentials unresolvable purely via env overrides
        (``make_credentials_unresolvable_env``). Azure resolves its subscription from settings /
        env / the ``az`` CLI, and only a *missing subscription* reliably raises its curated
        unavailable error without a network call, so it overrides this to write a settings.toml
        with no ``subscription_id``.
        """
        self.write_settings(settings_dir)

    @abc.abstractmethod
    def unavailable_reason(self) -> str | None:
        """Return a skip reason if the provider can't run here (missing creds / opt-in), else None."""

    @abc.abstractmethod
    def write_settings(self, settings_dir: Path) -> None:
        """Write the release-test settings.toml selecting this provider + isolation into settings_dir."""

    @abc.abstractmethod
    def create_extra_args(self) -> Sequence[str]:
        """Provider-specific args appended to ``mngr create`` (e.g. instance size build args)."""

    def write_auto_shutdown_settings(self, settings_dir: Path) -> None:
        """Write the Trip 2 settings.toml (a short-auto-shutdown variant); defaults to ``write_settings``.

        Trip 2 drives the idle watcher (``mngr create --idle-timeout``) so the host self-stops
        without activity. The cloud trio's idle poweroff lands the machine in its resumable
        stopped state, but on AWS that requires ``InstanceInitiatedShutdownBehavior = stop``
        (the ``terminate_on_shutdown = false`` settings variant) so the poweroff STOPS rather
        than terminates the instance; AWS overrides this to write that variant. GCP/Azure idle
        into a resumable state without a settings tweak, and Modal has no resumable path, so
        they use the normal settings.
        """
        self.write_settings(settings_dir)

    @abc.abstractmethod
    def auto_shutdown_create_args(self) -> Sequence[str]:
        """Provider-specific ``mngr create`` args that make the host self-stop on the shortest interval.

        For the cloud trio this is the idle-watcher timeout (``--idle-timeout <secs>``): with no
        SSH connection the in-host watcher sees no activity and powers the machine off into its
        resumable stopped state. For Modal, which has no idle watcher, this caps the sandbox
        lifetime instead (``-b --timeout=<secs>``) so Modal's own timeout terminates it.
        """

    @abc.abstractmethod
    def find_launched_host_handle(self, host_name: str) -> str | None:
        """Return the cloud-side id of the host this test launched (named ``host_name``), or None.

        Providers that tag launched resources may ignore ``host_name`` and match the tag; those
        without such a tag (e.g. Modal) match on the host name, which is unique per trip.
        """

    @abc.abstractmethod
    def is_host_compute_running(self, handle: str) -> bool:
        """Probe the cloud API: is the host's compute (VM / sandbox) powered on and billing?"""

    @abc.abstractmethod
    def is_host_compute_stopped(self, handle: str) -> bool:
        """Probe the cloud API: is the host's compute genuinely stopped so billing has halted?"""

    @abc.abstractmethod
    def force_strand_host(self, handle: str) -> None:
        """Out-of-band kill the host's compute (bypassing ``mngr destroy``); must be idempotent."""

    @abc.abstractmethod
    def is_backend_clean(self, handle: str) -> bool:
        """Probe the cloud API: is the host's compute gone (no leaked instance / sandbox)?"""


def _run_mngr(
    settings_dir: Path,
    workspace: Path,
    *args: str,
    timeout: float,
    env_overrides: Mapping[str, str | None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a ``mngr`` command against the test settings.toml via the shared subprocess runner.

    Delegates to ``run_mngr_subprocess`` (the same helper the agent release harness uses), which
    streams the command's output live to the test log so a timed-out ``mngr create`` is still
    diagnosable. ``workspace`` is the cwd and must be inside a git repo (``mngr create`` reads
    the source from the current checkout). Per-provider credential preservation across the
    conftest HOME swap is already handled by each provider's autouse ``setup_test_mngr_env``
    fixture, so copying the current environment is sufficient here.

    ``env_overrides`` mutates the copied environment before the call: a string value sets the
    var, and ``None`` removes it. Trip 4 uses this to make a provider's credentials
    *unresolvable* (e.g. dropping the ``AWS_*`` vars the conftest froze in) so the
    missing-credential error path can be exercised without touching the real account.

    ``run_mngr_subprocess`` returns stdout and stderr separately, but mngr writes its errors and
    logs to stderr; the trip's assertions speak a single stream, so stderr is merged into the
    returned ``stdout``. A timeout is surfaced as a non-zero (124) result rather than raised.
    """
    env = os.environ.copy()
    env["MNGR_PROJECT_CONFIG_DIR"] = str(settings_dir)
    for key, value in (env_overrides or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    try:
        result = run_mngr_subprocess(*args, timeout=timeout, env=env, cwd=workspace)
    except subprocess.TimeoutExpired:
        # 124 is the GNU-coreutils ``timeout`` convention; the output already streamed to the log.
        return subprocess.CompletedProcess(
            args=("mngr", *args),
            returncode=124,
            stdout=f"`mngr {args[0] if args else 'cmd'}` timed out after {timeout}s (output streamed above)",
            stderr="",
        )
    return subprocess.CompletedProcess(
        args=result.args, returncode=result.returncode, stdout=result.stdout + result.stderr, stderr=""
    )


def _exec_on_host(
    settings_dir: Path,
    workspace: Path,
    host_name: str,
    shell_command: str,
) -> subprocess.CompletedProcess[str]:
    return _run_mngr(settings_dir, workspace, "exec", host_name, shell_command, timeout=_EXEC_TIMEOUT_SECONDS)


def _host_state_in_list(settings_dir: Path, workspace: Path, host_name: str) -> str | None:
    """Return the host state ``mngr list`` reports for ``host_name``, or None if it is absent.

    Reads the explicit ``host.state`` field so the assertion keys on the host lifecycle state
    (RUNNING / STOPPED / CRASHED / DESTROYED) rather than the agent's session state.
    """
    result = _run_mngr(
        settings_dir, workspace, "list", "--fields", "name,host.state", timeout=_LIFECYCLE_TIMEOUT_SECONDS
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if host_name in line:
            # The row is "<name> <HOST_STATE>"; the state is the last token.
            tokens = line.split()
            return tokens[-1] if tokens else None
    return None


def run_provider_release_trip1(
    profile: ProviderReleaseProfile,
    tmp_path: Path,
    workspace: Path,
) -> None:
    """Drive Trip 1 (the full create -> lifecycle -> sketchy-kill -> gc arc) for ``profile``.

    Skips (rather than fails) when the provider's credentials / opt-in are absent, so the
    test is a no-op without them. Every assertion reasonable for all providers runs
    uniformly; capability booleans gate the provider-specific branches.
    """
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    settings_dir = tmp_path
    profile.write_settings(settings_dir)
    host_name = f"{profile.name_prefix}{get_short_random_string()}"
    marker_token = f"trip1-{get_short_random_string()}"
    handle: str | None = None
    is_destroyed = False

    try:
        # 1. Create the host. A ``command`` agent (``-- sleep 99999``) keeps the trip about the
        #    provider lifecycle, not the agent: ``mngr exec`` works regardless of agent type.
        create = _run_mngr(
            settings_dir,
            workspace,
            "create",
            host_name,
            "--type",
            "command",
            "--provider",
            profile.provider_name,
            "--no-connect",
            *profile.create_extra_args(),
            "--",
            "sleep",
            "99999",
            timeout=_CREATE_TIMEOUT_SECONDS,
        )
        # returncode is the authoritative cross-provider success signal; the human-readable
        # wording of a successful create differs by provider (e.g. Modal does not print
        # "successfully"), so don't assert on it.
        assert create.returncode == 0, f"create failed:\n{create.stdout}"

        # 2. The cloud resource exists and is discoverable by name (proves tagging / identity).
        handle = profile.find_launched_host_handle(host_name)
        assert handle is not None, f"could not find the launched cloud resource for {host_name}"
        assert profile.is_host_compute_running(handle), "cloud resource should be running right after create"

        # 3. The host shows up RUNNING in discovery.
        assert _host_state_in_list(settings_dir, workspace, host_name) == "RUNNING", (
            f"host should be RUNNING in `mngr list`:\n"
            f"{_run_mngr(settings_dir, workspace, 'list', timeout=_LIFECYCLE_TIMEOUT_SECONDS).stdout}"
        )

        # 4. Write a marker file on the host and read it straight back.
        written = _exec_on_host(settings_dir, workspace, host_name, f"echo {marker_token} > {_MARKER_HOST_PATH}")
        assert written.returncode == 0, f"writing the marker failed:\n{written.stdout}"
        read_back = _exec_on_host(settings_dir, workspace, host_name, f"cat {_MARKER_HOST_PATH}")
        assert marker_token in read_back.stdout, f"marker not readable after write:\n{read_back.stdout}"

        # 4b. Bare shape: the agent shell is the VM's own root, not a container. Assert the bare
        #     host store exists and there is no `/.dockerenv`, so a NONE-isolation host that silently
        #     fell back to a container fails loudly. (Container shapes skip this: Modal's container
        #     equivalent is a sandbox, not a docker container, so `/.dockerenv` is not universal.)
        if profile.is_bare_host:
            bare_shape = _exec_on_host(
                settings_dir,
                workspace,
                host_name,
                f"test -d {_BARE_HOST_STORE_PATH} && test ! -e /.dockerenv && echo bare-confirmed",
            )
            assert "bare-confirmed" in bare_shape.stdout, (
                f"expected a bare (non-container) host -- {_BARE_HOST_STORE_PATH} present and no "
                f"/.dockerenv:\n{bare_shape.stdout}"
            )

        # 5. Plain stop stops only the agent's tmux; the host keeps running and the marker stays.
        stopped = _run_mngr(settings_dir, workspace, "stop", host_name, timeout=_LIFECYCLE_TIMEOUT_SECONDS)
        assert stopped.returncode == 0, f"plain stop failed:\n{stopped.stdout}"
        assert profile.is_host_compute_running(handle), "host compute should still run after a plain `mngr stop`"
        still_there = _exec_on_host(settings_dir, workspace, host_name, f"cat {_MARKER_HOST_PATH}")
        assert marker_token in still_there.stdout, f"marker lost after plain stop:\n{still_there.stdout}"

        # 6. `mngr stop --stop-host`: a real machine stop where supported, a loud refusal where not.
        if profile.supports_shutdown_hosts:
            host_stopped = _run_mngr(
                settings_dir, workspace, "stop", host_name, "--stop-host", timeout=_LIFECYCLE_TIMEOUT_SECONDS
            )
            assert host_stopped.returncode == 0, f"stop --stop-host failed:\n{host_stopped.stdout}"
            wait_for(
                lambda: profile.is_host_compute_stopped(handle),
                timeout=_CLOUD_TRANSITION_TIMEOUT_SECONDS,
                poll_interval=_CLOUD_POLL_INTERVAL_SECONDS,
                error_message="host compute did not stop (billing should halt) after `mngr stop --stop-host`",
            )
            # Opt-in offline-host_dir read (kept out of the default happy path behind an env var so
            # it never destabilizes Trip 1's core lifecycle). `mngr stop --stop-host` captured the
            # host_dir to the state bucket, so while the host is genuinely stopped `mngr file get`
            # must serve the marker from that offline mirror -- and must not start the host to do it.
            if os.environ.get(_OFFLINE_HOST_DIR_ENV_VAR) == "1" and profile.supports_offline_host_dir:
                # `--relative-to host` resolves the path against the host_dir (the captured volume);
                # the default work-dir base is not served offline.
                offline_read = _run_mngr(
                    settings_dir,
                    workspace,
                    "file",
                    "get",
                    host_name,
                    _MARKER_HOSTDIR_RELPATH,
                    "--relative-to",
                    "host",
                    timeout=_LIFECYCLE_TIMEOUT_SECONDS,
                )
                assert marker_token in offline_read.stdout, (
                    f"offline host_dir read did not serve the marker while the host was stopped:\n"
                    f"{offline_read.stdout}"
                )
                assert profile.is_host_compute_stopped(handle), (
                    "the offline host_dir read must not have started the host"
                )
        else:
            refused = _run_mngr(
                settings_dir, workspace, "stop", host_name, "--stop-host", timeout=_LIFECYCLE_TIMEOUT_SECONDS
            )
            assert refused.returncode != 0, f"stop --stop-host should be refused but succeeded:\n{refused.stdout}"
            # The CLI surfaces HostShutdownNotSupportedError as its user-facing message rather
            # than the class name; key on that stable phrase.
            assert "does not support stopping hosts" in refused.stdout.lower(), (
                f"expected a host-shutdown-not-supported refusal:\n{refused.stdout}"
            )

        # 7. Start brings the host back (and is idempotent), then the marker must have survived.
        started = _run_mngr(
            settings_dir, workspace, "start", host_name, "--no-connect", timeout=_CREATE_TIMEOUT_SECONDS
        )
        assert started.returncode == 0, f"start failed:\n{started.stdout}"
        if profile.supports_shutdown_hosts:
            wait_for(
                lambda: profile.is_host_compute_running(handle),
                timeout=_CLOUD_TRANSITION_TIMEOUT_SECONDS,
                poll_interval=_CLOUD_POLL_INTERVAL_SECONDS,
                error_message="host compute did not come back running after `mngr start`",
            )
        started_again = _run_mngr(
            settings_dir, workspace, "start", host_name, "--no-connect", timeout=_LIFECYCLE_TIMEOUT_SECONDS
        )
        assert started_again.returncode == 0, f"second `mngr start` (idempotency) failed:\n{started_again.stdout}"
        survived = _exec_on_host(settings_dir, workspace, host_name, f"cat {_MARKER_HOST_PATH}")
        assert marker_token in survived.stdout, f"marker did not survive stop/start:\n{survived.stdout}"

        # 8. Snapshot create + list, where this shape supports snapshots (skipped on bare shapes).
        if profile.supports_snapshots:
            snapshot_created = _run_mngr(
                settings_dir, workspace, "snapshot", "create", host_name, timeout=_CREATE_TIMEOUT_SECONDS
            )
            assert snapshot_created.returncode == 0, f"snapshot create failed:\n{snapshot_created.stdout}"
            snapshot_listed = _run_mngr(
                settings_dir, workspace, "snapshot", "list", host_name, timeout=_LIFECYCLE_TIMEOUT_SECONDS
            )
            assert snapshot_listed.returncode == 0, f"snapshot list failed:\n{snapshot_listed.stdout}"
        else:
            logger.info(
                "Skipped snapshot step: provider {} reports no snapshot support for this shape", profile.provider_name
            )

        # 9. Sketchy kill: terminate the compute out of band, bypassing `mngr destroy`.
        profile.force_strand_host(handle)

        # 10. Discovery reflects the kill: the host stays visible but turns CRASHED.
        wait_for(
            lambda: _host_state_in_list(settings_dir, workspace, host_name) in ("CRASHED", None),
            timeout=_CLOUD_TRANSITION_TIMEOUT_SECONDS,
            poll_interval=_CLOUD_POLL_INTERVAL_SECONDS,
            error_message="`mngr list` did not reflect the out-of-band kill (expected CRASHED)",
        )

        # 11. gc reclaims the stranded host and the cloud backend ends up clean.
        collected = _run_mngr(
            settings_dir, workspace, "gc", "--provider", profile.provider_name, timeout=_CREATE_TIMEOUT_SECONDS
        )
        assert collected.returncode == 0, f"gc failed:\n{collected.stdout}"
        wait_for(
            lambda: profile.is_backend_clean(handle),
            timeout=_CLOUD_TRANSITION_TIMEOUT_SECONDS,
            poll_interval=_CLOUD_POLL_INTERVAL_SECONDS,
            error_message="cloud backend still shows the host after gc (resource leaked)",
        )
        is_destroyed = True
    finally:
        # Best-effort cleanup: destroy through mngr, then force-strand as a backstop so a
        # failed/partial run cannot leak compute between iterative local runs. The session-end
        # orphan scanner in each provider's conftest is the final net.
        if not is_destroyed:
            _run_mngr(settings_dir, workspace, "destroy", host_name, "--force", timeout=_DESTROY_TIMEOUT_SECONDS)
        if handle is not None:
            profile.force_strand_host(handle)


def run_provider_release_trip2(
    profile: ProviderReleaseProfile,
    tmp_path: Path,
    workspace: Path,
) -> None:
    """Drive Trip 2 ("idle auto-shutdown contract") for ``profile``.

    Asserts the provider's auto-shutdown honestly stops billing: create a host that self-stops
    on the shortest reliable interval (the idle watcher for the cloud trio, the sandbox
    lifetime cap for Modal), then poll the cloud API until the compute genuinely stops/halts
    (the billing-stop probe). Where the provider resumes from that stopped state (the cloud
    trio), a marker written before shutdown must survive ``mngr start`` and the host must be
    running again; where it does not (Modal: the sandbox is gone), the trip asserts the
    shutdown only and skips the resume.

    The shutdown is driven by the idle watcher (``mngr create --idle-timeout``) rather than the
    ``auto_shutdown_seconds`` time cap, because the watcher's poweroff lands the cloud trio in a
    *resumable* stopped state (AWS stop / GCP TERMINATED / Azure deallocated), which the spec's
    resume step requires -- the release-test time cap, by contrast, terminates/deletes the
    instance (self-cleaning, but not resumable). This is the same path the per-provider idle
    tests Trip 2 unifies already exercise.

    Skips (rather than fails) when the provider's credentials / opt-in are absent.
    """
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    settings_dir = tmp_path
    profile.write_auto_shutdown_settings(settings_dir)
    host_name = f"{profile.name_prefix}{get_short_random_string()}"
    marker_token = f"trip2-{get_short_random_string()}"
    handle: str | None = None

    try:
        # 1. Create the host with a short auto-shutdown and no SSH connection, so the idle
        #    watcher sees no activity and self-stops (or the sandbox lifetime cap expires).
        create = _run_mngr(
            settings_dir,
            workspace,
            "create",
            host_name,
            "--type",
            "command",
            "--provider",
            profile.provider_name,
            "--no-connect",
            *profile.auto_shutdown_create_args(),
            *profile.create_extra_args(),
            "--",
            "sleep",
            "99999",
            timeout=_CREATE_TIMEOUT_SECONDS,
        )
        assert create.returncode == 0, f"create failed:\n{create.stdout}"

        # 2. The cloud resource exists and is running right after create.
        handle = profile.find_launched_host_handle(host_name)
        assert handle is not None, f"could not find the launched cloud resource for {host_name}"
        assert profile.is_host_compute_running(handle), "cloud resource should be running right after create"

        # 3. Where the provider resumes after auto-shutdown, write a marker BEFORE it stops so the
        #    resume step can prove it survived. (Modal has no resume, so there is nothing to check
        #    a marker against -- writing it would also just risk resetting the idle timer.)
        if profile.resumes_after_auto_shutdown:
            written = _exec_on_host(
                settings_dir, workspace, host_name, f"echo {marker_token} > {_TRIP2_MARKER_HOST_PATH}"
            )
            assert written.returncode == 0, f"writing the pre-shutdown marker failed:\n{written.stdout}"

        # 4. Wait for the auto-shutdown to genuinely stop the compute (billing-stop probe). For the
        #    cloud trio this is the HALTED state; for Modal the sandbox is gone (its is_backend_clean
        #    probe), so use whichever signal the provider's auto-shutdown produces.
        if profile.resumes_after_auto_shutdown:
            wait_for(
                lambda: profile.is_host_compute_stopped(handle),
                timeout=_CLOUD_TRANSITION_TIMEOUT_SECONDS,
                poll_interval=_CLOUD_POLL_INTERVAL_SECONDS,
                error_message="host compute did not auto-stop on idle (billing should halt)",
            )
        else:
            wait_for(
                lambda: profile.is_backend_clean(handle),
                timeout=_CLOUD_TRANSITION_TIMEOUT_SECONDS,
                poll_interval=_CLOUD_POLL_INTERVAL_SECONDS,
                error_message="sandbox was not terminated by its own timeout on auto-shutdown",
            )

        # 5. Resume from the auto-shutdown state, where supported, and assert the marker survived.
        #    A resumed host must not immediately re-stop, so confirm it stays running and the
        #    post-resume exec works (this is the stale-idle-sentinel regression the per-provider
        #    idle tests guard).
        if profile.resumes_after_auto_shutdown:
            started = _run_mngr(
                settings_dir, workspace, "start", host_name, "--no-connect", timeout=_CREATE_TIMEOUT_SECONDS
            )
            assert started.returncode == 0, f"start after auto-shutdown failed:\n{started.stdout}"
            wait_for(
                lambda: profile.is_host_compute_running(handle),
                timeout=_CLOUD_TRANSITION_TIMEOUT_SECONDS,
                poll_interval=_CLOUD_POLL_INTERVAL_SECONDS,
                error_message="host compute did not come back running after `mngr start`",
            )
            survived = _exec_on_host(settings_dir, workspace, host_name, f"cat {_TRIP2_MARKER_HOST_PATH}")
            assert marker_token in survived.stdout, (
                f"marker did not survive the auto-shutdown stop/start:\n{survived.stdout}"
            )
        else:
            logger.info(
                "Skipped resume step: provider {} does not resume after auto-shutdown (sandbox terminated)",
                profile.provider_name,
            )
    finally:
        # Best-effort cleanup: destroy through mngr, then force-strand as a backstop so a
        # failed/partial run cannot leak compute between iterative local runs.
        _run_mngr(settings_dir, workspace, "destroy", host_name, "--force", timeout=_DESTROY_TIMEOUT_SECONDS)
        if handle is not None:
            profile.force_strand_host(handle)


# A build arg using the dropped shared ``--vps-*`` prefix. Every VPS-family provider's build-arg
# parser routes this through ``raise_if_vps_migration_arg``, which raises an ``MngrError`` whose
# message carries the migration hint -- a synchronous, no-network arg-validation failure.
_VPS_MIGRATION_BUILD_ARG: Final[str] = "--vps-region=trip4-bogus-region"


def _curated_help_text(cli_output: str) -> str:
    """Return the bracketed ``user_help_text`` from a rendered ``Error: <msg>  [<help>]``, or "".

    ``MngrError.show`` formats the curated help as a trailing ``[...]`` block. Trip 4 keys the
    "is the help text provider-correct?" check on *that* block specifically -- not the whole
    message -- because a provider's error *reason* may already echo the setup command (e.g. the
    google-auth ``DefaultCredentialsError`` text names ``gcloud auth application-default login``)
    even when the curated guidance is still the generic "start Docker" default.
    """
    open_index = cli_output.find("[")
    close_index = cli_output.rfind("]")
    if open_index == -1 or close_index <= open_index:
        return ""
    return cli_output[open_index + 1 : close_index]


def run_provider_release_trip4(
    profile: ProviderReleaseProfile,
    tmp_path: Path,
    workspace: Path,
) -> None:
    """Drive Trip 4 ("error classification contract") for ``profile`` -- a pure no-boot CLI exercise.

    Asserts the CLI surfaces the right error class / curated help for each failure mode without
    ever provisioning a host (so it costs no compute and runs in seconds):

    1. Missing credentials. ``mngr create`` resolves the provider eagerly, so a run with the
       provider's credentials made unresolvable surfaces the credential error (non-zero exit).
       The contract class is ``ProviderUnavailableError`` ("is not available"); where the provider
       diverges (Modal's create-bootstrap surfaces a plain ``MngrError`` rather than the contract
       class) the profile declares it via ``raises_contract_unavailable_error`` and the assertion
       keys on its own surfaced message instead. The curated help text is provider-correct only
       where the profile declares ``has_curated_unavailable_help``; elsewhere the documented
       divergence (the generic "start Docker" guidance) is asserted so the test flips loudly once
       the help text is fixed.
    2. Build arg with the dropped ``--vps-*`` prefix. Where the provider's parser routes through
       the shared migration check (the VPS family), ``mngr create`` fails synchronously with the
       migration hint -- before any network call -- so the user is pointed at the per-provider
       flag. Skipped for providers with their own arg parser (Modal).

    The ``--stop-host``-on-an-unsupported-provider refusal that the spec lists under Trip 4 is
    *not* exercised here: the CLI resolves the target host before the capability gate, so it
    needs a real booted host and is already covered by Trip 1's refusal branch (Modal).

    Skips (rather than fails) when the provider's credentials / opt-in are absent.
    """
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    settings_dir = tmp_path
    host_name = f"{profile.name_prefix}{get_short_random_string()}"

    # 1. Missing credentials -> the provider's credential error surfaces from `mngr create`. Some
    #    providers (Azure) need a settings.toml variant for this case, so use the dedicated hook.
    profile.write_credentials_unresolvable_settings(settings_dir)
    missing_creds = _run_mngr(
        settings_dir,
        workspace,
        "create",
        host_name,
        "--type",
        "command",
        "--provider",
        profile.provider_name,
        "--no-connect",
        "--",
        "sleep",
        "99999",
        timeout=_LIFECYCLE_TIMEOUT_SECONDS,
        env_overrides=profile.make_credentials_unresolvable_env(),
    )
    assert missing_creds.returncode != 0, (
        f"`mngr create` should fail with credentials unresolvable, but it succeeded:\n{missing_creds.stdout}"
    )
    assert profile.unavailable_error_substring.lower() in missing_creds.stdout.lower(), (
        f"expected the missing-credential error to mention "
        f"{profile.unavailable_error_substring!r}:\n{missing_creds.stdout}"
    )
    if profile.raises_contract_unavailable_error:
        # The contract error is `ProviderUnavailableError`, whose user-facing message is the same
        # "is not available" phrase across providers; assert it so a regression to a non-contract
        # class is caught.
        assert "is not available" in missing_creds.stdout.lower(), (
            f"expected the contract ProviderUnavailableError ('is not available'):\n{missing_creds.stdout}"
        )
    setup_command = profile.credential_setup_command.lower()
    # Where the provider raises the contract `ProviderUnavailableError`, its curated guidance is
    # the trailing `[...]` block, so key the help-text check on that block (a provider whose error
    # *reason* happens to echo the setup command -- e.g. GCP's google-auth message -- must not be
    # mistaken for curated help). Providers that diverge on the error *class* (Modal raises a plain
    # MngrError with no `[...]` block) are checked against the whole surfaced message instead.
    help_haystack = (
        _curated_help_text(missing_creds.stdout) if profile.raises_contract_unavailable_error else missing_creds.stdout
    ).lower()
    if profile.has_curated_unavailable_help:
        assert setup_command in help_haystack, (
            f"help text should point at the provider-correct command "
            f"{profile.credential_setup_command!r}:\n{missing_creds.stdout}"
        )
    else:
        # Documented divergence: the curated help falls through to the generic "start Docker"
        # default rather than the provider-correct command. Assert it is absent, so the test fails
        # loudly the moment curated help lands (flip ``has_curated_unavailable_help`` then).
        assert setup_command not in help_haystack, (
            f"help text now mentions {profile.credential_setup_command!r}; set "
            f"has_curated_unavailable_help=True for {profile.provider_name}:\n{missing_creds.stdout}"
        )

    # 2. A `--vps-*` build arg is rejected with the migration hint (VPS family only). This needs
    #    valid credentials (the arg is parsed inside `create_host`, after the provider resolves),
    #    so restore the normal settings.toml first.
    if profile.supports_vps_migration_arg_check:
        profile.write_settings(settings_dir)
        migration = _run_mngr(
            settings_dir,
            workspace,
            "create",
            host_name,
            "--type",
            "command",
            "--provider",
            profile.provider_name,
            "--no-connect",
            "-b",
            _VPS_MIGRATION_BUILD_ARG,
            "--",
            "sleep",
            "99999",
            timeout=_LIFECYCLE_TIMEOUT_SECONDS,
        )
        assert migration.returncode != 0, (
            f"`mngr create` with a --vps-* build arg should be rejected:\n{migration.stdout}"
        )
        # The migration error's user-facing text (errors.py / build_args.py): keying on this stable
        # phrase rather than the class name, which the CLI never prints.
        assert "no longer supported" in migration.stdout.lower(), (
            f"expected the build-arg migration refusal:\n{migration.stdout}"
        )
        assert "build args are now per-provider" in migration.stdout.lower(), (
            f"expected the migration hint pointing at the per-provider flags:\n{migration.stdout}"
        )


# A unique prefix on the snapshot ``--format`` template so the id can be picked out of the
# merged stdout/stderr stream that ``_run_mngr`` returns.
_SNAPID_SENTINEL: Final[str] = "trip3-snapid="
# The snapshot must capture this marker, so it lives on the host's own filesystem (the container
# / sandbox root), NOT the ``/mngr`` volume mount -- a docker-commit snapshot does not capture
# the volume, only the writable container layer.
_SNAPSHOT_MARKER_PATH: Final[str] = "/root/trip3-marker.txt"


def _snapshot_ids_in_output(output: str) -> list[str]:
    """Return the snapshot ids printed via the ``trip3-snapid={...}`` format sentinel."""
    ids: list[str] = []
    for line in output.splitlines():
        marker_index = line.find(_SNAPID_SENTINEL)
        if marker_index != -1:
            ids.append(line[marker_index + len(_SNAPID_SENTINEL) :].strip())
    return ids


def run_provider_release_trip3(
    profile: ProviderReleaseProfile,
    tmp_path: Path,
    workspace: Path,
) -> None:
    """Drive Trip 3 ("snapshot survives destroy") for ``profile``.

    Asserts a snapshot is *portable*: it outlives ``mngr destroy`` and can seed a fresh
    ``mngr create --snapshot``, which restores the captured filesystem. Where the provider's
    snapshot is not portable (the container shape's ``docker commit`` lives on the VPS disk and
    dies with it), the trip instead asserts that documented divergence -- the snapshot record is
    *gone* after destroy -- so the test flips loudly the moment snapshots become portable there.

    Skipped where the shape has no snapshots at all (the bare realizer; providers without
    snapshot support), keyed on ``supports_snapshots``.
    """
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)
    if not profile.supports_snapshots:
        pytest.skip(f"shape does not support snapshots: {profile.provider_name}")

    settings_dir = tmp_path
    profile.write_settings(settings_dir)
    host_name = f"{profile.name_prefix}{get_short_random_string()}"
    restored_name = f"{profile.name_prefix}r-{get_short_random_string()}"
    marker_token = f"trip3-{get_short_random_string()}"
    snapshot_id: str | None = None
    is_host_destroyed = False
    is_restored_created = False

    try:
        # 1. Create the host and write a marker into its own filesystem (so the snapshot captures it).
        create = _run_mngr(
            settings_dir,
            workspace,
            "create",
            host_name,
            "--type",
            "command",
            "--provider",
            profile.provider_name,
            "--no-connect",
            *profile.create_extra_args(),
            "--",
            "sleep",
            "99999",
            timeout=_CREATE_TIMEOUT_SECONDS,
        )
        assert create.returncode == 0, f"create failed:\n{create.stdout}"
        written = _exec_on_host(settings_dir, workspace, host_name, f"echo {marker_token} > {_SNAPSHOT_MARKER_PATH}")
        assert written.returncode == 0, f"writing the marker failed:\n{written.stdout}"

        # 2. Snapshot the host and capture the new snapshot id (human output omits it; use --format).
        created = _run_mngr(
            settings_dir,
            workspace,
            "snapshot",
            "create",
            host_name,
            "--format",
            f"{_SNAPID_SENTINEL}{{snapshot_id}}",
            timeout=_CREATE_TIMEOUT_SECONDS,
        )
        assert created.returncode == 0, f"snapshot create failed:\n{created.stdout}"
        created_ids = _snapshot_ids_in_output(created.stdout)
        assert len(created_ids) == 1, f"expected exactly one created snapshot id, got {created_ids}:\n{created.stdout}"
        snapshot_id = created_ids[0]

        # 3. The snapshot is listed for the host before destroy (true for every snapshot provider).
        listed = _run_mngr(
            settings_dir,
            workspace,
            "snapshot",
            "list",
            host_name,
            "--format",
            f"{_SNAPID_SENTINEL}{{id}}",
            timeout=_LIFECYCLE_TIMEOUT_SECONDS,
        )
        assert snapshot_id in _snapshot_ids_in_output(listed.stdout), (
            f"snapshot {snapshot_id} not listed before destroy:\n{listed.stdout}"
        )

        # 4. Destroy the host.
        destroyed = _run_mngr(
            settings_dir, workspace, "destroy", host_name, "--force", timeout=_DESTROY_TIMEOUT_SECONDS
        )
        assert destroyed.returncode == 0, f"destroy failed:\n{destroyed.stdout}"
        is_host_destroyed = True

        # 5. Probe whether the snapshot is portable across the destroy.
        if profile.snapshot_survives_destroy:
            # The real, user-facing promise of a portable snapshot is that a fresh host created
            # from it carries the captured marker. (Probe restore, not `snapshot list`: that list
            # is host-scoped and omits a destroyed host's snapshots even when the image itself
            # persists -- e.g. Modal.)
            restored = _run_mngr(
                settings_dir,
                workspace,
                "create",
                restored_name,
                "--type",
                "command",
                "--provider",
                profile.provider_name,
                "--no-connect",
                "--snapshot",
                snapshot_id,
                *profile.create_extra_args(),
                "--",
                "sleep",
                "99999",
                timeout=_CREATE_TIMEOUT_SECONDS,
            )
            assert restored.returncode == 0, f"create --snapshot failed:\n{restored.stdout}"
            is_restored_created = True
            recovered = _exec_on_host(settings_dir, workspace, restored_name, f"cat {_SNAPSHOT_MARKER_PATH}")
            assert marker_token in recovered.stdout, (
                f"portable snapshot did not restore the marker file:\n{recovered.stdout}"
            )
        else:
            # Documented divergence: the container shape's docker-commit snapshot lives on the
            # VPS disk and dies with it, so its record is gone from `snapshot list` after destroy.
            # Assert that, so the test fails loudly (-> flip snapshot_survives_destroy) the moment
            # snapshots become portable here.
            after = _run_mngr(
                settings_dir,
                workspace,
                "snapshot",
                "list",
                "--format",
                f"{_SNAPID_SENTINEL}{{id}}",
                timeout=_LIFECYCLE_TIMEOUT_SECONDS,
            )
            assert snapshot_id not in _snapshot_ids_in_output(after.stdout), (
                f"snapshot {snapshot_id} unexpectedly survived destroy for {profile.provider_name}; "
                "if snapshots are now portable here, set snapshot_survives_destroy=True"
            )
    finally:
        if not is_host_destroyed:
            _run_mngr(settings_dir, workspace, "destroy", host_name, "--force", timeout=_DESTROY_TIMEOUT_SECONDS)
        if is_restored_created:
            _run_mngr(settings_dir, workspace, "destroy", restored_name, "--force", timeout=_DESTROY_TIMEOUT_SECONDS)
        if snapshot_id is not None and profile.snapshot_survives_destroy:
            _run_mngr(
                settings_dir,
                workspace,
                "snapshot",
                "destroy",
                "--snapshot",
                snapshot_id,
                "--force",
                timeout=_LIFECYCLE_TIMEOUT_SECONDS,
            )
