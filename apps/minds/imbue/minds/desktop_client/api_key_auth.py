"""Per-agent API key authentication for ``/api/v1/...`` endpoints.

Exposes a FastAPI dependency that maps a ``Bearer <api_key>``
``Authorization`` header onto the calling agent's :class:`AgentId`,
backed by the SHA-256 hash files in ``<data_dir>/agents/<id>/api_key_hash``
(see :mod:`imbue.minds.desktop_client.api_key_store`).

Kept as its own module so multiple ``/api/v1`` route modules
(``api_v1.py``, ``file_server.py``, ...) can share the same
``CallerAgentIdDep`` without forming an import cycle.
"""

from typing import Annotated

from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.api_key_store import find_agent_by_api_key
from imbue.mngr.primitives import AgentId


def _authenticate_api_key(request: Request) -> AgentId:
    """Map the request's ``Authorization: Bearer ...`` header to an AgentId.

    Raises ``HTTPException(401)`` if the header is missing, malformed,
    empty, or does not correspond to any stored API key hash.
    """
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")

    token = auth_header[len("Bearer ") :]
    if not token:
        raise HTTPException(status_code=401, detail="Empty Bearer token")

    paths: WorkspacePaths = request.app.state.api_v1_paths
    agent_id = find_agent_by_api_key(paths.data_dir, token)
    if agent_id is None:
        raise HTTPException(status_code=401, detail="Invalid API key")

    return agent_id


CallerAgentIdDep = Annotated[AgentId, Depends(_authenticate_api_key)]
