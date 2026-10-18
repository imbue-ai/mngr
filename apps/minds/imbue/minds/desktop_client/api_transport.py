"""Which way a request reached the desktop client, and what that says about who sent it.

Two transports serve the same Flask app. The loopback TCP listener is what
browsers and the latchkey gateway's ``minds-api-proxy`` reach, and every
request on it has to prove itself with a session cookie or the gateway's
bearer key. The Unix domain socket listener (``api_socket.py``) admits only
connections from processes running as this user, verified by the kernel's
peer credentials before a byte is read, so a request that arrives over it
*is* the user and needs no further credential. The listener says so by
setting :data:`LOCAL_TRUSTED_ENVIRON_KEY` in the WSGI environ, which nothing
reachable from a browser can do.
"""

from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Mapping
from enum import auto
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import SkipValidation

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel

# Set (to True) on the WSGI environ of every request the local-socket listener
# accepted. The key is namespaced so no HTTP header can ever spell it: headers
# land in the environ as ``HTTP_*`` keys.
LOCAL_TRUSTED_ENVIRON_KEY: Final[str] = "imbue_studio.is_local_trusted"

# A self-declared label a same-machine agent may send so its actions can be told
# apart in the logs and the inbox. Never consulted for authorization.
AGENT_LABEL_HEADER: Final[str] = "X-Imbue-Studio-Agent"
_AGENT_LABEL_ENVIRON_KEY: Final[str] = "HTTP_X_IMBUE_STUDIO_AGENT"

WsgiApplication = Callable[[dict[str, object], Callable[..., object]], Iterable[bytes]]


class ApiTransport(UpperCaseStrEnum):
    """How a caller reaches the API, which decides what the API tells it exists."""

    # Through the latchkey gateway's minds-api-proxy, from inside a workspace.
    GATEWAY = auto()
    # Over the same-machine Unix domain socket, as the user.
    LOCAL_SOCKET = auto()


def is_local_trusted_environ(environ: Mapping[str, object]) -> bool:
    return environ.get(LOCAL_TRUSTED_ENVIRON_KEY) is True


def transport_for_environ(environ: Mapping[str, object]) -> ApiTransport:
    if is_local_trusted_environ(environ):
        return ApiTransport.LOCAL_SOCKET
    return ApiTransport.GATEWAY


def agent_label_from_environ(environ: Mapping[str, object]) -> str | None:
    """The caller's self-declared agent label, or None when it sent none."""
    label = environ.get(_AGENT_LABEL_ENVIRON_KEY)
    if not isinstance(label, str) or not label.strip():
        return None
    return label.strip()


class LocalTrustedWsgiApp(FrozenModel):
    """WSGI middleware under which every request counts as the local user.

    Only the local-socket listener serves this wrapper; the TCP listener serves
    the bare app, which is what keeps the marker unreachable from a browser.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    inner: SkipValidation[WsgiApplication] = Field(frozen=True, description="The app serving the marked requests")

    def __call__(self, environ: dict[str, object], start_response: Callable[..., object]) -> Iterable[bytes]:
        environ[LOCAL_TRUSTED_ENVIRON_KEY] = True
        agent_label = agent_label_from_environ(environ)
        logger.debug(
            "Serving a local-socket request {} {} (agent={})",
            environ.get("REQUEST_METHOD"),
            environ.get("PATH_INFO"),
            agent_label if agent_label is not None else "unlabeled",
        )
        return self.inner(environ, start_response)


def mark_requests_local_trusted(wsgi_app: WsgiApplication) -> WsgiApplication:
    return LocalTrustedWsgiApp(inner=wsgi_app)
