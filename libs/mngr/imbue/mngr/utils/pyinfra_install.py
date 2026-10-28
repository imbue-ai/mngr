import importlib.metadata
import sys
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path

import psutil
import pyinfra
from pydantic import Field
from pyinfra.api.connectors import get_all_connectors
from pyinfra.api.connectors import get_execution_connectors

from imbue.imbue_common.frozen_model import FrozenModel


class PyinfraInstallSnapshot(FrozenModel):
    """What this process can currently see of its pyinfra install, for diagnosing a vanished connector.

    pyinfra rebuilds its connector registry from package entry points on every
    ``Inventory``, so a long-running process whose venv is rewritten under it can
    lose a connector it resolved earlier, while every module it already imported
    keeps working.
    """

    module_dir: Path = Field(description="The imported pyinfra package directory")
    is_module_dir_present: bool = Field(description="Whether that directory still exists on disk")
    distribution_location: Path | None = Field(
        description="Directory holding the pyinfra distribution metadata that a lookup finds now, or None if none is found"
    )
    distribution_version: str | None = Field(description="Version of that distribution, or None if none is found")
    is_distribution_beside_module: bool = Field(
        description="Whether that metadata sits in the site-packages directory holding the imported package"
    )
    connector_names: tuple[str, ...] | None = Field(
        description="Names in pyinfra's connector entry-point group, or None if a connector failed to load"
    )
    execution_connector_names: tuple[str, ...] | None = Field(
        description="The subset pyinfra treats as execution connectors, or None if a connector failed to load"
    )
    connector_load_error: str | None = Field(description="Why a connector failed to load, or None if they all loaded")
    sys_prefix: Path = Field(description="This interpreter's sys.prefix")
    is_sys_prefix_present: bool = Field(description="Whether sys.prefix still exists on disk")
    site_packages_modified_at: datetime | None = Field(
        description="Modification time of the site-packages directory holding the imported package, or None if unreadable"
    )
    process_started_at: datetime = Field(description="When this process started")
    process_uptime_seconds: float = Field(description="Seconds since this process started")


def take_pyinfra_install_snapshot() -> PyinfraInstallSnapshot:
    module_dir = Path(pyinfra.__file__).parent
    site_packages_dir = module_dir.parent

    try:
        distribution = importlib.metadata.distribution("pyinfra")
    except importlib.metadata.PackageNotFoundError:
        distribution_location = None
        distribution_version = None
    else:
        distribution_location = Path(str(distribution.locate_file("")))
        distribution_version = distribution.version

    connector_load_error = None
    try:
        connector_names = tuple(sorted(get_all_connectors()))
        execution_connector_names = tuple(sorted(get_execution_connectors()))
    # A package rewritten mid-scan can fail to import in any of these ways.
    except (ImportError, SyntaxError, AttributeError, ValueError, OSError) as e:
        connector_names = None
        execution_connector_names = None
        connector_load_error = repr(e)

    try:
        site_packages_modified_at = datetime.fromtimestamp(site_packages_dir.stat().st_mtime, tz=timezone.utc)
    except OSError:
        site_packages_modified_at = None

    process_started_timestamp = psutil.Process().create_time()
    sys_prefix = Path(sys.prefix)
    return PyinfraInstallSnapshot(
        module_dir=module_dir,
        is_module_dir_present=module_dir.is_dir(),
        distribution_location=distribution_location,
        distribution_version=distribution_version,
        is_distribution_beside_module=distribution_location == site_packages_dir,
        connector_names=connector_names,
        execution_connector_names=execution_connector_names,
        connector_load_error=connector_load_error,
        sys_prefix=sys_prefix,
        is_sys_prefix_present=sys_prefix.is_dir(),
        site_packages_modified_at=site_packages_modified_at,
        process_started_at=datetime.fromtimestamp(process_started_timestamp, tz=timezone.utc),
        process_uptime_seconds=time.time() - process_started_timestamp,
    )
