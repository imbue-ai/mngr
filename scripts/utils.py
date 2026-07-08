import re
import tomllib
from collections import deque
from collections.abc import Iterator
from functools import cached_property
from pathlib import Path
from typing import Final

from pydantic import Field
from pydantic import computed_field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.plugin_catalog import UNPUBLISHED_PACKAGES

REPO_ROOT: Final[Path] = Path(__file__).parent.parent

# Workspace layout, per the root `[tool.uv.workspace] members = ["libs/*", "apps/*"]`.
# Only libs/ packages are publishable to PyPI; apps/ (e.g. minds) are desktop
# bundles that are never published. Both parents are still scanned for pin
# alignment, because apps pin internal packages too.
_PUBLISHABLE_PARENT: Final[str] = "libs"
_WORKSPACE_PARENTS: Final[tuple[str, ...]] = ("libs", "apps")


class PackageInfo(FrozenModel):
    """Metadata for a publishable package and its internal dependencies."""

    dir_name: str = Field(description="Directory name under libs/")
    pypi_name: str = Field(description="PyPI package name")
    internal_deps: tuple[str, ...] = Field(
        description="PyPI names of publishable workspace packages this one depends on at runtime"
    )

    @computed_field
    @cached_property
    def pyproject_path(self) -> Path:
        # Publishable packages always live under libs/ (see _PUBLISHABLE_PARENT).
        return REPO_ROOT / "libs" / self.dir_name / "pyproject.toml"


