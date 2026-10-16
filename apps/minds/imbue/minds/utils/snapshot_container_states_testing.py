"""The Docker container states a minds-snapshot sandbox was resumed into.

Test-only. The ``exclude`` list in ``apps/minds/pyproject.toml`` names this file
to keep it out of the shipped app, so renaming it puts it back in the wheel.

``scripts/snapshot_minds_e2e_state.py`` ``docker stop``s every workspace
container before capturing the filesystem, so a sandbox booted from that image
finds them ``exited``. Whether it did is a one-shot observation: the first
snapshot-resume test to ``docker start`` the containers destroys it, and offload
runs several batches -- each its own pytest process -- inside one reused sandbox,
so no in-process fixture can carry the reading to the batch that checks it.

These helpers take the reading at the only moment it is defined (just after
dockerd first comes up from the snapshot, before any test body has run) and
publish it to a file every later process in that sandbox reads instead.
"""

import json
import os
import tempfile
from collections.abc import Callable
from collections.abc import Mapping
from enum import auto
from pathlib import Path

from loguru import logger

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.minds.errors import SnapshotContainerStatesError


class DockerContainerState(LowerCaseStrEnum):
    """A container's ``State`` as the Docker daemon reports it.

    The values are Docker's own lowercase wire format -- the same set
    ``docker ps --filter status=`` accepts.
    """

    CREATED = auto()
    RUNNING = auto()
    PAUSED = auto()
    RESTARTING = auto()
    REMOVING = auto()
    EXITED = auto()
    DEAD = auto()


def record_snapshot_container_states(path: Path, read_states: Callable[[], Mapping[str, str]]) -> None:
    """Publish the sandbox's container states at ``path``, unless they are published already.

    ``read_states`` runs only on the call that does the publishing, so a later
    one -- by then reading a daemon whose containers an earlier batch has
    started -- cannot replace the resumed-into reading with its own.
    """
    if path.exists():
        return
    states = read_states()
    file_descriptor, temp_path_str = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    temp_path = Path(temp_path_str)
    try:
        with os.fdopen(file_descriptor, "w") as temp_file:
            json.dump(dict(states), temp_file)
        # Hard-linking the fully written temp file publishes it atomically, so a
        # visible file always holds a complete reading.
        try:
            os.link(temp_path, path)
        except FileExistsError:
            logger.debug("Lost the race to publish the resumed container states to {}", path)
    finally:
        temp_path.unlink(missing_ok=True)


def read_snapshot_container_states(path: Path) -> Mapping[str, DockerContainerState]:
    """Read back the container states published at ``path``, keyed by container name.

    Parses here rather than at publish time, so a state Docker starts reporting
    that ``DockerContainerState`` does not cover fails only the tests that read
    the states -- not every test in the sandbox, via the fixture that publishes.

    Raises ``SnapshotContainerStatesError`` when nothing was published, so a
    sandbox that never took the reading fails its test rather than passing one
    that checked nothing.
    """
    if not path.is_file():
        raise SnapshotContainerStatesError(
            f"No resumed container states were recorded at {path}. They are recorded when dockerd "
            "first comes up in a snapshot-resumed sandbox, so nothing here ever brought the "
            "snapshot's dockerd up."
        )
    try:
        raw_states = path.read_text()
    except OSError as e:
        raise SnapshotContainerStatesError(f"Could not read the resumed container states at {path}: {e}") from e
    try:
        published = json.loads(raw_states)
    except json.JSONDecodeError as e:
        raise SnapshotContainerStatesError(
            f"The resumed container states at {path} are not valid JSON: {raw_states!r}"
        ) from e
    if not isinstance(published, dict) or not all(isinstance(name, str) for name in published):
        raise SnapshotContainerStatesError(
            f"The resumed container states at {path} are not a container-name to state mapping: {published!r}"
        )
    return {name: _parse_container_state(path, name, raw_state) for name, raw_state in published.items()}


def _parse_container_state(path: Path, container_name: str, raw_state: object) -> DockerContainerState:
    """Resolve one recorded state string to a ``DockerContainerState``."""
    try:
        return DockerContainerState(raw_state)
    except ValueError as e:
        raise SnapshotContainerStatesError(
            f"The state {raw_state!r} recorded for container {container_name!r} at {path} is not a Docker "
            f"container state. Docker reports one of {sorted(state.value for state in DockerContainerState)}; "
            "if it has grown a new one, add it to DockerContainerState."
        ) from e
