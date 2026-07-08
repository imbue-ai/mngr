"""Unit tests for the providers-panel SSE payload builders.

Covers ``_build_providers_state_payload`` (combines resolver-tracked providers,
errored providers, and disabled-on-disk providers into the SSE payload) and
``_build_workspace_list`` (stale marking + color emission).

Provider enable/disable now lives on ``PATCH /api/v1/desktop/providers/<name>``
and is tested in ``api_v1_test.py``.
"""

from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.desktop_client.app import _build_providers_state_payload
from imbue.minds.desktop_client.app import _build_workspace_list
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import ParsedAgentsResult
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import seed_provider_snapshots
from imbue.minds.desktop_client.workspace_color import DEFAULT_WORKSPACE_COLOR
from imbue.minds.testing import stub_mngr_host_dir
from imbue.mngr.api.discovery_events import DiscoveredProvider
from imbue.mngr.api.discovery_events import DiscoveryError
from imbue.mngr.api.discovery_events import make_discovered_provider
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName

_ROOT_NAME = "minds-dev-tname"


# -- _build_providers_state_payload ----------------------------------------


def _make_discovered_provider(name: str, backend: str = "docker") -> DiscoveredProvider:
    return make_discovered_provider(
        ProviderInstanceName(name),
        ProviderInstanceConfig(backend=ProviderBackendName(backend), is_enabled=True),
    )


def test_build_providers_state_payload_returns_empty_for_non_mngr_resolver() -> None:
    """A non-``MngrCliBackendResolver`` resolver yields an empty payload (defensive default)."""
    resolver = StaticBackendResolver(url_by_agent_and_service={})

    payload = _build_providers_state_payload(resolver)

    assert payload == {"providers": [], "last_event_at": None, "last_full_snapshot_at": None}


def test_build_providers_state_payload_hides_local_provider() -> None:
    """The ``local`` provider is filtered out of the panel even when reported as healthy."""
    resolver = MngrCliBackendResolver()
    now = datetime.now(timezone.utc)
    seed_provider_snapshots(
        resolver,
        providers=(_make_discovered_provider("local"),),
        error_by_provider_name={},
        last_snapshot_at=now,
    )

    payload = _build_providers_state_payload(resolver)

    assert payload["providers"] == []
    assert payload["last_full_snapshot_at"] == now.isoformat()
    assert payload["last_event_at"] == now.isoformat()


def test_build_providers_state_payload_hides_default_imbue_cloud_provider() -> None:
    """The default ``imbue_cloud`` singleton is hidden -- minds uses per-account variants."""
    resolver = MngrCliBackendResolver()
    now = datetime.now(timezone.utc)
    seed_provider_snapshots(
        resolver,
        providers=(_make_discovered_provider("imbue_cloud", backend="imbue_cloud"),),
        error_by_provider_name={},
        last_snapshot_at=now,
    )

    payload = _build_providers_state_payload(resolver)

    assert payload["providers"] == []


def test_build_providers_state_payload_keeps_per_account_imbue_cloud_provider() -> None:
    """Per-account ``imbue_cloud_<slug>`` providers are NOT hidden; only the default is."""
    resolver = MngrCliBackendResolver()
    now = datetime.now(timezone.utc)
    seed_provider_snapshots(
        resolver,
        providers=(_make_discovered_provider("imbue_cloud_alice-example-com", backend="imbue_cloud"),),
        error_by_provider_name={},
        last_snapshot_at=now,
    )

    payload = _build_providers_state_payload(resolver)

    assert [entry["name"] for entry in payload["providers"]] == ["imbue_cloud_alice-example-com"]


def test_build_providers_state_payload_combines_ok_error_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Healthy + errored + disabled providers all appear, sorted alphabetically."""
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, _ROOT_NAME)
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, _ROOT_NAME)
    # Seed a [providers.docker] is_enabled=false block on disk so
    # list_disabled_provider_names() surfaces it.
    settings_path.write_text("[providers.docker]\nis_enabled = false\n")

    errored_name = ProviderInstanceName("modal")
    resolver = MngrCliBackendResolver()
    seed_provider_snapshots(
        resolver,
        providers=(_make_discovered_provider("zzz_last"), _make_discovered_provider("local")),
        error_by_provider_name={
            errored_name: DiscoveryError(
                type_name="ImbueCloudAuthError",
                message="token missing",
                provider_name=errored_name,
            ),
        },
        last_snapshot_at=datetime.now(timezone.utc),
    )

    payload = _build_providers_state_payload(resolver)
    names = [entry["name"] for entry in payload["providers"]]

    # `local` is hidden; the rest are sorted alphabetically across all categories.
    assert names == ["docker", "modal", "zzz_last"]
    by_name = {entry["name"]: entry for entry in payload["providers"]}
    assert by_name["docker"]["status"] == "disabled"
    assert by_name["docker"]["is_enabled"] is False
    assert by_name["modal"]["status"] == "error"
    assert by_name["modal"]["error_type"] == "ImbueCloudAuthError"
    assert by_name["modal"]["error_message"] == "token missing"
    assert by_name["zzz_last"]["status"] == "ok"
    assert by_name["zzz_last"]["backend"] == "docker"


def test_build_providers_state_payload_dedups_provider_appearing_in_multiple_buckets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the same name shows up in both the resolver's errored set and the on-disk disabled set,
    only the disabled entry should appear (user intent wins over transient error state).

    This happens during the window between the user clicking Disable on an errored provider and
    mngr observe restarting with the new is_enabled=false setting.
    """
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, _ROOT_NAME)
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, _ROOT_NAME)
    settings_path.write_text("[providers.modal]\nis_enabled = false\n")

    errored_name = ProviderInstanceName("modal")
    resolver = MngrCliBackendResolver()
    seed_provider_snapshots(
        resolver,
        providers=(),
        error_by_provider_name={
            errored_name: DiscoveryError(
                type_name="ImbueCloudAuthError",
                message="token missing",
                provider_name=errored_name,
            ),
        },
        last_snapshot_at=datetime.now(timezone.utc),
    )

    payload = _build_providers_state_payload(resolver)
    names = [entry["name"] for entry in payload["providers"]]

    # Only one entry per provider name, with disabled winning over error.
    assert names == ["modal"]
    assert payload["providers"][0]["status"] == "disabled"
    assert payload["providers"][0]["is_enabled"] is False


