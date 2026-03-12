"""Unit tests for the mng_mind provisioning module."""

from pathlib import Path
from typing import Any
from typing import cast

import pytest

from imbue.mng_llm.data_types import ProvisioningSettings
from imbue.mng_mind.conftest import StubCommandResult
from imbue.mng_mind.conftest import StubHost
from imbue.mng_mind.provisioning import provision_default_content

_DEFAULT_PROVISIONING = ProvisioningSettings()


@pytest.mark.parametrize(
    "expected_path",
    [
        "GLOBAL.md",
        "thinking/PROMPT.md",
        "thinking/skills/send-message-to-user/SKILL.md",
        "talking/PROMPT.md",
        "working/PROMPT.md",
        "verifying/PROMPT.md",
    ],
    ids=[
        "global_md",
        "thinking_prompt",
        "thinking_skills",
        "talking_prompt",
        "working_prompt",
        "verifying_prompt",
    ],
)
def test_provision_default_content_writes_expected_files(expected_path: str) -> None:
    """Verify that provision_default_content writes each expected default file."""
    host = StubHost(command_results={"test -f": StubCommandResult(success=False)})
    provision_default_content(cast(Any, host), Path("/test/work"), _DEFAULT_PROVISIONING)

    written_paths = [str(p) for p, _ in host.written_text_files]
    assert any(expected_path in p for p in written_paths), f"Expected {expected_path} to be written"


def test_provision_default_content_does_not_overwrite_existing() -> None:
    host = StubHost()
    provision_default_content(cast(Any, host), Path("/test/work"), _DEFAULT_PROVISIONING)

    assert len(host.written_text_files) == 0
