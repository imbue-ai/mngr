from modal._grpc_client import UnaryStreamWrapper
from modal._grpc_client import UnaryUnaryWrapper

from imbue.resource_guards.resource_guards import MethodKind
from imbue.resource_guards.resource_guards import create_sdk_method_guard


def register_modal_guard() -> None:
    """Register the Modal SDK guard. Safe to call multiple times."""
    create_sdk_method_guard(
        "modal",
        [
            (UnaryUnaryWrapper, "__call__", MethodKind.ASYNC),
            (UnaryStreamWrapper, "unary_stream", MethodKind.ASYNC_GEN),
        ],
    )
