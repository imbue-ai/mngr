"""Shared FastAPI dependency helpers for the desktop client."""

from typing import Annotated

from fastapi import Depends
from fastapi import Request

from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface


def _get_backend_resolver(request: Request) -> BackendResolverInterface:
    return request.app.state.backend_resolver


BackendResolverDep = Annotated[BackendResolverInterface, Depends(_get_backend_resolver)]
