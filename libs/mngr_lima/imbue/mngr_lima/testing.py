import os
from pathlib import Path

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_lima.config import LimaProviderConfig
from imbue.mngr_lima.instance import LimaProviderInstance


def install_fake_limactl(directory: Path, script_body: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Install an executable fake ``limactl`` in ``directory`` and prepend it to PATH."""
    directory.mkdir(parents=True, exist_ok=True)
    fake = directory / "limactl"
    fake.write_text("#!/bin/sh\n" + script_body)
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{directory}:{os.environ['PATH']}")


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
