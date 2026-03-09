# resource-guards-modal

[resource-guards](../resource_guards/README.md) extension that guards Modal gRPC calls.

Monkeypatches `UnaryUnaryWrapper.__call__` and `UnaryStreamWrapper.unary_stream` -- the entry points for all Modal unary and streaming RPC calls.

## Setup

```python
# conftest.py
from imbue.resource_guards.resource_guards import start_resource_guards
from imbue.resource_guards.resource_guards import stop_resource_guards
from imbue.resource_guards_modal.guards import register_modal_guard

register_modal_guard()

def pytest_configure(config):
    config.addinivalue_line("markers", "modal: marks tests that connect to Modal")

def pytest_sessionstart(session):
    start_resource_guards(session)

def pytest_sessionfinish(session, exitstatus):
    stop_resource_guards()
```

Tests that make Modal API calls need `@pytest.mark.modal`. Tests without the mark that trigger a Modal RPC will fail with a `ResourceGuardViolation`.

## Version pinning

Modal is pinned because the guard monkeypatches internal gRPC wrapper methods. New Modal versions may move or rename them.
