import re
import tomllib
from functools import cached_property
from pathlib import Path
from typing import Final

from pydantic import Field
from pydantic import computed_field

from imbue.imbue_common.frozen_model import FrozenModel

REPO_ROOT: Final[Path] = Path(__file__).parent.parent


class PackageInfo(FrozenModel):
    """Metadata for a publishable package and its internal dependencies."""

    dir_name: str = Field(description="Directory name under libs/")
    pypi_name: str = Field(description="PyPI package name")
    internal_deps: tuple[str, ...] = Field(description="PyPI names of internal dependencies")

    @computed_field
    @cached_property
    def pyproject_path(self) -> Path:
        return REPO_ROOT / "libs" / self.dir_name / "pyproject.toml"


# Hard-coded dependency graph. Validated by tests against actual pyproject.toml files.
PACKAGES: Final[tuple[PackageInfo, ...]] = (
    PackageInfo(dir_name="imbue_common", pypi_name="imbue-common", internal_deps=()),
    PackageInfo(dir_name="concurrency_group", pypi_name="concurrency-group", internal_deps=("imbue-common",)),
    PackageInfo(dir_name="mng", pypi_name="mng", internal_deps=("imbue-common", "concurrency-group")),
    PackageInfo(dir_name="mng_pair", pypi_name="mng-pair", internal_deps=("mng",)),
    PackageInfo(dir_name="mng_opencode", pypi_name="mng-opencode", internal_deps=("mng",)),
    PackageInfo(dir_name="mng_kanpan", pypi_name="mng-kanpan", internal_deps=("mng",)),
    PackageInfo(dir_name="mng_tutor", pypi_name="mng-tutor", internal_deps=("mng",)),
)

PACKAGE_BY_PYPI_NAME: Final[dict[str, PackageInfo]] = {pkg.pypi_name: pkg for pkg in PACKAGES}


def normalize_pypi_name(name: str) -> str:
    """PEP 503 normalization: lowercase and replace runs of [-_.] with a single dash."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_dep_name(dep_str: str) -> str:
    """Extract and normalize the package name from a dependency string like 'foo==1.0' or 'foo>=2.0'."""
    match = re.match(r"^([A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?)", dep_str)
    if match is None:
        raise ValueError(f"Cannot parse dependency name from: {dep_str!r}")
    return normalize_pypi_name(match.group(1))


def get_package_versions() -> dict[str, str]:
    """Read the version from each publishable package. Returns {pypi_name: version}."""
    versions: dict[str, str] = {}
    for pkg in PACKAGES:
        data = tomllib.loads(pkg.pyproject_path.read_text())
        versions[pkg.pypi_name] = data["project"]["version"]
    return versions


def validate_package_graph() -> None:
    """Assert the hard-coded graph matches actual pyproject.toml dependency declarations.

    For each package, verify that every internal dep listed in PACKAGES actually appears
    in the package's dependencies, and that no unlisted internal deps are present.
    """
    internal_names = {pkg.pypi_name for pkg in PACKAGES}

    for pkg in PACKAGES:
        data = tomllib.loads(pkg.pyproject_path.read_text())
        raw_deps: list[str] = data["project"].get("dependencies", [])
        actual_internal = {dep_name for dep in raw_deps if (dep_name := parse_dep_name(dep)) in internal_names}
        expected_internal = set(pkg.internal_deps)

        if actual_internal != expected_internal:
            raise ValueError(
                f"Package graph mismatch for {pkg.pypi_name}: "
                f"expected internal deps {sorted(expected_internal)}, "
                f"got {sorted(actual_internal)}"
            )


def verify_pin_consistency() -> list[str]:
    """Check that all internal dep pins use == and match the depended-on package's version.

    Returns a list of error strings. Empty list means everything is consistent.
    """
    internal_names = {pkg.pypi_name for pkg in PACKAGES}
    versions = get_package_versions()
    errors: list[str] = []

    for pkg in PACKAGES:
        data = tomllib.loads(pkg.pyproject_path.read_text())
        raw_deps: list[str] = data["project"].get("dependencies", [])
        for dep_str in raw_deps:
            dep_name = parse_dep_name(dep_str)
            if dep_name not in internal_names:
                continue
            pin_match = re.search(r"==(.+)$", dep_str)
            if pin_match is None:
                errors.append(f"{pkg.pypi_name}: internal dep {dep_name} not pinned: {dep_str!r}")
                continue
            pinned_version = pin_match.group(1)
            expected_version = versions[dep_name]
            if pinned_version != expected_version:
                errors.append(
                    f"{pkg.pypi_name}: pin for {dep_name} is {pinned_version} "
                    f"but {dep_name} is at version {expected_version}"
                )

    return errors
