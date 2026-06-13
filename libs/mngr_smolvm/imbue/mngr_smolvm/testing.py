from pathlib import Path

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_smolvm.config import SmolvmProviderConfig
from imbue.mngr_smolvm.instance import SmolvmProviderInstance


def make_smolvm_provider(
    mngr_ctx: MngrContext,
    host_dir: Path | None = None,
    smolvm_command: str = "smolvm",
) -> SmolvmProviderInstance:
    """Create a SmolvmProviderInstance for testing without smolvm checks.

    Pass smolvm_command to point the provider at a stub script standing in
    for the real smolvm binary.
    """
    config = SmolvmProviderConfig(
        smolvm_command=smolvm_command,
        host_dir=host_dir or Path("/mngr"),
        default_idle_timeout=60,
    )
    return SmolvmProviderInstance(
        name=ProviderInstanceName("smolvm-test"),
        host_dir=config.host_dir or Path("/mngr"),
        mngr_ctx=mngr_ctx,
        config=config,
    )
