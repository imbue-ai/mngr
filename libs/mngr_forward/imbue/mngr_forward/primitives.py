import re
from typing import Final

from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel
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

# Strict subdomain pattern: only ``agent-<hex>.localhost(:port)?`` and the
# ``127.0.0.1`` synonym are accepted by the host-header middleware.
FORWARD_SUBDOMAIN_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(agent-[a-f0-9]+)\.(?:localhost|127\.0\.0\.1)(?::\d+)?$",
    re.IGNORECASE,
)


class ReverseTunnelSpec(FrozenModel):
    """A repeatable ``--reverse <remote-port>:<local-port>`` pair.

    ``remote_port == 0`` means "ask sshd to dynamically assign a remote port";
    the actual bound port is reported back via the ``reverse_tunnel_established``
    envelope event. ``local_port`` must be a real positive integer.
    """

    remote_port: NonNegativeInt = Field(description="Remote bind port; 0 means sshd-assigned")
    local_port: PositiveInt = Field(description="Local target port the tunnel forwards to")