def test_build_providers_state_payload_dedups_healthy_provider_also_in_disabled_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Healthy-in-snapshot + disabled-on-disk should yield only the disabled entry (user intent wins)."""
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, _ROOT_NAME)
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, _ROOT_NAME)
    settings_path.write_text("[providers.docker]\nis_enabled = false\n")

    resolver = MngrCliBackendResolver()
    seed_provider_snapshots(
        resolver,
        providers=(_make_discovered_provider("docker"),),
        error_by_provider_name={},
        last_snapshot_at=datetime.now(timezone.utc),
    )

    payload = _build_providers_state_payload(resolver)
    names = [entry["name"] for entry in payload["providers"]]

    assert names == ["docker"]
    assert payload["providers"][0]["status"] == "disabled"


# -- _build_workspace_list stale marking --


def _make_workspace_agent(provider_name: str, extra_labels: dict[str, str] | None = None) -> DiscoveredAgent:
    """A primary workspace agent (carries the workspace + is_primary labels)."""
    labels = {"workspace": "my-workspace", "is_primary": "true", **(extra_labels or {})}
    return DiscoveredAgent(
        host_id=HostId("host-" + "0" * 31 + "1"),
        agent_id=AgentId("agent-" + "0" * 31 + "1"),
        agent_name=AgentName("ws-agent"),
        provider_name=ProviderInstanceName(provider_name),
        certified_data={"labels": labels},
    )


def test_build_workspace_list_marks_workspace_stale_when_its_provider_errored() -> None:
    """A retained workspace whose provider's last poll errored is flagged ``is_stale``; healthy ones are not."""
    resolver = MngrCliBackendResolver()
    provider_name = "imbue_cloud_acct"
    agent = _make_workspace_agent(provider_name)
    resolver.update_agents(ParsedAgentsResult(agent_ids=(agent.agent_id,), discovered_agents=(agent,)))

    # No provider error -> the workspace is not stale.
    healthy = _build_workspace_list(resolver)
    assert len(healthy) == 1
    assert "is_stale" not in healthy[0]

    # Its provider's latest poll errored -> the retained workspace is stale.
    errored = ProviderInstanceName(provider_name)
    seed_provider_snapshots(
        resolver,
        providers=(),
        error_by_provider_name={
            errored: DiscoveryError(type_name="RuntimeError", message="boom", provider_name=errored)
        },
        last_snapshot_at=datetime.now(timezone.utc),
    )
    stale = _build_workspace_list(resolver)
    assert len(stale) == 1
    assert stale[0]["is_stale"] == "true"


def test_build_workspace_list_does_not_mark_stale_for_unrelated_provider_error() -> None:
    """An error on a different provider must not flag a healthy provider's workspace stale."""
    resolver = MngrCliBackendResolver()
    agent = _make_workspace_agent("imbue_cloud_acct")
    resolver.update_agents(ParsedAgentsResult(agent_ids=(agent.agent_id,), discovered_agents=(agent,)))

    other = ProviderInstanceName("some_other_provider")
    seed_provider_snapshots(
        resolver,
        providers=(),
        error_by_provider_name={other: DiscoveryError(type_name="RuntimeError", message="boom", provider_name=other)},
        last_snapshot_at=datetime.now(timezone.utc),
    )
    workspaces = _build_workspace_list(resolver)
    assert len(workspaces) == 1
    assert "is_stale" not in workspaces[0]


# -- _build_workspace_list color emission --
#
# These assert the SSE workspaces payload carries the stored color.
# Pre-migration workspaces (no ``color`` label) fall back to
# ``DEFAULT_WORKSPACE_COLOR`` so the rollout doesn't visually break
# existing workspaces. The titlebar derives its contrasting foreground
# from the accent in pure CSS (see .titlebar-surface in app.css), so the
# payload no longer carries an ``accent_fg``.


def test_build_workspace_list_emits_stored_color_when_label_present() -> None:
    resolver = MngrCliBackendResolver()
    agent = _make_workspace_agent("docker", extra_labels={"color": "#0b292b"})
    resolver.update_agents(ParsedAgentsResult(agent_ids=(agent.agent_id,), discovered_agents=(agent,)))

    workspaces = _build_workspace_list(resolver)
    assert len(workspaces) == 1
    assert workspaces[0]["accent"] == "#0b292b"
    assert "accent_fg" not in workspaces[0]


def test_build_workspace_list_falls_back_to_default_color_when_label_missing() -> None:
    """Workspaces without a ``color`` label (created before the picker
    shipped, or backfilled but not yet written through ``mngr label``)
    render as ``DEFAULT_WORKSPACE_COLOR`` -- the same value new
    workspaces get pre-selected in the picker."""
    resolver = MngrCliBackendResolver()
    agent = _make_workspace_agent("imbue_cloud_acct")
    resolver.update_agents(ParsedAgentsResult(agent_ids=(agent.agent_id,), discovered_agents=(agent,)))

    workspaces = _build_workspace_list(resolver)
    assert workspaces[0]["accent"] == DEFAULT_WORKSPACE_COLOR
