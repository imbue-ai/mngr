"""Modal app exposing Cloudflare tunnel management via FastAPI endpoints."""

import functools
import os
from typing import Annotated

import modal
from fastapi import Depends
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from imbue.cloudflare_forwarding.auth import verify_credentials
from imbue.cloudflare_forwarding.cloudflare_client import create_cloudflare_client
from imbue.cloudflare_forwarding.data_types import AddServiceRequest
from imbue.cloudflare_forwarding.data_types import CreateTunnelRequest
from imbue.cloudflare_forwarding.errors import CloudflareApiError
from imbue.cloudflare_forwarding.errors import ServiceNotFoundError
from imbue.cloudflare_forwarding.errors import TunnelNotFoundError
from imbue.cloudflare_forwarding.errors import TunnelOwnershipError
from imbue.cloudflare_forwarding.forwarding_service import ForwardingService
from imbue.cloudflare_forwarding.primitives import CloudflareAccountId
from imbue.cloudflare_forwarding.primitives import CloudflareZoneId
from imbue.cloudflare_forwarding.primitives import DomainName
from imbue.cloudflare_forwarding.primitives import ServiceName
from imbue.cloudflare_forwarding.primitives import TunnelName
from imbue.cloudflare_forwarding.primitives import Username

image = modal.Image.debian_slim().pip_install("fastapi[standard]", "httpx")
modal_app = modal.App(name="cloudflare-forwarding", image=image)

web_app = FastAPI()


@functools.cache
def _get_forwarding_service() -> ForwardingService:
    """Return a cached ForwardingService, creating it on first call."""
    client = create_cloudflare_client(
        api_token=os.environ["CLOUDFLARE_API_TOKEN"],
        account_id=CloudflareAccountId(os.environ["CLOUDFLARE_ACCOUNT_ID"]),
        zone_id=CloudflareZoneId(os.environ["CLOUDFLARE_ZONE_ID"]),
    )
    domain = DomainName(os.environ["CLOUDFLARE_DOMAIN"])
    return ForwardingService(client=client, domain=domain)


@web_app.exception_handler(CloudflareApiError)
def _handle_cloudflare_error(_request: object, exc: CloudflareApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"errors": exc.cf_errors},
    )


@web_app.exception_handler(TunnelNotFoundError)
def _handle_tunnel_not_found(_request: object, exc: TunnelNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@web_app.exception_handler(TunnelOwnershipError)
def _handle_ownership_error(_request: object, exc: TunnelOwnershipError) -> JSONResponse:
    return JSONResponse(status_code=403, content={"detail": str(exc)})


@web_app.exception_handler(ServiceNotFoundError)
def _handle_service_not_found(_request: object, exc: ServiceNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@web_app.post("/tunnels")
def create_tunnel(
    body: CreateTunnelRequest,
    username: Annotated[Username, Depends(verify_credentials)],
) -> dict[str, object]:
    """Create a tunnel (idempotent) and return its info with token."""
    service = _get_forwarding_service()
    info = service.create_tunnel(username=username, agent_id=body.agent_id)
    return info.model_dump()


@web_app.get("/tunnels")
def list_tunnels(
    username: Annotated[Username, Depends(verify_credentials)],
) -> list[dict[str, object]]:
    """List all tunnels belonging to the authenticated user."""
    service = _get_forwarding_service()
    tunnels = service.list_tunnels(username=username)
    return [t.model_dump() for t in tunnels]


@web_app.delete("/tunnels/{tunnel_name}")
def delete_tunnel(
    tunnel_name: str,
    username: Annotated[Username, Depends(verify_credentials)],
) -> dict[str, str]:
    """Delete a tunnel and all its associated DNS records and ingress rules."""
    service = _get_forwarding_service()
    service.delete_tunnel(tunnel_name=TunnelName(tunnel_name), username=username)
    return {"status": "deleted"}


@web_app.post("/tunnels/{tunnel_name}/services")
def add_service(
    tunnel_name: str,
    body: AddServiceRequest,
    username: Annotated[Username, Depends(verify_credentials)],
) -> dict[str, object]:
    """Add a service to a tunnel."""
    service = _get_forwarding_service()
    info = service.add_service(
        tunnel_name=TunnelName(tunnel_name),
        username=username,
        service_name=body.service_name,
        service_url=body.service_url,
    )
    return info.model_dump()


@web_app.delete("/tunnels/{tunnel_name}/services/{service_name}")
def remove_service(
    tunnel_name: str,
    service_name: str,
    username: Annotated[Username, Depends(verify_credentials)],
) -> dict[str, str]:
    """Remove a service from a tunnel."""
    svc = _get_forwarding_service()
    svc.remove_service(
        tunnel_name=TunnelName(tunnel_name),
        username=username,
        service_name=ServiceName(service_name),
    )
    return {"status": "deleted"}


@modal_app.function(
    secrets=[modal.Secret.from_name("cloudflare-forwarding-secrets")],
)
@modal.asgi_app()
def fastapi_app() -> FastAPI:
    """Serve the FastAPI app as a Modal ASGI app."""
    return web_app
