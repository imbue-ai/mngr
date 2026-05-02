from modal._grpc_client import UnaryStreamWrapper
from modal._grpc_client import UnaryUnaryWrapper

from imbue.resource_guards.resource_guards import MethodKind
from imbue.resource_guards.resource_guards import create_sdk_method_guard
from imbue.resource_guards.resource_guards import register_resource_guard


def register_modal_guard() -> None:
    """Register the Modal CLI binary guard and SDK monkeypatch. Safe to call multiple times.

    Modal calls reach the network via two paths: subprocess invocations of the
    `modal` CLI (caught by the PATH wrapper) and in-process gRPC traffic from
    the Python SDK (caught by the monkeypatch). Both must be registered for
    @pytest.mark.modal to fully cover Modal usage.
    """
    register_resource_guard("modal")
    create_sdk_method_guard(
        "modal",
        [
            (UnaryUnaryWrapper, "__call__", MethodKind.ASYNC),
            (UnaryStreamWrapper, "unary_stream", MethodKind.ASYNC_GEN),
        ],
    )
