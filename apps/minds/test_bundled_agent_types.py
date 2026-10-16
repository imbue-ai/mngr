"""Guard: every way of installing minds must carry every agent-type plugin the template uses.

minds runs ``mngr create`` against the default workspace template, and mngr
hard-fails parsing ``.mngr/settings.toml`` when an ``[agent_types.<name>]``
section belongs to a plugin that is not installed ("Unknown fields in
agent_types.<name>"). Two dependency lists decide which plugins are installed,
and neither is the monorepo venv: the packaged desktop app installs the
workspace-package list in ``electron/pyproject/pyproject.toml``, and running
from source (e.g. dev-mode Electron's ``uv run --package minds``) installs only
the ``minds`` package's own dependency closure. A template that grows a new
agent type works in a fully synced dev venv yet breaks every create through
either path, so this test fails as soon as the lists fall behind the template.

Lives in the ``minds_snapshot_resume`` suite because that is the CI stage
whose image carries the paired default-workspace-template worktree the test
reads. It is in its own file (not ``test_snapshot_resume.py``) because it
needs no dockerd, which that module's autouse fixture requires.
"""

import tomllib
from typing import Final

import pytest

from imbue.minds.desktop_client.default_workspace_template_worktree import DEFAULT_WORKSPACE_TEMPLATE_EXTERNAL_WORKTREE
from imbue.minds.testing import MINDS_INSTALLS
from imbue.minds.testing import MindsInstall
from imbue.minds.testing import get_mngr_plugin_package_names
from imbue.minds.testing import read_pyproject_dependency_names
from imbue.mngr.config.agent_plugin_registry import get_agent_type_owner
from imbue.mngr.main import get_or_create_plugin_manager

# Plugins mngr registers itself (not via setuptools entry points); their agent
# types ship inside imbue-mngr, which is always installed.
_BUILTIN_AGENT_PLUGIN_NAMES: Final[frozenset[str]] = frozenset({"command", "headless_command"})


@pytest.mark.minds_snapshot_resume
@pytest.mark.timeout(60)
@pytest.mark.parametrize("minds_install", MINDS_INSTALLS, ids=lambda install: install.test_id)
def test_template_agent_types_are_installed_with_minds(
    minds_install: MindsInstall,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_path = DEFAULT_WORKSPACE_TEMPLATE_EXTERNAL_WORKTREE / ".mngr" / "settings.toml"
    assert settings_path.is_file(), (
        f"No default-workspace-template settings at {settings_path}. This test needs the paired "
        "template worktree (baked into the snapshot image; created locally by "
        "materialize_paired_default_workspace_template_worktree or `just dwt-worktree`)."
    )
    agent_type_sections = tomllib.loads(settings_path.read_text()).get("agent_types", {})
    assert agent_type_sections, f"{settings_path} declares no [agent_types.*] sections -- template layout changed?"

    # The base agent types the template requires: sections configuring an
    # existing type directly (no parent_type), plus any parent_type target not
    # itself defined in the file. Sections WITH parent_type define new
    # template-local types and need no plugin of their own.
    declared_names = set(agent_type_sections)
    derived_names = {name for name, section in agent_type_sections.items() if "parent_type" in section}
    referenced_parent_names = {
        section["parent_type"] for section in agent_type_sections.values() if "parent_type" in section
    }
    required_base_type_names = (declared_names - derived_names) | (referenced_parent_names - declared_names)

    # Load every installed mngr plugin so the agent-type owner registry is
    # populated exactly as `mngr` itself populates it.
    monkeypatch.setenv("MNGR_LOAD_ALL_PLUGINS", "1")
    get_or_create_plugin_manager()

    package_by_plugin_name = get_mngr_plugin_package_names()
    installed_package_names = read_pyproject_dependency_names(minds_install.pyproject_path)

    problems: list[str] = []
    for agent_type_name in sorted(required_base_type_names):
        owner_plugin_name = get_agent_type_owner(agent_type_name)
        if owner_plugin_name is None:
            problems.append(
                f"agent type '{agent_type_name}' (declared in {settings_path}) is not registered by any "
                "installed mngr plugin -- either the plugin is missing from the monorepo venv, or the "
                "template references a type that no longer exists."
            )
        elif owner_plugin_name in _BUILTIN_AGENT_PLUGIN_NAMES:
            pass
        else:
            package_name = package_by_plugin_name.get(owner_plugin_name)
            if package_name is None:
                problems.append(
                    f"agent type '{agent_type_name}' is owned by plugin '{owner_plugin_name}', which has no "
                    "matching setuptools entry point in the 'mngr' group -- cannot determine its package."
                )
            elif package_name.lower() in installed_package_names:
                pass
            else:
                problems.append(
                    f"agent type '{agent_type_name}' is provided by package '{package_name}', which is NOT "
                    f"a dependency of {minds_install.description}. Every `mngr create` from it will fail with "
                    f'"Unknown fields in agent_types.{agent_type_name}". Fix: {minds_install.fix_instructions}'
                )
    assert not problems, (
        f"The default-workspace-template declares agent types {minds_install.description} cannot parse:\n- "
        + "\n- ".join(problems)
    )
