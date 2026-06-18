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

Future trips (not yet implemented). This module currently ships Trip 1 only. Still owed,
per ``specs/provider-release-tests.md``: Trip 1b (N agents per host), Trip 2 (idle
auto-shutdown), Trip 3 (snapshot survives destroy), Trip 4 (error classification), AND a
dedicated **offline host_dir** trip -- create with ``is_offline_host_dir_enabled=True``, write
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
    # supports snapshots).
    supports_shutdown_hosts: bool
    supports_snapshots: bool

    @abc.abstractmethod
    def unavailable_reason(self) -> str | None:
        """Return a skip reason if the provider can't run here (missing creds / opt-in), else None."""

    @abc.abstractmethod
    def write_settings(self, settings_dir: Path) -> None:
        """Write the release-test settings.toml selecting this provider + isolation into settings_dir."""

    @abc.abstractmethod
    def create_extra_args(self) -> Sequence[str]:
        """Provider-specific args appended to ``mngr create`` (e.g. instance size build args)."""

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
) -> subprocess.CompletedProcess[str]:
    """Run a ``mngr`` command against the test settings.toml via the shared subprocess runner.

    Delegates to ``run_mngr_subprocess`` (the same helper the agent release harness uses), which
    streams the command's output live to the test log so a timed-out ``mngr create`` is still
    diagnosable. ``workspace`` is the cwd and must be inside a git repo (``mngr create`` reads
    the source from the current checkout). Per-provider credential preservation across the
    conftest HOME swap is already handled by each provider's autouse ``setup_test_mngr_env``
    fixture, so copying the current environment is sufficient here.

    ``run_mngr_subprocess`` returns stdout and stderr separately, but mngr writes its errors and
    logs to stderr; the trip's assertions speak a single stream, so stderr is merged into the
    returned ``stdout``. A timeout is surfaced as a non-zero (124) result rather than raised.
    """
    env = os.environ.copy()
    env["MNGR_PROJECT_CONFIG_DIR"] = str(settings_dir)
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
