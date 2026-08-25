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

# Identity headers stamped onto every forwarded request so a workspace service
# sees the same contract locally as it does over the share relay: X-Share-Owner
# is always present, and X-Share-Email is present only for a non-owner. The
# local proxy serves the single authenticated user, who is always the owner, so
# it sets X-Share-Owner=true and never an email. Both are set with replace
# semantics (any inbound copy is dropped first), so an agent-controlled backend
# page can never forge them. This mirrors the share_gateway header contract in
# the default-workspace-template.
SHARE_OWNER_HEADER: Final[str] = "X-Share-Owner"
SHARE_EMAIL_HEADER: Final[str] = "X-Share-Email"

# The bare-origin browser auth bridge: a host application that spawned the
# plugin with ``--browser-bridge-token`` 302s a browser here with the
# spawn-time opaque token so the browser gets the bare-origin session cookie
# without consuming an OTP. Exposed here (not in server.py) because host
# applications (minds) build the redirect URL without importing the server.
BROWSER_BRIDGE_PATH: Final[str] = "/_bridge"

MNGR_BINARY: Final[str] = "mngr"

# A single service name usable as a hostname label: lowercase alphanumeric /
# underscore runs joined by single hyphens (so no leading/trailing/consecutive
# hyphens, no dots). Underscores are tolerated because ``system_interface``
# predates the hostname scheme; the workspace template's registration script
# enforces the same rule for new services.
_SERVICE_LABEL_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9_]+(?:-[a-z0-9_]+)*$")


class ServiceLabel(NonEmptyStr):
    """A service name usable as a hostname label (lowercase alphanumeric/underscore + single hyphens)."""

    def __new__(cls, value: str) -> "ServiceLabel":
        instance = super().__new__(cls, value)
        if _SERVICE_LABEL_RE.match(str(instance)) is None:
            raise InvalidPrimitiveValueError(
                f"{cls.__name__} must be lowercase alphanumeric/underscore runs joined by single hyphens "
                f"(got {value!r})"
            )
        return instance


# Host-header pattern for the forwarding middleware. Coordinates:
#
# - bare agent origin: ``agent-<32hex>.localhost(:port)?`` -> the shell
# - service origin: ``<name>.agent-<32hex>.localhost(:port)?`` -> that service
# - deeper labels (``sub.<name>.agent-<32hex>.localhost``) route to the same
#   service: the LAST label before the agent id is the service name; the rest
#   is the service's own sub-origin space (multi-origin apps).
#
# The agent id is the canonical origin coordinate: an origin belongs to the
# agent whose services it serves, and it survives the agent moving to a
# different host. Legacy ``host-<32hex>`` coordinates (URLs minted before the
# agent keying) still parse; HTML navigations to them are redirected to the
# canonical agent origin (see the server's legacy-coordinate handling).
#
# ``127.0.0.1`` stays a synonym for ``localhost``. The id requires the full
# 32 hex characters so malformed hosts fall through to the bare-origin
# routes instead of being half-parsed.
FORWARD_SUBDOMAIN_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?:(?P<labels>[a-z0-9_-]+(?:\.[a-z0-9_-]+)*)\.)?(?P<coordinate>(?:host|agent)-[a-f0-9]{32})\.(?P<suffix>localhost|127\.0\.0\.1)(?::\d+)?$",
    re.IGNORECASE,
)


class ParsedForwardHost(FrozenModel):
    """The coordinates parsed from a forward Host header.

    ``service_labels`` is None for the bare workspace origin (the shell);
    otherwise it is the full dotted label chain before the coordinate, whose
    last label selects the service (deeper labels are that service's own
    sub-origin space). ``workspace_domain`` is the ``<coordinate>.<suffix>``
    base shared by the shell and every service origin -- the scope of the
    workspace session cookie.
    """

    coordinate: NonEmptyStr = Field(
        description="The origin coordinate from the Host header: an agent id, or a legacy host id"
    )
    service_labels: str | None = Field(
        default=None,
        description="Full dotted label chain before the coordinate (e.g. 'sub.svc'); None for the bare origin",
    )
    workspace_domain: NonEmptyStr = Field(
        description="<coordinate>.<localhost|127.0.0.1> -- cookie Domain scope for the workspace",
    )

    @property
    def is_legacy_host_coordinate(self) -> bool:
        """Whether the origin used a pre-agent-keying ``host-<hex>`` coordinate."""
        return str(self.coordinate).startswith("host-")

    @property
    def service_name(self) -> str | None:
        """Service label (last label before the coordinate); None for the bare origin."""
        if self.service_labels is None:
            return None
        return self.service_labels.rsplit(".", 1)[-1]


def parse_forward_host(host_header: str) -> ParsedForwardHost | None:
    """Parse a Host header into its workspace/service coordinates, or None.

    Returns None when the host is not an ``[<labels>.]<coordinate>.localhost``
    shape at all (the coordinate being an agent id or a legacy host id).
    Labels are lowercased (DNS names are case-insensitive; registration only
    accepts lowercase labels).
    """
    if not host_header:
        return None
    match = FORWARD_SUBDOMAIN_PATTERN.match(host_header)
    if match is None:
        return None
    labels = match.group("labels")
    coordinate = match.group("coordinate").lower()
    suffix = match.group("suffix").lower()
    return ParsedForwardHost(
        coordinate=NonEmptyStr(coordinate),
        service_labels=labels.lower() if labels else None,
        workspace_domain=NonEmptyStr(f"{coordinate}.{suffix}"),
    )


class ReverseTunnelSpec(FrozenModel):
    """A repeatable ``--reverse <remote-port>:<local-port>`` pair.

    ``remote_port == 0`` means "ask sshd to dynamically assign a remote port";
    the actual bound port is reported back via the ``reverse_tunnel_established``
    envelope event. ``local_port`` must be a real positive integer.
    """

    remote_port: NonNegativeInt = Field(description="Remote bind port; 0 means sshd-assigned")
    local_port: PositiveInt = Field(description="Local target port the tunnel forwards to")
