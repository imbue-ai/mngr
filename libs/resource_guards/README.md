# resource-guards

Pytest infrastructure for enforcing that tests declare their external resource usage via marks.

Resource guards catch two classes of bugs:

- **Missing marks**: a test calls an external resource without the corresponding `@pytest.mark.<resource>`. The guard fails the test with a clear message.
- **Superfluous marks**: a test carries a resource mark but never actually invokes the resource. The guard fails the test so the mark doesn't rot.

## How it works

There are two guard mechanisms, covering CLI binaries and Python SDKs respectively.

**Binary guards** create wrapper scripts that shadow the real binary on `PATH`. During a test, the wrapper checks environment variables to decide whether the test is allowed to use the binary. If not, it records a tracking file and exits 127. If yes, it records a tracking file and delegates to the real binary.

**SDK guards** monkeypatch a chokepoint in a Python SDK. The monkeypatched function calls `enforce_sdk_guard()`, which checks the same environment variables and either raises `ResourceGuardViolation` or records a tracking file.

Both mechanisms use per-test tracking files so the `makereport` hook can detect violations even when the test swallows errors or handles non-zero exit codes.

## Built-in guards

mng provides Docker guards out of the box (in `imbue.mng.register_guards_docker`), and the mng_modal plugin provides Modal guards (in `imbue.mng_modal.register_guards`). These are registered automatically in each project's conftest.py.

## Setup

In your `conftest.py`, register each resource you want to guard. You need two things per resource: a **marker** (so pytest knows about the mark) and a **guard** (so the enforcement hooks are installed).

```python
# conftest.py
from imbue.resource_guards.resource_guards import (
    register_resource_guard,
    start_resource_guards,
    stop_resource_guards,
)

# Register a binary guard for tmux
register_resource_guard("tmux")

def pytest_configure(config):
    config.addinivalue_line("markers", "tmux: marks tests that use tmux")

def pytest_sessionstart(session):
    start_resource_guards(session)

def pytest_sessionfinish(session, exitstatus):
    stop_resource_guards()
```

Then mark your tests:

```python
import pytest

@pytest.mark.tmux
def test_agent_creates_tmux_session():
    ...
```

## Writing a custom SDK guard

You can guard any Python SDK by registering an install/cleanup pair:

```python
from imbue.resource_guards.resource_guards import enforce_sdk_guard
from imbue.resource_guards.resource_guards import register_sdk_guard

_originals = {}

def _install():
    _originals["send"] = SomeClient.send
    SomeClient.send = _guarded_send

def _cleanup():
    if "send" in _originals:
        SomeClient.send = _originals["send"]
        _originals.clear()

def _guarded_send(self, *args, **kwargs):
    enforce_sdk_guard("my_sdk")
    return _originals["send"](self, *args, **kwargs)

register_sdk_guard("my_sdk", _install, _cleanup)
```

The key requirement is that your monkeypatch calls `enforce_sdk_guard("my_sdk")` at the SDK's chokepoint -- the single method through which all external calls flow.

## Compatibility with pytest-xdist

Binary guards work transparently with xdist. The controller process creates the wrapper scripts and modifies `PATH`; workers inherit both via environment variables. SDK guards are installed independently in each process (controller and workers), since monkeypatches are process-local.
