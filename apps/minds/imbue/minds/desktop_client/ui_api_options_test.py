"""Unit tests for the T3 /ui/api options-data endpoint and its pure helpers."""

import json
from pathlib import Path

from pydantic import Field

from imbue.imbue_common.model_update import to_update
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.ui_api_options import WHOLE_MACHINE_SERVICE
from imbue.minds.desktop_client.ui_api_options import _workspace_host_coordinate_for_options
from imbue.minds.desktop_client.ui_api_options import accepted_service_icon
from imbue.minds.desktop_client.ui_api_options import share_target_labels
from imbue.minds.desktop_client.ui_api_options import split_share_targets
from imbue.minds.desktop_client.workspace_color import DEFAULT_WORKSPACE_COLOR
from imbue.minds.desktop_client.workspace_color import WORKSPACE_PALETTE
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import ProviderInstanceName

_AGENT_ID = "agent-" + "a" * 32
_HOST_ID = "host-" + "f" * 32


class _OptionsSeededResolver(StaticBackendResolver):
    """Static resolver carrying the display info + labels + icons the options endpoint reads."""

    display_info_by_agent_id: dict[str, AgentDisplayInfo] = Field(default_factory=dict, frozen=True)
    color_by_agent_id: dict[str, str] = Field(default_factory=dict, frozen=True)
    labels_by_agent_id: dict[str, dict[str, str]] = Field(default_factory=dict, frozen=True)
    icons_by_agent_id: dict[str, dict[str, str]] = Field(default_factory=dict, frozen=True)
    errored_providers: tuple[str, ...] = Field(default=(), frozen=True)

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        return self.display_info_by_agent_id.get(str(agent_id))

    def get_workspace_color(self, agent_id: AgentId) -> str | None:
        return self.color_by_agent_id.get(str(agent_id))

    def list_service_labels_for_agent(self, agent_id: AgentId) -> dict:
        return dict(self.labels_by_agent_id.get(str(agent_id), {}))

    def list_service_icons_for_agent(self, agent_id: AgentId) -> dict:
        return dict(self.icons_by_agent_id.get(str(agent_id), {}))

    def get_provider_errors(self) -> dict:
        return {ProviderInstanceName(name): object() for name in self.errored_providers}


def _seeded_resolver() -> _OptionsSeededResolver:
    return _OptionsSeededResolver(
        url_by_agent_and_service={
            _AGENT_ID: {
                "system_interface": "http://127.0.0.1:9001",
                "web": "http://127.0.0.1:9002",
                "terminal": "http://127.0.0.1:9003",
                "bad_name": "http://127.0.0.1:9004",
            }
        },
        display_info_by_agent_id={
            _AGENT_ID: AgentDisplayInfo(agent_name="sunny", host_id=_HOST_ID, provider_name="local")
        },
        color_by_agent_id={_AGENT_ID: "#9fbbd3"},
        labels_by_agent_id={_AGENT_ID: {"web": "web-r4nd", "system_interface": "shell-r4nd"}},
    )


def test_options_data_requires_authentication(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=False, backend_resolver=_seeded_resolver()
    )

    response = client.get(f"/ui/api/workspaces/{_AGENT_ID}/options")

    assert response.status_code == 401


def test_options_data_rejects_malformed_agent_ids(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, backend_resolver=_seeded_resolver()
    )

    response = client.get("/ui/api/workspaces/not-an-agent-id/options")

    assert response.status_code == 404


def test_options_data_returns_workspace_context(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, backend_resolver=_seeded_resolver()
    )

    response = client.get(f"/ui/api/workspaces/{_AGENT_ID}/options")

    assert response.status_code == 200
    data = json.loads(response.get_data(as_text=True))
    assert data["agent_id"] == _AGENT_ID
    assert data["host_id"] == _HOST_ID
    assert data["name"] == "sunny"
    assert data["color"] == "#9fbbd3"
    assert data["palette"] == dict(WORKSPACE_PALETTE)
    assert data["is_stale"] is False
    assert data["is_leased_imbue_cloud"] is False
    # No session store in this minimal app: unassociated, no accounts offered.
    assert data["has_account"] is False
    assert data["account_email"] == ""
    assert data["accounts"] == []
    # The share targets exclude the shell, interface services, and non-DNS names.
    assert data["app_services"] == ["web"]
    assert data["service_labels"] == {"web": "web-r4nd", "system_interface": "shell-r4nd"}
    assert data["whole_service"] == "system_interface"


