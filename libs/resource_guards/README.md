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

mngr ships PATH guards for `tmux`, `rsync`, `unison` and the Docker CLI plus an SDK guard for the Docker Python client (`imbue.mngr.register_guards`). `modal_proxy` ships PATH and SDK guards for Modal (`imbue.modal_proxy.register_guards`). `mngr_lima` ships a guard for the `lima` CLI (`imbue.mngr_lima.register_guards`). All of these advertise themselves through the `imbue_resource_guards` entry point group, so any project that uses `imbue.imbue_common.conftest_hooks.register_conftest_hooks` picks them up automatically -- no per-project re-declaration required.

## Setup inside the imbue monorepo

Project conftests do not register guards directly. Instead they call `register_conftest_hooks(globals())`, which discovers every guard published via the `imbue_resource_guards` entry point group and registers it before any pytest hook runs. To add a new guard from a new library:

1. Implement a registration function (e.g. `register_my_guard()` in `imbue/my_lib/register_guards.py`) that calls `register_resource_guard()` for binary guards and/or `create_sdk_method_guard()` / `register_sdk_guard()` for SDK guards.
2. Declare the entry point in your library's `pyproject.toml`:

   ```toml
   [project.entry-points.imbue_resource_guards]
   my_lib = "imbue.my_lib.register_guards:register_my_guard"
   ```

3. Run `uv sync --all-packages` so the editable install picks up the new entry point.

The set of guarded resources is a global property of the monorepo: there is no project-specific list to keep in sync, so a project can never silently lose enforcement of a mark just because its conftest forgot to list it.

## Standalone setup (outside the monorepo)

External users who don't go through `register_conftest_hooks` can still wire guards up directly:

```python
# conftest.py
from imbue.resource_guards.resource_guards import (
    register_all_resource_guards,
    register_guarded_resource_markers,
    register_resource_guard,
    start_resource_guards,
    stop_resource_guards,
)

# Either declare each guard explicitly...
register_resource_guard("tmux")
register_resource_guard("rsync")

# ...or reuse entry-point discovery if you publish to imbue_resource_guards.
register_all_resource_guards()

def pytest_configure(config):
    register_guarded_resource_markers(config)

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
