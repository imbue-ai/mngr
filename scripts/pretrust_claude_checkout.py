"""Pre-seed the global Claude config so an agent can run unattended in CI.

When a Claude agent runs on the local host, mngr_claude's provisioning path goes
through Claude Code's trust dialog (see
mngr_claude/plugin.py:interactively_dismiss_claude_dialogs). In CI there is no
human to accept the dialog and no prior ~/.claude.json entry to inherit, so
without seeding trust the agent either raises ClaudeDirectoryNotTrustedError or
hangs waiting on the dialog. This script pre-seeds the global ~/.claude.json with
trust for the checkout plus the other dialogs the plugin gates on; a per-agent
config then inherits the trust via the plugin's copy_project_config_from path.

This is used by the TMR CI workflows (.github/actions/tmr-setup), but nothing
here is TMR-specific -- any CI job that launches a local Claude agent against the
checkout can run it.

Run from the repo root (which must be the checkout to trust). The ``--project``
selects the env where ``imbue.mngr_claude`` is importable:

    uv run --project libs/mngr_claude python scripts/pretrust_claude_checkout.py

This lives as a real module (rather than an inline heredoc in the CI workflow)
so the type checker catches breakages when claude_config's API changes.
"""

from pathlib import Path

from imbue.mngr_claude.claude_config import add_claude_trust_for_path
from imbue.mngr_claude.claude_config import complete_onboarding
from imbue.mngr_claude.claude_config import dismiss_effort_callout
from imbue.mngr_claude.claude_config import find_user_config_in_isolated_mode


def main() -> None:
    config_path = find_user_config_in_isolated_mode()
    add_claude_trust_for_path(config_path, Path.cwd().resolve())
    dismiss_effort_callout(config_path)
    complete_onboarding(config_path)


if __name__ == "__main__":
    main()
