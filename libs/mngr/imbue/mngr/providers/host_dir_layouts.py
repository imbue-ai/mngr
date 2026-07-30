"""The in-host ``host_dir`` layouts mngr has shipped, for reading across generations.

A host is baked with one layout and keeps it for life, while the provider config
resolved in the current context is free to name the other: an account-wide
provider block moved generations under the user, and a project
``.mngr/settings.toml`` applies from the workspace clone but not from ``$HOME``.
Addressing a host at the wrong layout reads no certified data (the host sits
UNKNOWN) and resolves agent state dirs to nothing, so ``mngr exec`` and
``mngr start`` fail with "Agent not found on host" against a healthy container.

Only the container/VM layouts mngr bakes belong here. A provider whose host_dir
is derived rather than chosen (the bare VPS realizer's ``/mngr/hosts/<name>``)
or that runs on a machine mngr does not own (ssh, local) must not probe these:
a stray ``/mngr`` there belongs to something else.
"""

from pathlib import Path
from typing import Final

# Newest first. This is the whole of the layout knowledge -- a future move adds
# an entry here and every candidate list picks it up.
KNOWN_WORKSPACE_HOST_DIRS: Final[tuple[str, ...]] = ("/home/user/.mngr", "/mngr")


def host_dir_fallbacks(configured_host_dir: str | Path) -> tuple[str, ...]:
    """Return the other known layouts to probe when ``configured_host_dir`` holds no data.

    Deliberately bidirectional: the caller's configured value always leads its
    own candidate list, and everything else follows, so a client on either
    generation reads hosts from both. Excludes the configured value itself so no
    candidate is probed twice.
    """
    configured = str(configured_host_dir)
    return tuple(candidate for candidate in KNOWN_WORKSPACE_HOST_DIRS if candidate != configured)
