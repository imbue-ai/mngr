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

## Basic usage

In your `conftest.py`, register each resource you want to guard with `register_resource_guard()`, then add `pytest_configure`, `pytest_sessionstart`, and `pytest_sessionfinish` hooks as shown below. `register_guarded_resource_markers` registers the pytest marks for all guarded resources in one call.

```python
# conftest.py
from imbue.resource_guards.resource_guards import (
    register_guarded_resource_markers,
    register_resource_guard,
    start_resource_guards,
    stop_resource_guards,
)

register_resource_guard("tmux")
register_resource_guard("rsync")

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

## Fixture-level resource declarations

By default, resource calls during fixture setup/teardown are attributed to whichever test happens to drive that lifecycle. That's fine for function-scoped fixtures but breaks down for module/session-scoped fixtures shared across multiple tests: the fixture's resource calls land in only one test's tracking dir, and siblings end up either failing the superfluous-mark check or having their fixture call blocked.

Opt a fixture into its own guard scope with `@fixture_uses_resources(...)`:

```python
import pytest
from imbue.resource_guards.resource_guards import fixture_uses_resources

@pytest.fixture(scope="module")
@fixture_uses_resources("modal")
def deployed_function():
    # Setup runs under the fixture's own guard scope: modal calls here
    # are authorized against this declaration, not the consuming test's marks.
    deploy_function(...)
    yield url
    # Teardown also runs under the fixture's guard scope.
    stop_function(...)
```

With this in place, `@pytest.mark.modal` on a test is satisfied by *either*:
- the test body directly invoking modal (the original meaning), OR
- the test consuming a `@fixture_uses_resources("modal")` fixture in its closure (the fixture's declaration is independently verified to invoke the resource, so the mark stays meaningful).

But the mark is **required** on every consumer of a tagged fixture, even those that only hit the fixture's output (e.g. HTTPing the URL it yields). This keeps `pytest -m modal` as the canonical "select every test that transitively needs modal" selector — there's no escape hatch where a consuming test silently inherits the dependency without declaring it. A consumer that lacks the matching mark fails with a clear message pointing at the fixture.

The block check (calls without the mark) is unaffected: a test body that directly invokes a resource still needs `@pytest.mark.<resource>` regardless of which fixtures it consumes.

The decorator must go *below* `@pytest.fixture` so it sees the underlying function before pytest captures it. Opt-in: untagged fixtures are unaffected.

## Usage for multi-package projects

When a project is split across multiple packages, listing every guard in every consumer's `conftest.py` becomes a maintenance hazard: each package has to know which guards every other package's tools need, and a forgotten line silently downgrades a guarded mark back to "unknown". Resource guards solve this by letting the package that owns a tool declare its guards through a `resource_guards` entry point group, and letting consumers pick them up automatically with one call.

Each entry point's value is a callable that takes no arguments and registers one or more guards via `register_resource_guard()` and/or `register_sdk_guard()`/`create_sdk_method_guard()`:

```toml
# library's pyproject.toml
[project.entry-points.resource_guards]
my_lib = "imbue.my_lib.register_guards:register_my_guard"
```

```python
# library's register_guards.py
from imbue.resource_guards.resource_guards import register_resource_guard

def register_my_guard():
    register_resource_guard("my_tool")
```

The consumer's `conftest.py` then replaces explicit `register_resource_guard(...)` calls with a single `register_all_resource_guards()`, which imports and invokes every entry point in the group:

```python
# consumer's conftest.py
from imbue.resource_guards.resource_guards import (
    register_all_resource_guards,
    register_guarded_resource_markers,
    start_resource_guards,
    stop_resource_guards,
)

register_all_resource_guards()

def pytest_configure(config):
    register_guarded_resource_markers(config)

def pytest_sessionstart(session):
    start_resource_guards(session)

def pytest_sessionfinish(session, exitstatus):
    stop_resource_guards()
```

The library that owns a tool is the natural place to declare its guard, and consumers don't need to know which guards exist in advance.

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
