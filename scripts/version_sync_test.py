from imbue.mngr.plugin_catalog import UNPUBLISHED_PACKAGES
from scripts.utils import PACKAGES
from scripts.utils import validate_package_graph
from scripts.utils import verify_pin_consistency


def test_package_graph_matches_pyproject_files() -> None:
    """The hard-coded package graph must match the actual pyproject.toml dependency declarations."""
    validate_package_graph()


def test_internal_dep_pins_are_consistent() -> None:
    """All internal deps must use == pins that match the depended-on package's actual version."""
    errors = verify_pin_consistency()
    assert not errors, "\n".join(errors)


def test_published_packages_not_in_unpublished_blocklist() -> None:
    """Packages in the release graph must not be in UNPUBLISHED_PACKAGES.

    If a package has been added to the release graph (scripts/utils.py PACKAGES),
    it should be removed from UNPUBLISHED_PACKAGES in plugin_catalog.py so the
    install wizard can offer it to users.
    """
    published_names = {pkg.pypi_name for pkg in PACKAGES}
    blocked = published_names & UNPUBLISHED_PACKAGES
    assert not blocked, (
        f"These packages are in the release graph but still in UNPUBLISHED_PACKAGES "
        f"(remove them from UNPUBLISHED_PACKAGES in plugin_catalog.py): {sorted(blocked)}"
    )
