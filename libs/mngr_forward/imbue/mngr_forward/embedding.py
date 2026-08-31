"""Embedding policy for proxied workspace origins.

The fronting proxy owns "who may embed this workspace": every proxied
response gets a ``Content-Security-Policy: frame-ancestors ...`` header
APPENDED (never replacing or modifying anything the service itself sent --
multiple CSP headers compose by intersection, so a service can only ever
tighten the policy, not loosen it).

The default policy is deny-external: the document's own origin (``'self'``),
the workspace's own origin family (the shell embeds its service origins, and
services own arbitrarily deep sub-origin spaces), and nothing else. Hosts
that embed workspaces (the minds chrome) opt in via ``--embedder-origin``.

Enforcement of this header is load-bearing for the embed contract's trust
model (see the minds embed contract): because a
disallowed embedder can never load a workspace frame at all, the workspace
side needs no allowlist of its own -- being framed proves the embedder was
allowed.
"""

import re
from typing import Final

from imbue.imbue_common.primitives import InvalidPrimitiveValueError
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.pure import pure
from imbue.mngr_forward.primitives import ParsedForwardHost

# A CSP host-source usable as an embedder origin: scheme://host[:port], no
# path, no wildcards (the embedder is a concrete origin the spawner knows).
_EMBEDDER_ORIGIN_RE: Final[re.Pattern[str]] = re.compile(r"^https?://[a-z0-9.-]+(?::\d+)?$")


class EmbedderOrigin(NonEmptyStr):
    """A concrete origin allowed to embed workspace content (``scheme://host[:port]``)."""

    def __new__(cls, value: str) -> "EmbedderOrigin":
        instance = super().__new__(cls, value)
        if _EMBEDDER_ORIGIN_RE.match(str(instance)) is None:
            raise InvalidPrimitiveValueError(
                f"{cls.__name__} must look like http(s)://host[:port] with no path (got {value!r})"
            )
        return instance


@pure
def build_frame_ancestors_policy(
    host_info: ParsedForwardHost,
    listen_port: int,
    use_http2: bool,
    embedder_origins: tuple[EmbedderOrigin, ...],
) -> str:
    """Build the ``frame-ancestors`` CSP value for one workspace origin's responses.

    Allows: the document's own origin, the workspace's whole origin family
    (bare domain + every service/sub-origin label depth, on the proxy's own
    scheme and port), and each configured embedder origin. ``*.<domain>``
    matches any label depth per the CSP host-source grammar.
    """
    scheme = "https" if use_http2 else "http"
    family_base = f"{scheme}://{host_info.workspace_domain}:{listen_port}"
    family_wildcard = f"{scheme}://*.{host_info.workspace_domain}:{listen_port}"
    sources = ["'self'", family_base, family_wildcard, *[str(origin) for origin in embedder_origins]]
    return "frame-ancestors " + " ".join(sources)
