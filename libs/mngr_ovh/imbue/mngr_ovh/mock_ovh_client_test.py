"""Shared fake OVH client for unit tests.

The real ``ovh.Client`` cannot be constructed without a valid endpoint +
credentials, and ``OvhVpsClient.ovh_client`` is validated with pydantic's
``arbitrary_types_allowed`` (an ``isinstance(ovh.Client)`` check). A
``MagicMock(spec=ovh.Client)`` satisfies that check while letting each test
script the transport via a router callable with the python-ovh
``call(method, path, body, need_auth)`` signature.

Centralized here so that signature -- and the boilerplate of wiring it into
an ``OvhVpsClient`` -- lives in exactly one place instead of being
re-hand-rolled in every ``*_test.py``. Tests import
:func:`make_fake_ovh_vps_client` directly (this is not a fixture).
"""

from typing import Any
from unittest.mock import MagicMock

import ovh

from imbue.mngr_ovh.client import OvhVpsClient


def make_fake_ovh_vps_client(
    call_router: Any,
    *,
    is_unconfigured: bool = False,
    task_poll_interval: float = 0.0,
    set_renew_retry_poll_interval_seconds: float = 0.0,
    set_renew_retry_timeout_seconds: float | None = None,
) -> OvhVpsClient:
    """Build an ``OvhVpsClient`` whose transport is driven by ``call_router``.

    ``call_router`` is the ``MagicMock.side_effect`` for the fake transport:
    usually a callable invoked as ``call_router(method, path, body, need_auth)``
    whose return value is what the SDK would have returned, but (per
    ``MagicMock`` semantics) it may also be an exception instance/class to
    raise on every call, or an iterable of per-call results. Typed ``Any``
    for that reason.

    The retry/poll intervals default to ``0.0`` so retry-path tests run in
    well under a second without sleeping (the production client uses
    dependency-injected intervals rather than patched module constants).
    Pass ``set_renew_retry_timeout_seconds`` to shrink the
    ``set_renew_at_expiration`` retry budget for budget-exhaustion tests.
    """
    raw_client = MagicMock(spec=ovh.Client)
    raw_client.call = MagicMock(side_effect=call_router)
    kwargs: dict[str, Any] = {
        "ovh_client": raw_client,
        "subsidiary": "US",
        "task_poll_interval": task_poll_interval,
        "set_renew_retry_poll_interval_seconds": set_renew_retry_poll_interval_seconds,
        "is_unconfigured": is_unconfigured,
    }
    if set_renew_retry_timeout_seconds is not None:
        kwargs["set_renew_retry_timeout_seconds"] = set_renew_retry_timeout_seconds
    return OvhVpsClient(**kwargs)
