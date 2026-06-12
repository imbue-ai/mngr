from pathlib import Path

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_smolvm.config import SmolvmProviderConfig
from imbue.mngr_smolvm.instance import SmolvmProviderInstance


def make_smolvm_provider(
    mngr_ctx: MngrContext,
    host_dir: Path | None = None,
) -> SmolvmProviderInstance:
    """Create a SmolvmProviderInstance for testing without smolvm checks."""
    config = SmolvmProviderConfig(
        host_dir=host_dir or Path("/mngr"),
        default_idle_timeout=60,
    )
    return SmolvmProviderInstance(
        name=ProviderInstanceName("smolvm-test"),
        host_dir=config.host_dir or Path("/mngr"),
        mngr_ctx=mngr_ctx,
        config=config,
    )
