"""Acceptance test: mngr_claude_usage's provisioner works on a Modal (remote) host.

Verifies the host-portability claim made by ``_provision_statusline_shim`` --
that all file I/O goes through ``OnlineHostInterface`` methods (``write_file``,
``read_text_file``) and therefore works the same way on a remote Modal host as
it does on a local filesystem.

This test lives in mngr_modal because the Modal fixtures (``real_modal_provider``)
are here; the import from mngr_claude_usage works because both packages are
workspace members and their source is on the test path. Marked
``@pytest.mark.acceptance`` so it only runs when explicitly requested (and the
fixture handles Modal env cleanup).
"""

import json

import pytest

from imbue.mngr.api.testing import created_host
from imbue.mngr.primitives import HostName
from imbue.mngr_claude_usage.plugin import _provision_statusline_shim
from imbue.mngr_claude_usage.plugin import _stable_shim_path
from imbue.mngr_modal.instance import ModalProviderInstance


@pytest.mark.modal
@pytest.mark.acceptance
@pytest.mark.timeout(300)
def test_provision_statusline_shim_on_modal_host(real_modal_provider: ModalProviderInstance) -> None:
    """Provision the usage statusline shim onto a real Modal sandbox host
    and verify each artifact lands correctly via ``host.read_text_file``.

    Covers the full host-portable pipeline:
    - ``read_json_dict_via_host`` reads the planted pre-existing
      ``settings.json`` from the remote host's work_dir.
    - ``install_packaged_script_on_host`` writes the shim and the writer
      scripts onto the remote host's host-stable ``<host_dir>/commands/``
      directory with mode 0755.
    - The per-agent user-statusline sidecar under ``<state_dir>/commands/``
      and the wrapping ``<work_dir>/.claude/settings.local.json`` get
      written through the remote host as well.
    """
    with created_host(real_modal_provider, HostName("usage-test")) as host:
        # State dir follows mngr core's ``get_agent_state_dir_path`` layout
        # (``<host_dir>/agents/<id>``) so the sidecar lands where the shim
        # expects it at render time.
        state_dir = host.host_dir / "agents" / "agent-modal-test"
        work_dir = host.host_dir / "work"

        # Plant a pre-existing user statusline in <work_dir>/.claude/settings.json
        # so the provisioner has something to capture into the sidecar.
        pre_existing_command = "/usr/local/bin/my-statusline.sh"
        host.write_file(
            work_dir / ".claude" / "settings.json",
            json.dumps({"statusLine": {"command": pre_existing_command}}).encode(),
        )

        # Run the provisioner against the remote host.
        _provision_statusline_shim(host, state_dir, work_dir)

        # The shim and writer scripts land under the host-stable commands dir,
        # not the per-agent state_dir.
        shim_path = _stable_shim_path(host.host_dir)
        writer_path = host.host_dir / "commands" / "claude_usage_writer.sh"
        shim_content = host.read_text_file(shim_path)
        writer_content = host.read_text_file(writer_path)
        # Shell scripts -- shebang sanity-check is enough here; content matches
        # the package resource and is exercised more thoroughly by the
        # in-process tests.
        assert shim_content.startswith("#!/bin/bash")
        assert writer_content.startswith("#!/bin/bash")

        # The runtime sidecar lives under the per-agent state_dir/commands/.
        sidecar = host.read_text_file(state_dir / "commands" / "user_statusline_cmd")
        assert sidecar == pre_existing_command

        # settings.local.json on the remote host now points at the stable shim.
        installed_settings = json.loads(host.read_text_file(work_dir / ".claude" / "settings.local.json"))
        assert installed_settings["statusLine"] == {"type": "command", "command": str(shim_path)}

        # Re-provisioning is idempotent: shim path stays, sidecar still has the
        # user's pre-existing command (not our shim).
        _provision_statusline_shim(host, state_dir, work_dir)
        sidecar_after = host.read_text_file(state_dir / "commands" / "user_statusline_cmd")
        assert sidecar_after == pre_existing_command
        installed_settings_after = json.loads(host.read_text_file(work_dir / ".claude" / "settings.local.json"))
        assert installed_settings_after["statusLine"]["command"] == str(shim_path)
