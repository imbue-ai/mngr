import tomllib

from imbue.mngr.plugin_catalog import UNPUBLISHED_PACKAGES
from scripts.utils import PACKAGES
from scripts.utils import iter_workspace_member_dirs
from scripts.utils import normalize_pypi_name
from scripts.utils import validate_package_graph
from scripts.utils import verify_pin_consistency


def test_publish_graph_is_closed() -> None:
    """No publishable package may have a runtime dependency on an unpublished workspace package."""
    validate_package_graph()


def test_internal_dep_pins_are_consistent() -> None:
    """All internal deps must use == pins that match the depended-on package's actual version."""
    errors = verify_pin_consistency()
    assert not errors, "\n".join(errors)


def test_every_lib_is_classified() -> None:
    """Every libs/ package is either publishable or explicitly unpublished -- never in limbo.

    This is the guard that the release tooling 'scans everything': a new libs/
    package is published by default (auto-discovered into the graph). If it should
    NOT be published, it must be added to UNPUBLISHED_PACKAGES. Nothing is allowed to
    silently fall through (which is how pins used to go stale unnoticed).
    """
    published_names = {pkg.pypi_name for pkg in PACKAGES}
    unclassified: list[str] = []
    for parent, child in iter_workspace_member_dirs():
        if parent != "libs":
            continue
        name = tomllib.loads((child / "pyproject.toml").read_text()).get("project", {}).get("name")
        if name is None:
            continue
        normalized = normalize_pypi_name(name)
        if normalized not in published_names and normalized not in UNPUBLISHED_PACKAGES:
            unclassified.append(normalized)
    assert not unclassified, (
        f"These libs/ packages are neither published nor in UNPUBLISHED_PACKAGES: {sorted(unclassified)}. "
        f"Add each to UNPUBLISHED_PACKAGES (plugin_catalog.py) if it should not publish, otherwise it will be "
        f"offered for release."
    )


def test_unpublished_packages_are_real_workspace_packages() -> None:
    """UNPUBLISHED_PACKAGES must not list stale names that no longer match any workspace package."""
    workspace_names = set()
    for _parent, child in iter_workspace_member_dirs():
        name = tomllib.loads((child / "pyproject.toml").read_text()).get("project", {}).get("name")
        if name is not None:
            workspace_names.add(normalize_pypi_name(name))
    stale = UNPUBLISHED_PACKAGES - workspace_names
    assert not stale, f"UNPUBLISHED_PACKAGES lists names with no matching workspace package: {sorted(stale)}"
