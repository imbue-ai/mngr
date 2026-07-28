from pathlib import Path

from inline_snapshot import snapshot

from imbue.imbue_common.ratchet_testing.core import FileExtension
from imbue.imbue_common.ratchet_testing.core import RegexPattern
from imbue.imbue_common.ratchet_testing.core import check_regex_ratchet
from imbue.imbue_common.ratchet_testing.core import format_ratchet_failure_message

_DIR = Path(__file__).parent


def test_prevent_ambient_provider_host_dir() -> None:
    pattern = RegexPattern(r"self\.host_dir", multiline=False)
    # config.py is the provider *config* class, where host_dir is its own field and reading it
    # is the point (validators, defaults). The anti-pattern is on the provider *instance*.
    chunks = check_regex_ratchet(_DIR, FileExtension(".py"), pattern, excluded_path_patterns=("config.py",))
    assert len(chunks) <= snapshot(3), format_ratchet_failure_message(
        rule_name="ambient provider host_dir reads",
        rule_description=(
            "A provider instance's own host_dir attribute is resolved from whatever settings the "
            "current process's cwd selects, so it answers 'where would a NEW host put its "
            "state', not 'where did THIS host put its state'. The two disagree whenever a host "
            "was created from a repo whose `.mngr/settings.toml` sets a custom host_dir and is "
            "later read from elsewhere -- `mngr create` runs in the workspace clone while "
            "`mngr forward` and `mngr event` run from $HOME. Reads that miss this find no "
            "agents and fail silently. It is correct ONLY inside create_host, where the ambient "
            "value IS the one being recorded. Anywhere a Host or HostRecord is in scope, use "
            "`host.host_dir` (which honors the recorded per-host value via host_dir_override) "
            "or `_recorded_host_dir_override(record)`."
        ),
        chunks=chunks,
    )
