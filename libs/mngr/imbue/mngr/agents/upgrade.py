import json
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.mngr.errors import InvalidUpgradeInputError
from imbue.mngr.hosts.host import SHARED_SHELL_LIB_NAMES
from imbue.mngr.hosts.host import read_shared_shell_lib
from imbue.mngr.interfaces.agent import UpgradableAgentMixin
from imbue.mngr.interfaces.data_types import HarnessFileSpec

_COMMAND_SCRIPT_MODE: Final[str] = "0755"

# The agent's record, relative to its state dir, which holds the launch command mngr stored at create.
AGENT_DATA_RELATIVE_PATH: Final[Path] = Path("data.json")


@pure
def read_stored_launch_command(agent_data_content: str | None) -> str:
    """The launch command in the content of an agent's record, empty when it stores none or the file is missing."""
    if agent_data_content is None:
        return ""
    try:
        agent_data = json.loads(agent_data_content)
    except json.JSONDecodeError as e:
        raise InvalidUpgradeInputError(f"{AGENT_DATA_RELATIVE_PATH} is not valid JSON: {e}") from e
    command = agent_data.get("command") if isinstance(agent_data, dict) else None
    return command if isinstance(command, str) else ""


@pure
def parse_json_object_input(relative_path: Path, content: str | None) -> dict[str, Any]:
    """The JSON object in the content of the upgrade input at ``relative_path``, empty when the file is missing."""
    if content is None:
        return {}
    try:
        parsed_content = json.loads(content)
    except json.JSONDecodeError as e:
        raise InvalidUpgradeInputError(f"{relative_path} is not valid JSON: {e}") from e
    if not isinstance(parsed_content, dict):
        raise InvalidUpgradeInputError(f"{relative_path} does not hold a JSON object")
    return parsed_content


@pure
def is_hooks_value_well_formed(hooks: Any) -> bool:
    """Whether ``hooks`` maps each event to a list of groups, each an object whose ``hooks``, if present, is a list."""
    return isinstance(hooks, dict) and all(
        isinstance(hook_groups, list)
        and all(
            isinstance(hook_group, dict) and isinstance(hook_group.get("hooks", []), list)
            for hook_group in hook_groups
        )
        for hook_groups in hooks.values()
    )


# Whether mngr wrote an entry of a hook group, given the entry and the group holding it.
IsMngrHookEntry = Callable[[Any, Mapping[str, Any]], bool]


@pure
def _is_group_holding_mngr_hook_entry(hook_group: Mapping[str, Any], is_mngr_hook_entry: IsMngrHookEntry) -> bool:
    return any(is_mngr_hook_entry(hook_entry, hook_group) for hook_entry in hook_group.get("hooks", []))


@pure
def _drop_mngr_hook_entries(
    hook_group: Mapping[str, Any], is_mngr_hook_entry: IsMngrHookEntry
) -> Mapping[str, Any] | None:
    """The group without mngr's entries: itself when it has none of them, None when it has no other."""
    hook_entries = hook_group.get("hooks", [])
    other_entries = [hook_entry for hook_entry in hook_entries if not is_mngr_hook_entry(hook_entry, hook_group)]
    if len(other_entries) == len(hook_entries):
        return hook_group
    return {**hook_group, "hooks": other_entries} if other_entries else None


@pure
def _rebuild_event_hook_groups(
    live_groups: Sequence[Mapping[str, Any]],
    mngr_groups: Sequence[Mapping[str, Any]],
    is_mngr_hook_entry: IsMngrHookEntry,
) -> list[Any]:
    """``live_groups`` with ``mngr_groups`` in place of mngr's entries: where the first of them was, or last when there was none."""
    first_mngr_group_idx = next(
        (
            idx
            for idx, live_group in enumerate(live_groups)
            if _is_group_holding_mngr_hook_entry(live_group, is_mngr_hook_entry)
        ),
        len(live_groups),
    )
    kept_later_groups = [
        kept_group
        for kept_group in (
            _drop_mngr_hook_entries(live_group, is_mngr_hook_entry)
            for live_group in live_groups[first_mngr_group_idx:]
        )
        if kept_group is not None
    ]
    return [*live_groups[:first_mngr_group_idx], *mngr_groups, *kept_later_groups]


@pure
def rebuild_hook_groups_keeping_other_entries(
    live_hooks: Mapping[str, Sequence[Mapping[str, Any]]],
    mngr_hooks: Mapping[str, Sequence[Mapping[str, Any]]],
    is_mngr_hook_entry: IsMngrHookEntry,
) -> dict[str, Any]:
    """The ``hooks`` value of a shared hooks file holding ``live_hooks``, with ``mngr_hooks`` in place of every entry mngr wrote.

    Each event's groups are rebuilt in place, so groups listed before mngr's stay before them; a group mixing mngr's
    entries with others keeps the others. Live events keep their order, and events only ``mngr_hooks`` has follow them.
    """
    rebuilt_groups_by_live_event = {
        event_name: _rebuild_event_hook_groups(live_groups, mngr_hooks.get(event_name, []), is_mngr_hook_entry)
        for event_name, live_groups in live_hooks.items()
    }
    # An event left with no group is dropped, unless it already had none.
    live_event_hooks = {
        event_name: rebuilt_groups
        for event_name, rebuilt_groups in rebuilt_groups_by_live_event.items()
        if rebuilt_groups or not live_hooks[event_name]
    }
    new_event_hooks = {
        event_name: list(mngr_groups) for event_name, mngr_groups in mngr_hooks.items() if event_name not in live_hooks
    }
    return {**live_event_hooks, **new_event_hooks}


@pure
def build_command_script_files(content_by_script_name: Mapping[str, str]) -> dict[Path, HarnessFileSpec]:
    """Executable scripts in the agent's ``commands/`` dir, keyed by path relative to its state dir."""
    return {
        Path("commands") / script_name: HarnessFileSpec(content=content, mode=_COMMAND_SCRIPT_MODE)
        for script_name, content in content_by_script_name.items()
    }


def build_shared_shell_lib_files() -> dict[Path, HarnessFileSpec]:
    """The shared shell libraries mngr's bash scripts source, as files in the agent's ``commands/`` dir."""
    return build_command_script_files({name: read_shared_shell_lib(name) for name in SHARED_SHELL_LIB_NAMES})


class NoOpUpgradeMixin(UpgradableAgentMixin):
    """Upgrade support for an agent type mngr generates no files for, which ``mngr upgrade`` always finds current."""

    def get_upgrade_input_paths(self) -> tuple[Path, ...]:
        return ()

    def get_upgrade_unsupported_detail(self, input_content_by_path: Mapping[Path, str | None]) -> str | None:
        return None

    def build_upgraded_files(self, input_content_by_path: Mapping[Path, str | None]) -> dict[Path, HarnessFileSpec]:
        return {}

    def get_ready_signal_paths(self) -> tuple[Path, ...]:
        return ()

    def wait_for_ready_after_restart(self, timeout: float) -> None:
        return None