def normalize_pypi_name(name: str) -> str:
    """PEP 503 normalization: lowercase and replace runs of [-_.] with a single dash."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_dep_name(dep_str: str) -> str:
    """Extract and normalize the package name from a dependency string like 'foo==1.0' or 'foo>=2.0'."""
    match = re.match(r"^([A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?)", dep_str)
    if match is None:
        raise ValueError(f"Cannot parse dependency name from: {dep_str!r}")
    return normalize_pypi_name(match.group(1))


def parse_exact_pin(dep_str: str) -> str | None:
    """Return the version from a `==` pin in a dependency string, or None if it isn't `==` pinned.

    Stops at whitespace, commas, and semicolons so it ignores any environment marker
    (``; python_version < ...``) or additional constraints.
    """
    match = re.search(r"==\s*([^\s,;]+)", dep_str)
    return match.group(1) if match is not None else None


def _iter_runtime_dependency_strings(pyproject_data: dict) -> Iterator[str]:
    """Yield the runtime dependency strings: ``[project.dependencies]`` and every
    ``[project.optional-dependencies]`` extra.

    These are the requirements that ship in the built wheel's metadata, so they are
    what must resolve when a consumer installs the package from PyPI. Dev-only
    ``[dependency-groups]`` are deliberately excluded (see _iter_all_dependency_strings).
    """
    project = pyproject_data.get("project", {})
    yield from project.get("dependencies", [])
    for extra in project.get("optional-dependencies", {}).values():
        yield from extra


def _iter_all_dependency_strings(pyproject_data: dict) -> Iterator[str]:
    """Yield every dependency string in a pyproject: runtime deps plus PEP 735
    ``[dependency-groups]`` (e.g. ``dev``).

    Group entries that are ``{include-group = "..."}`` tables rather than plain
    strings are skipped. Used for pin alignment / consistency checks, which must
    catch a stale pin wherever it lives (a stale dev-group pin still breaks
    ``uv lock``).
    """
    yield from _iter_runtime_dependency_strings(pyproject_data)
    for group in pyproject_data.get("dependency-groups", {}).values():
        for entry in group:
            if isinstance(entry, str):
                yield entry


def iter_workspace_member_dirs() -> Iterator[tuple[str, Path]]:
    """Yield ``(parent, directory)`` for every workspace member holding a pyproject.toml.

    ``parent`` is one of ``_WORKSPACE_PARENTS`` (``"libs"`` / ``"apps"``).
    """
    for parent in _WORKSPACE_PARENTS:
        parent_dir = REPO_ROOT / parent
        if not parent_dir.is_dir():
            continue
        for child in sorted(parent_dir.iterdir()):
            if (child / "pyproject.toml").is_file():
                yield parent, child


def _workspace_member_name(pyproject_data: dict) -> str | None:
    """Return the normalized PyPI name declared in a pyproject, or None if absent."""
    name = pyproject_data.get("project", {}).get("name")
    return normalize_pypi_name(name) if name is not None else None


def get_workspace_package_versions() -> dict[str, str]:
    """Return ``{pypi_name: version}`` for EVERY workspace package (libs + apps),
    whether or not it is published.

    Pin alignment and the consistency check must be able to look up the version of
    any internal package a pin might point at, including ones excluded from
    publication.
    """
    versions: dict[str, str] = {}
    for _parent, child in iter_workspace_member_dirs():
        data = tomllib.loads((child / "pyproject.toml").read_text())
        name = _workspace_member_name(data)
        version = data.get("project", {}).get("version")
        if name is not None and version is not None:
            versions[name] = version
    return versions


def get_package_versions() -> dict[str, str]:
    """Read the version from each publishable package. Returns {pypi_name: version}."""
    versions: dict[str, str] = {}
    for pkg in PACKAGES:
        data = tomllib.loads(pkg.pyproject_path.read_text())
        versions[pkg.pypi_name] = data["project"]["version"]
    return versions


def _topologically_sort(infos: list[PackageInfo]) -> tuple[PackageInfo, ...]:
    """Order packages so every package follows all of its internal dependencies.

    Callers (e.g. release.py's bump-level cascade) rely on PACKAGES being in this
    order. Ties are broken alphabetically for a deterministic, stable ordering.
    """
    by_name = {pkg.pypi_name: pkg for pkg in infos}
    indegree: dict[str, int] = {pkg.pypi_name: 0 for pkg in infos}
    dependents: dict[str, list[str]] = {pkg.pypi_name: [] for pkg in infos}
    for pkg in infos:
        for dep in pkg.internal_deps:
            dependents[dep].append(pkg.pypi_name)
            indegree[pkg.pypi_name] += 1

    ready: deque[str] = deque(sorted(name for name, degree in indegree.items() if degree == 0))
    order: list[PackageInfo] = []
    while ready:
        name = ready.popleft()
        order.append(by_name[name])
        newly_ready: list[str] = []
        for dependent in dependents[name]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                newly_ready.append(dependent)
        ready.extend(sorted(newly_ready))

    if len(order) != len(infos):
        raise ValueError("Cycle detected in the publishable package dependency graph")
    return tuple(order)


def _discover_publishable_packages() -> tuple[PackageInfo, ...]:
    """Auto-discover the publish graph from the workspace.

    A package is publishable iff it lives under libs/ and is NOT listed in
    UNPUBLISHED_PACKAGES. Each package's ``internal_deps`` are the publishable
    workspace packages it depends on at runtime (so a bump cascades to dependents
    whose published pin would change). Returns the packages in topological order.
    """
    name_to_dir: dict[str, tuple[str, Path]] = {}
    for parent, child in iter_workspace_member_dirs():
        data = tomllib.loads((child / "pyproject.toml").read_text())
        name = _workspace_member_name(data)
        if name is not None:
            name_to_dir[name] = (parent, child)

    publishable = {
        name
        for name, (parent, _child) in name_to_dir.items()
        if parent == _PUBLISHABLE_PARENT and name not in UNPUBLISHED_PACKAGES
    }

    infos: list[PackageInfo] = []
    for name in publishable:
        _parent, child = name_to_dir[name]
        data = tomllib.loads((child / "pyproject.toml").read_text())
        dep_names = {parse_dep_name(dep) for dep in _iter_runtime_dependency_strings(data)}
        internal = tuple(sorted(dep for dep in dep_names if dep in publishable and dep != name))
        infos.append(PackageInfo(dir_name=child.name, pypi_name=name, internal_deps=internal))

    return _topologically_sort(infos)


# The publish graph, auto-discovered from the workspace at import time. Everything
# under libs/ that is not deliberately excluded (UNPUBLISHED_PACKAGES) is here.
PACKAGES: Final[tuple[PackageInfo, ...]] = _discover_publishable_packages()

PACKAGE_BY_PYPI_NAME: Final[dict[str, PackageInfo]] = {pkg.pypi_name: pkg for pkg in PACKAGES}


def validate_package_graph() -> None:
    """Assert the auto-discovered publish graph is *closed* under runtime dependencies.

    A publishable package must not have a runtime dependency on a workspace package
    that is excluded from publication (listed in UNPUBLISHED_PACKAGES): that
    dependency is never uploaded, so the published wheel would be unresolvable on
    PyPI. If this fires, either publish the dependency (remove it from
    UNPUBLISHED_PACKAGES) or drop the runtime dependency.

    Dev-only ``[dependency-groups]`` are not checked: they are not part of the
    published wheel's requirements, so a publishable package may use an unpublished
    package as a dev dependency.
    """
    all_versions = get_workspace_package_versions()
    publishable = {pkg.pypi_name for pkg in PACKAGES}
    for pkg in PACKAGES:
        data = tomllib.loads(pkg.pyproject_path.read_text())
        for dep_str in _iter_runtime_dependency_strings(data):
            dep_name = parse_dep_name(dep_str)
            if dep_name == pkg.pypi_name:
                continue
            if dep_name in all_versions and dep_name not in publishable:
                raise ValueError(
                    f"Publishable package {pkg.pypi_name} has a runtime dependency on workspace "
                    f"package {dep_name}, which is excluded from publication (it is in "
                    f"UNPUBLISHED_PACKAGES). This wheel cannot resolve on PyPI. Either publish "
                    f"{dep_name} or drop the dependency."
                )


def verify_pin_consistency() -> list[str]:
    """Check internal dependency pins across the entire workspace.

    Returns a list of error strings (empty means everything is consistent). Two rules:

    1. Every publishable package must pin each of its publishable *runtime* internal
       deps with ``==`` (so the published wheel resolves on PyPI). A missing pin is an
       error.
    2. Anywhere in the workspace -- any package, any dependency table including dev
       ``[dependency-groups]`` and apps/ -- a ``==`` pin pointing at an internal
       workspace package must match that package's current version. A stale pin is an
       error. This is what keeps the override-free pyproject that
       ``apps/minds/scripts/build.js`` stages resolvable under ``uv lock``.
    """
    all_versions = get_workspace_package_versions()
    publishable = {pkg.pypi_name for pkg in PACKAGES}
    errors: list[str] = []

    # Rule 1: publishable packages must pin their publishable runtime internal deps.
    for pkg in PACKAGES:
        data = tomllib.loads(pkg.pyproject_path.read_text())
        for dep_str in _iter_runtime_dependency_strings(data):
            dep_name = parse_dep_name(dep_str)
            if dep_name == pkg.pypi_name or dep_name not in publishable:
                continue
            if parse_exact_pin(dep_str) is None:
                errors.append(f"{pkg.pypi_name}: internal dep {dep_name} not pinned with ==: {dep_str!r}")

    # Rule 2: every == pin to an internal package, anywhere in the workspace, must match.
    for parent, child in iter_workspace_member_dirs():
        data = tomllib.loads((child / "pyproject.toml").read_text())
        self_name = _workspace_member_name(data)
        for dep_str in _iter_all_dependency_strings(data):
            dep_name = parse_dep_name(dep_str)
            if dep_name == self_name or dep_name not in all_versions:
                continue
            pinned = parse_exact_pin(dep_str)
            if pinned is not None and pinned != all_versions[dep_name]:
                errors.append(
                    f"{parent}/{child.name}: pin for {dep_name} is {pinned} "
                    f"but {dep_name} is at version {all_versions[dep_name]}"
                )

    return errors
