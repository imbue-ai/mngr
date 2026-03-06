from typing import Any

from docker.api.client import APIClient
from modal._grpc_client import UnaryStreamWrapper
from modal._grpc_client import UnaryUnaryWrapper

from imbue.imbue_common.conftest_hooks import register_marker
from imbue.imbue_common.resource_guards import enforce_sdk_guard
from imbue.imbue_common.resource_guards import register_resource_guard
from imbue.imbue_common.resource_guards import register_sdk_guard

# Stores original methods so they can be restored by cleanup.
# Typed as Any because the values are method references with heterogeneous signatures.
_modal_originals: dict[str, Any] = {}
_docker_originals: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Module-level guard functions (looked up from originals dicts at call time)
# ---------------------------------------------------------------------------


# async is required here because the original methods are async
async def _guarded_modal_unary_call(self, *args, **kwargs):
    enforce_sdk_guard("modal")
    original = _modal_originals["unary_call"]
    return await original(self, *args, **kwargs)


async def _guarded_modal_unary_stream(self, *args, **kwargs):
    enforce_sdk_guard("modal")
    original = _modal_originals["unary_stream"]
    async for response in original(self, *args, **kwargs):
        yield response


def _guarded_docker_send(self, *args, **kwargs):
    enforce_sdk_guard("docker_sdk")
    original = _docker_originals["send_original_resolved"]
    return original(self, *args, **kwargs)


# ---------------------------------------------------------------------------
# Install / cleanup
# ---------------------------------------------------------------------------


def _install_modal_guards() -> None:
    """Monkeypatch Modal's gRPC wrapper classes to enforce resource guards.

    Patches UnaryUnaryWrapper.__call__ and UnaryStreamWrapper.unary_stream,
    which are the entry points for all Modal unary and streaming RPC calls.
    """
    _modal_originals["unary_call"] = UnaryUnaryWrapper.__call__
    _modal_originals["unary_stream"] = UnaryStreamWrapper.unary_stream

    UnaryUnaryWrapper.__call__ = _guarded_modal_unary_call  # ty: ignore[invalid-assignment]
    UnaryStreamWrapper.unary_stream = _guarded_modal_unary_stream  # ty: ignore[invalid-assignment]


def _cleanup_modal_guards() -> None:
    if "unary_call" not in _modal_originals:
        return

    UnaryUnaryWrapper.__call__ = _modal_originals["unary_call"]
    UnaryStreamWrapper.unary_stream = _modal_originals["unary_stream"]
    _modal_originals.clear()


def _install_docker_sdk_guards() -> None:
    """Monkeypatch Docker's APIClient.send to enforce resource guards.

    APIClient inherits send from requests.Session. We shadow it on APIClient
    so that all Docker HTTP requests are guarded without affecting other
    requests.Session usage.
    """
    _docker_originals["send_original_resolved"] = APIClient.send
    _docker_originals["send_existed"] = "send" in APIClient.__dict__
    if "send" in APIClient.__dict__:
        _docker_originals["send_original"] = APIClient.__dict__["send"]

    APIClient.send = _guarded_docker_send  # ty: ignore[invalid-assignment]


def _cleanup_docker_sdk_guards() -> None:
    if "send_existed" not in _docker_originals:
        return

    if _docker_originals["send_existed"]:
        APIClient.send = _docker_originals["send_original"]
    elif "send" in APIClient.__dict__:
        # No send was defined directly on APIClient before we patched it;
        # remove our shadow so MRO resolution goes back to the parent class.
        del APIClient.send
    else:
        # Our shadow was already removed (e.g. by another cleanup path).
        pass
    _docker_originals.clear()


# ---------------------------------------------------------------------------
# Registration (called from each project's conftest.py)
# ---------------------------------------------------------------------------


def register_modal_guard() -> None:
    """Register the Modal SDK guard and marker. Safe to call multiple times."""
    register_marker("modal: marks tests that connect to the Modal cloud service")
    register_sdk_guard("modal", _install_modal_guards, _cleanup_modal_guards)


def register_docker_cli_guard() -> None:
    """Register the Docker CLI binary guard and marker. Safe to call multiple times.

    Uses a PATH wrapper to intercept docker CLI subprocess calls, including
    from child processes launched by mng create.
    """
    register_marker("docker: marks tests that invoke the docker CLI via subprocess")
    register_resource_guard("docker")


def register_docker_sdk_guard() -> None:
    """Register the Docker SDK guard and marker. Safe to call multiple times.

    Monkeypatches APIClient.send to intercept in-process Docker SDK HTTP calls.
    """
    register_marker("docker_sdk: marks tests that use the Docker Python SDK in-process")
    register_sdk_guard("docker_sdk", _install_docker_sdk_guards, _cleanup_docker_sdk_guards)
