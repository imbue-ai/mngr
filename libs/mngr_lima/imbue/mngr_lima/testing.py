from pathlib import Path

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_lima.config import LimaProviderConfig
from imbue.mngr_lima.instance import LimaProviderInstance


def make_lima_provider(
    mngr_ctx: MngrContext,
    host_dir: Path | None = None,
) -> LimaProviderInstance:
    """Create a LimaProviderInstance for testing without limactl checks."""
    config = LimaProviderConfig(
        host_dir=host_dir or Path("/mngr"),
        default_idle_timeout=60,
    )
    return LimaProviderInstance(
        name=ProviderInstanceName("lima-test"),
        host_dir=config.host_dir or Path("/mngr"),
        mngr_ctx=mngr_ctx,
        config=config,
    )
