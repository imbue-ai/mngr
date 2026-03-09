# resource-guards-docker

[resource-guards](../resource_guards/README.md) extension that guards Docker CLI and SDK usage.

Two guards are provided:

- **`register_docker_cli_guard()`** -- binary guard that intercepts `docker` CLI subprocess calls via a PATH wrapper script. Enforces `@pytest.mark.docker`.
- **`register_docker_sdk_guard()`** -- SDK guard that monkeypatches `APIClient.send` to intercept in-process Docker HTTP calls. Enforces `@pytest.mark.docker_sdk`.

## Setup

```python
# conftest.py
from imbue.resource_guards.resource_guards import start_resource_guards
from imbue.resource_guards.resource_guards import stop_resource_guards
from imbue.resource_guards_docker.guards import register_docker_cli_guard
from imbue.resource_guards_docker.guards import register_docker_sdk_guard

register_docker_cli_guard()
register_docker_sdk_guard()

def pytest_configure(config):
    config.addinivalue_line("markers", "docker: marks tests that invoke the docker CLI")
    config.addinivalue_line("markers", "docker_sdk: marks tests that use the Docker Python SDK")

def pytest_sessionstart(session):
    start_resource_guards(session)

def pytest_sessionfinish(session, exitstatus):
    stop_resource_guards()
```

## Version pinning

Docker is pinned because the SDK guard monkeypatches `APIClient.send`. New Docker SDK versions may change the class hierarchy.
