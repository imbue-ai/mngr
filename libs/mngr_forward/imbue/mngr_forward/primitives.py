import re
from typing import Final

from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import InvalidPrimitiveValueError
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.primitives import NonNegativeInt
from imbue.imbue_common.primitives import PositiveInt


class ForwardPort(PositiveInt):
    """A TCP port the plugin binds or proxies to. Must be > 0."""


class OneTimeCode(NonEmptyStr):
    """A single-use authentication code for the bare-origin login URL."""


class CookieSigningKey(SecretStr):
    """Secret key used for signing the plugin's session cookies."""


MNGR_FORWARD_SESSION_COOKIE_NAME: Final[str] = "mngr_forward_session"

MNGR_BINARY: Final[str] = "mngr"

# A single service label usable as a hostname label: lowercase alphanumeric/
# underscore runs joined by single hyphens (so no leading/trailing/consecutive
# hyphens). Underscores are allowed -- ``system_interface`` predates this
# scheme and underscore hostname labels resolve fine in practice (Cloudflare
# DNS and Chromium both accept them). Consecutive hyphens are rejected because
# the shared-workspace hostname scheme (``<name>--<host>--<user>.<domain>``)
# uses ``--`` as its separator, so a registered name containing ``--`` would
# be unparseable there; this mirrors the registration rule in the workspace
# template's ``forward_port.py``. Service names that collide with the agent
# coordinate (``agent-<hex>``) are rejected separately at registration.
_SERVICE_LABEL_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9_]+(?:-[a-z0-9_]+)*$")


class ServiceLabel(NonEmptyStr):
    """A service name usable as a hostname label (lowercase alphanumeric/underscore + single hyphens)."""

    def __new__(cls, value: str) -> "ServiceLabel":
        instance = super().__new__(cls, value)
        if _SERVICE_LABEL_RE.match(str(instance)) is None:
            raise InvalidPrimitiveValueError(
                f"{cls.__name__} must be lowercase alphanumeric/underscore runs joined by single hyphens (got {value!r})"
            )
        return instance


# Host-header pattern for the forwarding middleware. Two coordinates:
#
# - bare workspace origin: ``agent-<hex>.localhost(:port)?`` -> the shell
# - service origin: ``<name>.agent-<hex>.localhost(:port)?`` -> that service
# - deeper labels (``sub.<name>.agent-<hex>.localhost``) route to the same
#   service: the LAST label before ``agent-<hex>`` is the service name; the
#   rest is the service's own sub-origin space (multi-origin apps).
#
# ``127.0.0.1`` stays a synonym for ``localhost``.
FORWARD_SUBDOMAIN_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?:(?P<labels>[a-z0-9_-]+(?:\.[a-z0-9_-]+)*)\.)?(?P<agent>agent-[a-f0-9]+)\.(?P<suffix>localhost|127\.0\.0\.1)(?::\d+)?$",
    re.IGNORECASE,
)


class ParsedForwardHost(FrozenModel):
    """The two coordinates parsed from a forward Host header.

    ``service_name`` is None for the bare workspace origin (the shell);
    otherwise it is the last label before ``agent-<hex>``. Deeper labels are
    accepted and ignored for routing -- they are the service's own sub-origin
    space. ``workspace_domain`` is the ``agent-<hex>.<suffix>`` registrable
    base shared by the shell and every service origin -- the scope of the
    workspace session cookie.
    """

    agent_id_str: NonEmptyStr = Field(description="The agent-<hex> coordinate from the Host header")
    service_labels: str | None = Field(
        default=None,
        description="Full dotted label chain before agent-<hex> (e.g. 'sub.svc'); None for the bare origin",
    )
    workspace_domain: NonEmptyStr = Field(
        description="agent-<hex>.<localhost|127.0.0.1> -- cookie Domain scope for the workspace",
    )

    @property
    def service_name(self) -> str | None:
        """Service label (last label before agent-<hex>); None for the bare origin."""
        if self.service_labels is None:
            return None
        return self.service_labels.rsplit(".", 1)[-1]


def parse_forward_host(host_header: str) -> ParsedForwardHost | None:
    """Parse a Host header into its ``(agent, service)`` coordinates, or None.

    Returns None when the host is not an ``[<labels>.]agent-<hex>.localhost``
    shape at all. Labels are lowercased (DNS names are case-insensitive;
    registration only accepts lowercase labels).
    """
    if not host_header:
        return None
    match = FORWARD_SUBDOMAIN_PATTERN.match(host_header)
    if match is None:
        return None
    labels = match.group("labels")
    agent = match.group("agent")
    suffix = match.group("suffix").lower()
    return ParsedForwardHost(
        agent_id_str=NonEmptyStr(agent),
        service_labels=labels.lower() if labels else None,
        workspace_domain=NonEmptyStr(f"{agent}.{suffix}"),
    )


class ReverseTunnelSpec(FrozenModel):
    """A repeatable ``--reverse <remote-port>:<local-port>`` pair.

    ``remote_port == 0`` means "ask sshd to dynamically assign a remote port";
    the actual bound port is reported back via the ``reverse_tunnel_established``
    envelope event. ``local_port`` must be a real positive integer.
    """

    remote_port: NonNegativeInt = Field(description="Remote bind port; 0 means sshd-assigned")
    local_port: PositiveInt = Field(description="Local target port the tunnel forwards to")
