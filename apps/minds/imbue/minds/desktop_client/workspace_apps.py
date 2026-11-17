"""The apps of an owned workspace as the home page lists them (specs/home-screen-redesign, 4.10).

The service registry already carries every app's name, display name, origin
label, and icon markup; this module reads the app-kind services out of it
(system and internal services are part of the workspace, not apps built on
it) and stamps each icon with a short digest, so the channel entry says when
an icon changed without carrying the markup itself.
"""

from collections.abc import Mapping

from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.share_targets import resolve_share_targets
from imbue.minds.desktop_client.ui_api_options import accepted_service_icon
from imbue.minds.desktop_client.ui_models import UiWorkspaceApp
from imbue.minds.desktop_client.wallpaper_cache import content_version
from imbue.mngr.primitives import AgentId


@pure
def icon_version_of(raw_icon: str) -> str:
    """The version of an app's icon as served, or '' when the markup is not acceptable."""
    accepted = accepted_service_icon(raw_icon)
    return content_version(accepted.encode("utf-8")) if accepted else ""


@pure
def build_workspace_apps(
    app_services: tuple[str, ...],
    display_names: Mapping[str, str],
    labels: Mapping[str, str],
    icons: Mapping[str, str],
) -> tuple[UiWorkspaceApp, ...]:
    return tuple(
        UiWorkspaceApp(
            name=service,
            display_name=display_names.get(service) or service,
            label=labels.get(service, ""),
            icon_version=icon_version_of(icons.get(service, "")),
        )
        for service in app_services
    )


def resolve_workspace_apps(
    backend_resolver: BackendResolverInterface, agent_id: AgentId
) -> tuple[UiWorkspaceApp, ...]:
    """The app-kind services of ``agent_id`` from the discovered registrations, in the registry's order."""
    targets = resolve_share_targets(backend_resolver, agent_id)
    display_names = {
        str(service): value
        for service, value in backend_resolver.list_service_display_names_for_agent(agent_id).items()
    }
    labels = {
        str(service): label for service, label in backend_resolver.list_service_labels_for_agent(agent_id).items()
    }
    icons = {str(service): icon for service, icon in backend_resolver.list_service_icons_for_agent(agent_id).items()}
    return build_workspace_apps(targets.app_services, display_names, labels, icons)


def accepted_workspace_app_icon(backend_resolver: BackendResolverInterface, agent_id: AgentId, app_name: str) -> str:
    """The served icon markup of one app of ``agent_id``, or '' when the app is unknown or its icon unusable."""
    if app_name not in resolve_share_targets(backend_resolver, agent_id).app_services:
        return ""
    icons = backend_resolver.list_service_icons_for_agent(agent_id)
    for service, icon in icons.items():
        if str(service) == app_name:
            return accepted_service_icon(icon)
    return ""