def test_options_data_flags_stale_and_leased_workspaces(tmp_path: Path) -> None:
    resolver = _OptionsSeededResolver(
        url_by_agent_and_service={_AGENT_ID: {}},
        display_info_by_agent_id={
            _AGENT_ID: AgentDisplayInfo(agent_name="leased", host_id=_HOST_ID, provider_name="imbue_cloud_alice")
        },
        errored_providers=("imbue_cloud_alice",),
    )
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, backend_resolver=resolver
    )

    response = client.get(f"/ui/api/workspaces/{_AGENT_ID}/options")

    data = json.loads(response.get_data(as_text=True))
    assert data["is_stale"] is True
    assert data["is_leased_imbue_cloud"] is True
    # No stored color label: the default is reported.
    assert data["color"] == DEFAULT_WORKSPACE_COLOR


def test_split_share_targets_filters_interfaces_and_non_dns_names() -> None:
    # owner-exec is the internal SSH-equivalent exec channel (authorized by
    # request signatures, never a share grant); like the chat/terminal/browser
    # interfaces it must never be offered as a per-app share target.
    app_services, whole = split_share_targets(
        ["system_interface", "web", "Terminal", "chats", "owner-exec", "bad_name", "host-abc", "my-app"]
    )

    assert whole == WHOLE_MACHINE_SERVICE
    assert app_services == ["web", "my-app"]


def test_share_target_labels_cover_targets_and_shell_only() -> None:
    labels = share_target_labels(
        ["web"], {"web": "web-r4nd", "system_interface": "shell-r4nd", "unrendered": "u-r4nd"}
    )

    assert labels == {"web": "web-r4nd", "system_interface": "shell-r4nd"}


def test_options_data_serves_only_gate_passing_app_icons(tmp_path: Path) -> None:
    # The icons are workspace-authored markup headed for the trusted shell's
    # DOM: only well-shaped, non-executable svg reaches the payload, and only
    # for the rendered app targets.
    good_icon = '<svg viewBox="0 0 24 24"><path d="M2 2h20"/></svg>'
    base = _seeded_resolver()
    resolver = base.model_copy_update(
        to_update(
            base.field_ref().icons_by_agent_id,
            {
                _AGENT_ID: {
                    "web": good_icon,
                    "terminal": good_icon,
                    "system_interface": '<svg onload="x()"></svg>',
                }
            },
        )
    )
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, backend_resolver=resolver
    )

    response = client.get(f"/ui/api/workspaces/{_AGENT_ID}/options")

    data = json.loads(response.get_data(as_text=True))
    # terminal is an interface (not an app target) and system_interface's
    # markup fails the gate; only web's icon is served.
    assert data["service_icons"] == {"web": good_icon}


def test_accepted_service_icon_refuses_anything_executable() -> None:
    good = '<svg viewBox="0 0 24 24"><path d="M2 2h20"/></svg>'
    assert accepted_service_icon(good) == good
    assert accepted_service_icon("  " + good + "  ") == good
    assert accepted_service_icon("") == ""
    assert accepted_service_icon("not an svg") == ""
    assert accepted_service_icon('<div><svg viewBox="0 0 24 24"></svg></div>') == ""
    assert accepted_service_icon('<svg onload="x()"></svg>') == ""
    assert accepted_service_icon("<svg><script>1</script></svg>") == ""
    assert accepted_service_icon('<svg><a href="javascript:alert(1)">x</a></svg>') == ""
    assert accepted_service_icon("<svg><style>svg{}</style></svg>") == ""
    assert accepted_service_icon("<svg><foreignObject/></svg>") == ""
    assert accepted_service_icon("<svg>" + "a" * 16400 + "</svg>") == ""


# -- _workspace_host_coordinate_for_options ---------------------------------


def test_workspace_host_coordinate_prefers_discovery() -> None:
    resolver = _OptionsSeededResolver(
        url_by_agent_and_service={},
        display_info_by_agent_id={_AGENT_ID: AgentDisplayInfo(agent_name="sunny", host_id=_HOST_ID)},
    )
    assert _workspace_host_coordinate_for_options(resolver, None, _AGENT_ID) == _HOST_ID


def test_workspace_host_coordinate_empty_for_undiscovered_agent_without_records() -> None:
    resolver = _OptionsSeededResolver(url_by_agent_and_service={})
    assert _workspace_host_coordinate_for_options(resolver, None, _AGENT_ID) == ""


def test_workspace_host_coordinate_ignores_non_host_shaped_coordinates() -> None:
    # A local workspace's coordinate is not a host-<hex> id; the sharing API
    # has nothing to key by, so the pane must get '' rather than 'localhost'.
    resolver = _OptionsSeededResolver(
        url_by_agent_and_service={},
        display_info_by_agent_id={_AGENT_ID: AgentDisplayInfo(agent_name="sunny", host_id="localhost")},
    )
    assert _workspace_host_coordinate_for_options(resolver, None, _AGENT_ID) == ""
