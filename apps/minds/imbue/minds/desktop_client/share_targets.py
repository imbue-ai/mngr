"""The share targets of a workspace and their public origin labels.

A share link is ``https://<label>.<workspace domain>/``: the workspace's frpc
claims each registered service's ``<name>-<rand>`` label on the relay and its
caddy routes only those labels, so a target whose label is not known has no
link that can work -- the bare workspace domain and a bare service name both
fail to route. Every surface that builds a share link (the options payload the
Share tab opens with, the sharing document, the readiness poll) reads the label
map from here so they can never disagree about which targets have a link.
"""

import re
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.mngr.primitives import AgentId

# The share target that grants the whole machine (the shell service).
WHOLE_MACHINE_SERVICE: Final[str] = "system_interface"

# Interfaces the workspace is built out of (or internal infrastructure) rather
# than apps built on top of it: excluded from the per-app share targets (the
# whole machine remains the deliberate way to grant everything). ``owner-exec``
# is the internal SSH-equivalent exec channel (authorized by request signatures
# against authorized_keys, never a share grant), so it must never be offered as
# a per-app share target.
_NON_APP_SHARE_SERVICES: Final[frozenset[str]] = frozenset(
    {"chat", "chats", "terminal", "terminals", "browser", "browsers", "owner-exec"}
)

# A per-app share link is a real origin, so only DNS-label-safe names qualify.
_DNS_SAFE_SERVICE_NAME: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@pure
def split_share_targets(servers: Sequence[str]) -> tuple[list[str], str]:
    """Split a workspace's services into per-app share targets and the whole-machine one.

    The whole-machine entry is always offered; interface services and names
    that cannot be a hostname label are excluded from the per-app list (they
    stay reachable through a whole-machine share).
    """
    app_services = [
        str(service)
        for service in servers
        if str(service) != WHOLE_MACHINE_SERVICE
        and str(service).lower() not in _NON_APP_SHARE_SERVICES
        and _DNS_SAFE_SERVICE_NAME.match(str(service)) is not None
        and not str(service).startswith(("host-", "agent-"))
    ]
    return app_services, WHOLE_MACHINE_SERVICE


@pure
def share_target_labels(app_services: Sequence[str], service_labels: Mapping[str, str]) -> dict[str, str]:
    """The origin-label map for the rendered share targets (services without a label are omitted)."""
    target_labels = {service: service_labels[service] for service in app_services if service in service_labels}
    if WHOLE_MACHINE_SERVICE in service_labels:
        target_labels[WHOLE_MACHINE_SERVICE] = service_labels[WHOLE_MACHINE_SERVICE]
    return target_labels


def resolve_share_target_labels(backend_resolver: BackendResolverInterface, agent_id: AgentId) -> dict[str, str]:
    """The current label per share target of ``agent_id``, from the discovered service registrations.

    A target absent from the result has no known label yet (its registration
    has not reached this client, e.g. right after app start) and therefore no
    link that can be shown.
    """
    services = [str(service) for service in backend_resolver.list_services_for_agent(agent_id)]
    labels = {
        str(service): label for service, label in backend_resolver.list_service_labels_for_agent(agent_id).items()
    }
    app_services, _whole_service = split_share_targets(services)
    return share_target_labels(app_services, labels)
