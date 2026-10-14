"""The share targets of a workspace and their public origin labels.

A share link is ``https://<label>.<workspace domain>/``: the workspace's frpc
claims each registered service's ``<name>-<rand>`` label on the relay and its
caddy routes only those labels, so a target whose label is not known has no
link that can work -- the bare workspace domain and a bare service name both
fail to route. Every surface that builds a share link (the options payload the
Share tab opens with, the sharing document, the readiness poll) reads the label
map from here so they can never disagree about which targets have a link.

Because the link comes from the label, a target's name is free of hostname
rules: it is an identifier -- the registry key, and the key of its grants --
and what the panel shows a person is the display name beside it.
"""

from collections.abc import Mapping
from collections.abc import Sequence
from typing import Final
from typing import assert_never

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import ServiceShareKind
from imbue.mngr.primitives import AgentId

# The share target that grants the whole machine (the shell service).
WHOLE_MACHINE_SERVICE: Final[str] = "system_interface"

# How a workspace that announces no share kind has its services sorted: the
# interfaces it is built out of are system apps, and its plumbing is internal.
# CLEANUP: remove (with ``_legacy_share_kind``) once no supported workspace
# predates the default-workspace-template release whose shell announces
# ``system`` and ``internal`` on every service registration.
_LEGACY_SYSTEM_SERVICES: Final[frozenset[str]] = frozenset(
    {"chat", "chats", "terminal", "terminals", "browser", "browsers", "getting-started"}
)
_LEGACY_INTERNAL_SERVICES: Final[frozenset[str]] = frozenset({"owner-exec", "vm-exec", "terminal-pty"})


class ShareTargets(FrozenModel):
    """A workspace's share targets, as the Share tab lists them."""

    app_services: tuple[str, ...] = Field(description="Apps offered as share targets of their own")
    system_services: tuple[str, ...] = Field(
        description="System apps: share targets of their own too, but not meant to be shared by default"
    )
    whole_service: str = Field(description="The share target that grants the whole machine")

    @property
    def per_app_services(self) -> tuple[str, ...]:
        return self.app_services + self.system_services


@pure
def _legacy_share_kind(service: str) -> ServiceShareKind:
    lowered = service.lower()
    if lowered in _LEGACY_INTERNAL_SERVICES:
        return ServiceShareKind.INTERNAL
    if lowered in _LEGACY_SYSTEM_SERVICES:
        return ServiceShareKind.SYSTEM
    return ServiceShareKind.APP


@pure
def split_share_targets(servers: Sequence[str], share_kinds: Mapping[str, ServiceShareKind]) -> ShareTargets:
    """Sort a workspace's services into app, system, and whole-machine share targets.

    ``share_kinds`` is what the workspace announced per service; a service it
    announced nothing for (an older workspace) is sorted by a fixed list of
    names instead. An internal service, and a name reserved for a workspace
    coordinate, is no per-app target at all (it stays reachable through a
    whole-machine share). Nothing is excluded for the shape of its name: a
    target's link is built from its origin label, so a name that could never be
    a hostname label still has one. The whole-machine entry is always offered.
    """
    app_services: list[str] = []
    system_services: list[str] = []
    for service in (str(server) for server in servers):
        if service == WHOLE_MACHINE_SERVICE or service.startswith(("host-", "agent-")):
            continue
        share_kind = share_kinds.get(service) or _legacy_share_kind(service)
        match share_kind:
            case ServiceShareKind.APP:
                app_services.append(service)
            case ServiceShareKind.SYSTEM:
                system_services.append(service)
            case ServiceShareKind.INTERNAL:
                pass
            case _ as unreachable:
                assert_never(unreachable)
    return ShareTargets(
        app_services=tuple(app_services),
        system_services=tuple(system_services),
        whole_service=WHOLE_MACHINE_SERVICE,
    )


@pure
def share_target_labels(per_app_services: Sequence[str], service_labels: Mapping[str, str]) -> dict[str, str]:
    """The origin-label map for the rendered share targets (services without a label are omitted)."""
    target_labels = {service: service_labels[service] for service in per_app_services if service in service_labels}
    if WHOLE_MACHINE_SERVICE in service_labels:
        target_labels[WHOLE_MACHINE_SERVICE] = service_labels[WHOLE_MACHINE_SERVICE]
    return target_labels


@pure
def share_target_display_names(
    per_app_services: Sequence[str], service_display_names: Mapping[str, str]
) -> dict[str, str]:
    """What users read for each per-app share target (a service with no display name is omitted).

    The whole-machine target is deliberately absent: the panel names it for what
    it grants, not after the shell app that serves it.
    """
    return {
        service: service_display_names[service] for service in per_app_services if service in service_display_names
    }


def resolve_share_targets(backend_resolver: BackendResolverInterface, agent_id: AgentId) -> ShareTargets:
    """The share targets of ``agent_id``, from the discovered service registrations."""
    services = [str(service) for service in backend_resolver.list_services_for_agent(agent_id)]
    share_kinds = {
        str(service): share_kind
        for service, share_kind in backend_resolver.list_service_share_kinds_for_agent(agent_id).items()
    }
    return split_share_targets(services, share_kinds)


def resolve_share_target_labels(backend_resolver: BackendResolverInterface, agent_id: AgentId) -> dict[str, str]:
    """The current label per share target of ``agent_id``, from the discovered service registrations.

    A target absent from the result has no known label yet (its registration
    has not reached this client, e.g. right after app start) and therefore no
    link that can be shown.
    """
    labels = {
        str(service): label for service, label in backend_resolver.list_service_labels_for_agent(agent_id).items()
    }
    return share_target_labels(resolve_share_targets(backend_resolver, agent_id).per_app_services, labels)


def resolve_share_target_display_names(
    backend_resolver: BackendResolverInterface, agent_id: AgentId
) -> dict[str, str]:
    """The display name per per-app share target of ``agent_id``, from the discovered service registrations.

    A target absent from the result has no display name (it registered without
    a manifest, or the workspace's template predates the field) and is shown by
    its service name.
    """
    display_names = {
        str(service): value
        for service, value in backend_resolver.list_service_display_names_for_agent(agent_id).items()
    }
    return share_target_display_names(
        resolve_share_targets(backend_resolver, agent_id).per_app_services, display_names
    )
