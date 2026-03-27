"""Unit tests for the mngr_mind provisioning module."""

from pathlib import Path
from typing import Any
from typing import cast

from imbue.mngr_llm.data_types import ProvisioningSettings
from imbue.mngr_mind.conftest import StubCommandResult
from imbue.mngr_mind.conftest import StubHost
from imbue.mngr_mind.provisioning import provision_link_skills_script_file

_DEFAULT_PROVISIONING = ProvisioningSettings()


def test_provision_link_skills_script_file_writes_when_missing() -> None:
    """Verify that provision_link_skills_script_file writes link_skills.sh when it doesn't exist."""
    host = StubHost(command_results={"test -f": StubCommandResult(success=False)})
    provision_link_skills_script_file(cast(Any, host), Path("/test/work"), _DEFAULT_PROVISIONING)

    written_paths = [str(p) for p, _ in host.written_text_files]
    assert any("link_skills.sh" in p for p in written_paths)
    _, content = host.written_text_files[0]
    assert "#!/usr/bin/env bash" in content
    assert "ln -s" in content


def test_provision_link_skills_script_file_does_not_overwrite_existing() -> None:
    """Verify that provision_link_skills_script_file skips when file exists."""
    host = StubHost()
    provision_link_skills_script_file(cast(Any, host), Path("/test/work"), _DEFAULT_PROVISIONING)

    assert len(host.written_text_files) == 0
