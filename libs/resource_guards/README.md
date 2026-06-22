# resource-guards

Pytest infrastructure for enforcing that tests declare their external resource usage via marks.

Resource guards catch two classes of bugs:

- **Missing marks**: a test calls an external resource (a CLI binary or Python SDK) without the corresponding `@pytest.mark.<resource>`. The guard fails the test with a clear message.
- **Superfluous marks**: a test carries a resource mark but never actually invokes the resource. The guard fails the test so the mark doesn't rot.

These two checks together enforce a single design invariant: **for any (test, guarded resource) pair, there is exactly one correct mark state.** A test that exercises the resource (directly or via a tagged fixture in its closure) must carry the mark; a test that doesn't must not. Allowing a test to pass both with and without the mark would defeat the point of the system, since `pytest -m <resource>` would no longer reliably select every test that needs the resource. Every rule that follows is in service of this invariant.

## How it works

There are two guard mechanisms, covering CLI binaries and Python SDKs respectively.

**Binary guards** create wrapper scripts that shadow the real binary on `PATH`. During a test, the wrapper checks environment variables to decide whether the test is allowed to use the binary. If not, it records a tracking file and exits 127. If yes, it records a tracking file and delegates to the real binary.

**SDK guards** monkeypatch a chokepoint in a Python SDK. The monkeypatched function calls `enforce_sdk_guard()`, which checks the same environment variables and either raises `ResourceGuardViolation` or records a tracking file.

Both mechanisms use per-test tracking files so the `makereport` hook can detect violations even when the test swallows errors or handles non-zero exit codes.

## Basic usage

In your `conftest.py`, register each resource you want to guard with `register_resource_guard()`, then wire up the three session hooks. `register_guarded_resource_markers` registers the pytest marks for all guarded resources in one call.

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

`@fixture_uses_resources(...)` declares which resources a fixture itself uses. This is the fixture-level analogue of a per-test mark, and is independently verified: the fixture must actually invoke each declared resource during setup.

It exists for module/session-scoped fixtures whose consumers reach a resource only through the fixture. Without it, the fixture's setup call is attributed to whichever test happened to trigger setup, so sibling tests carrying the mark fail the superfluous-mark check.

```python
import pytest
from imbue.resource_guards.resource_guards import fixture_uses_resources

@pytest.fixture(scope="module")
@fixture_uses_resources("modal", "docker")
def deployed_function():
    deploy_function(...)
    yield url
    stop_function(...)
```

With this in place, `@pytest.mark.modal` on a test is satisfied by *either* the test body directly invoking modal, or the test consuming a `@fixture_uses_resources("modal")` fixture. The mark is **required** on every consumer of a tagged fixture, even if they don't otherwise use the resource, so that `pytest -m modal` selects every test that transitively needs modal.

Notes:
- The decorator must go *below* `@pytest.fixture`.
- A test body that directly invokes a resource always needs the mark, regardless of which fixtures it consumes.
- Opt-in: untagged fixtures are unaffected.

## Usage for multi-package projects

When a project spans multiple packages, listing every guard in every consumer's `conftest.py` becomes a maintenance hazard. Instead, the package that owns a tool declares its guards through a `resource_guards` entry point, and consumers pick them all up with one call.

Each entry point's value is a no-argument callable that registers one or more guards:

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

## Writing a custom SDK guard

You can guard any Python SDK by registering an install/cleanup pair. The install function patches the SDK's chokepoint -- the single method through which all external calls flow -- so that it calls `enforce_sdk_guard("my_sdk")` before delegating to the original:

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

## Compatibility with pytest-xdist

Binary guards work transparently with xdist: workers inherit the wrapper scripts and `PATH` from the controller. SDK guards are installed independently in each process, since patches are process-local.
