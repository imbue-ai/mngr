"""Tests for the CONFIGURATION tutorial section.

Each test corresponds 1:1 to a tutorial script block.
"""

import tomllib
from pathlib import Path

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


# Runs several mngr invocations (each with non-trivial startup cost), so it needs
# more than the default 10s per-test timeout.
@pytest.mark.timeout(60)
@pytest.mark.release
def test_config_list(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list all configuration values
        mngr config list
    """)
    # `config list` (no scope) prints the configuration merged across all scopes.
    result = e2e.run("mngr config list", comment="list all configuration values")
    expect(result).to_succeed()
    expect(result.stdout).to_contain("Merged configuration (all scopes):")

    # Verify the listing reflects values the user has actually set: set a
    # top-level option, then confirm it appears as a flattened `key = value`
    # line in the merged output. --scope local is used because the harness's
    # local settings file already opts into pytest (is_allowed_in_pytest);
    # writing to the default scope would create a fresh config file that the
    # pytest config-loader guard rejects.
    expect(e2e.run("mngr config set headless true --scope local", comment="set a config value")).to_succeed()
    after = e2e.run("mngr config list", comment="list all configuration values after setting one")
    expect(after).to_succeed()
    expect(after.stdout).to_contain("headless = true")


# Runs three sequential mngr subprocesses; each cold CLI startup takes several
# seconds, so the default 10s function-only timeout is too tight.
@pytest.mark.timeout(60)
@pytest.mark.release
def test_config_list_scope(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list configuration at a specific scope (user, project, or local)
        mngr config list --scope user
        mngr config list --scope project
        mngr config list --scope local
    """)
    # Each scope reads only its own config file, so the three invocations must
    # produce distinct, scope-specific output -- not the merged view.
    user_result = e2e.run("mngr config list --scope user", comment="list user scope")
    expect(user_result).to_succeed()
    expect(user_result.stdout).to_contain("Config from user")

    project_result = e2e.run("mngr config list --scope project", comment="list project scope")
    expect(project_result).to_succeed()
    expect(project_result.stdout).to_contain("Config from project")

    local_result = e2e.run("mngr config list --scope local", comment="list local scope")
    expect(local_result).to_succeed()
    expect(local_result.stdout).to_contain("Config from local")
    # The fixture writes connect_command only into the local-scope settings file,
    # so it must appear under --scope local and must NOT leak into --scope user
    # (which would indicate the scope filter is being ignored).
    expect(local_result.stdout).to_contain("commands.create.connect_command")
    expect(user_result.stdout).not_to_contain("commands.create.connect_command")


@pytest.mark.release
# Two mngr subprocess invocations (seed + get); the default 10s func timeout is
# too tight for back-to-back cold CLI startups.
@pytest.mark.timeout(60)
def test_config_get(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # get a specific config value
        mngr config get commands.create.provider
    """)
    # The tutorial's example key is unset in a fresh config, so seed it first so
    # the get below returns a value rather than "Key not found". `config set`
    # writes the flat `commands.create.provider` path; merged `config get` must
    # resolve that same path back. Seed at the local scope, whose settings file
    # the fixture already marks `is_allowed_in_pytest = true`; writing a fresh
    # project-scope file would lack that opt-in and break the subsequent load.
    expect(
        e2e.run(
            "mngr config set commands.create.provider modal --scope local",
            comment="seed the config value",
        )
    ).to_succeed()
    result = e2e.run("mngr config get commands.create.provider", comment="get a specific config value")
    expect(result).to_succeed()
    # Verify the value actually round-trips through `config get`, not just exit 0.
    expect(result.stdout.strip()).to_equal("modal")


@pytest.mark.release
def test_config_get_missing_key(e2e: E2eSession) -> None:
    # Same tutorial block as `test_config_get`, covering the unhappy path: a key
    # that was never set must fail with a clear "Key not found" error.
    e2e.write_tutorial_block("""
        # get a specific config value
        mngr config get commands.create.provider
    """)
    result = e2e.run("mngr config get commands.create.provider", comment="get an unset config value")
    expect(result).to_fail()
    expect(result.stderr).to_contain("Key not found: commands.create.provider")


@pytest.mark.release
def test_config_set(e2e: E2eSession, project_config_dir: Path) -> None:
    e2e.write_tutorial_block("""
        # set a config value (at the default scope)
        mngr config set commands.create.provider modal
    """)
    set_result = e2e.run("mngr config set commands.create.provider modal", comment="set a config value")
    expect(set_result).to_succeed()
    # The default scope is "project"; the set should report what it wrote and where.
    expect(set_result.stdout).to_contain("commands.create.provider = modal")
    expect(set_result.stdout).to_contain("project")
    # Verify the concrete effect by reading the project-scope config file directly
    # from disk (a follow-up `mngr` command would trip the pytest opt-in guard,
    # since the freshly written file lacks is_allowed_in_pytest).
    settings = tomllib.loads((project_config_dir / "settings.toml").read_text())
    assert settings["commands"]["create"]["provider"] == "modal", settings


@pytest.mark.release
def test_config_set_rejects_unknown_field(e2e: E2eSession, project_config_dir: Path) -> None:
    # Unhappy path for the same tutorial block: `config set` validates the
    # resulting config before writing, so an unknown top-level field is rejected
    # and nothing is persisted.
    e2e.write_tutorial_block("""
        # set a config value (at the default scope)
        mngr config set commands.create.provider modal
    """)
    set_result = e2e.run(
        "mngr config set not_a_real_config_field modal",
        comment="set an unknown config field (should be rejected)",
    )
    expect(set_result).to_fail()
    expect(set_result.stderr).to_contain("Invalid configuration")
    # The rejected key must not have been written to the project config file.
    settings_path = project_config_dir / "settings.toml"
    if settings_path.exists():
        expect(settings_path.read_text()).not_to_contain("not_a_real_config_field")


@pytest.mark.release
@pytest.mark.timeout(60)
def test_config_set_scope(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # set a config value at a specific scope
        mngr config set headless true --scope user
    """)
    expect(e2e.run("mngr config set headless true --scope user", comment="set at a specific scope")).to_succeed()

    # The value must actually be persisted at the user scope.
    user_value = e2e.run("mngr config get headless --scope user", comment="read it back from the user scope")
    expect(user_value).to_succeed()
    expect(user_value.stdout.strip()).to_equal("true")

    # --scope user must write to the user scope only, not the default project
    # scope. The project scope file should not contain the key.
    project_value = e2e.run(
        "mngr config get headless --scope project", comment="confirm it was not written to the project scope"
    )
    expect(project_value).to_fail()
    expect(project_value.stdout + project_value.stderr).to_match(r"(?i)not found")

    # The path command should point at the user scope's config file, which must
    # now exist and contain the key we set on disk.
    path_result = e2e.run("mngr config path --scope user", comment="locate the user scope config file")
    expect(path_result).to_succeed()
    cat_result = e2e.run(f"cat {path_result.stdout.strip()}", comment="inspect the user scope config file on disk")
    expect(cat_result).to_succeed()
    expect(cat_result.stdout).to_match(r"headless\s*=\s*true")


@pytest.mark.release
def test_config_set_scope_invalid_scope(e2e: E2eSession) -> None:
    # Unhappy path for the same tutorial block: --scope only accepts user,
    # project, or local, so an unrecognized scope must be rejected outright.
    e2e.write_tutorial_block("""
        # set a config value at a specific scope
        mngr config set headless true --scope user
    """)
    result = e2e.run(
        "mngr config set headless true --scope bogus", comment="reject an unrecognized scope"
    )
    expect(result).to_fail()
    expect(result.stdout + result.stderr).to_match(r"(?i)invalid|not one of|scope")


@pytest.mark.release
@pytest.mark.timeout(60)
def test_config_unset(e2e: E2eSession, project_config_dir: Path) -> None:
    e2e.write_tutorial_block("""
        # unset a config value
        mngr config unset commands.create.provider
    """)
    # `config unset` defaults to the project scope, so the key must already
    # exist there. Pre-seed the project settings file with two opt-ins:
    #   - is_allowed_in_pytest: mirrors the local-scope file the e2e fixture
    #     writes, so the `config set` precondition (and every later command
    #     that loads this file) passes the pytest guard.
    #   - allow_settings_key_assignment_narrowing: the e2e fixture also writes a
    #     [commands.create] table at local scope, so adding one at project scope
    #     would otherwise trip the settings-narrowing guard when the merged
    #     config is loaded.
    (project_config_dir / "settings.toml").write_text(
        "is_allowed_in_pytest = true\nallow_settings_key_assignment_narrowing = true\n"
    )
    expect(
        e2e.run("mngr config set commands.create.provider modal", comment="precondition: set the value")
    ).to_succeed()
    before = e2e.run("mngr config get commands.create.provider --scope project")
    expect(before).to_succeed()
    expect(before.stdout).to_contain("modal")
    # unset a config value
    expect(e2e.run("mngr config unset commands.create.provider", comment="unset a config value")).to_succeed()
    # Verify the key is actually gone from the project scope.
    expect(e2e.run("mngr config get commands.create.provider --scope project")).to_fail()


@pytest.mark.release
def test_config_unset_missing_key(e2e: E2eSession) -> None:
    # Unhappy path for the same tutorial block: unsetting a key that is not set
    # at the (default) project scope fails cleanly with a "Key not found" error
    # rather than silently succeeding.
    e2e.write_tutorial_block("""
        # unset a config value
        mngr config unset commands.create.provider
    """)
    result = e2e.run("mngr config unset commands.create.provider", comment="unset a config value")
    expect(result).to_fail()
    expect(result.stderr).to_contain("Key not found")


@pytest.mark.release
def test_config_edit(e2e: E2eSession, tmp_path: Path, project_config_dir: Path) -> None:
    e2e.write_tutorial_block("""
        # open the config file in your editor
        mngr config edit
    """)
    # `mngr config edit` opens the config file in $EDITOR. To verify the full
    # round-trip without a human at the keyboard, point $EDITOR at a script that
    # appends a setting to the file it is handed, then confirm the change landed
    # on disk in the project-scope config file (the default scope for `edit`).
    fake_editor = tmp_path / "fake_editor.sh"
    fake_editor.write_text('#!/bin/sh\necho "headless = true" >> "$1"\n')
    fake_editor.chmod(0o755)

    # The project config file should not exist yet -- `edit` creates it on demand.
    project_config_path = project_config_dir / "settings.toml"
    assert not project_config_path.exists()

    result = e2e.run(f"EDITOR={fake_editor} mngr config edit", comment="open the config file in your editor")
    expect(result).to_succeed()
    # The command reports which file it is opening; that file is the one we edit.
    expect(result.stdout).to_contain("settings.toml")

    # The file was created and the edit made through the editor persisted to disk.
    assert project_config_path.exists(), "config edit should create the project config file"
    expect(project_config_path.read_text()).to_contain("headless = true")


@pytest.mark.release
def test_config_edit_editor_failure(e2e: E2eSession) -> None:
    # Shares the `mngr config edit` tutorial block, but exercises the unhappy
    # path: when the editor exits with an error, `mngr config edit` must
    # propagate that failure rather than silently reporting success.
    e2e.write_tutorial_block("""
        # open the config file in your editor
        mngr config edit
    """)
    # `false` exits non-zero immediately, standing in for an editor that failed.
    expect(e2e.run("EDITOR=false mngr config edit", comment="editor exits with an error")).to_fail()


@pytest.mark.release
@pytest.mark.timeout(60)
def test_config_edit_scope(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # open a specific scope's config file
        mngr config edit --scope project
    """)
    # Resolve where the project-scope config file lives so we can cross-check that
    # `config edit --scope project` targets exactly that path (and creates it).
    path_result = e2e.run(
        "mngr config path --scope project",
        comment="resolve the project scope config file path",
    )
    expect(path_result).to_succeed()
    project_config_path = path_result.stdout.strip()
    # The file should not exist yet; creating it is `config edit`'s responsibility.
    expect(e2e.run(f'test -e "{project_config_path}"')).to_fail()

    # `mngr config edit` spawns $EDITOR; force it to /bin/true so the command
    # returns immediately with success.
    edit_result = e2e.run(
        "EDITOR=/bin/true mngr config edit --scope project",
        comment="open a specific scope's config file",
    )
    expect(edit_result).to_succeed()
    # It must open the project-scope file specifically, not another scope's file.
    expect(edit_result.stdout).to_contain(project_config_path)
    # And the file must now exist on disk (config edit creates it if missing).
    expect(e2e.run(f'test -f "{project_config_path}"')).to_succeed()


@pytest.mark.release
def test_config_edit_scope_rejects_invalid_scope(e2e: E2eSession) -> None:
    # Shares the `config edit --scope` tutorial block, but covers the unhappy
    # path: an unsupported scope value must be rejected rather than accepted.
    e2e.write_tutorial_block("""
        # open a specific scope's config file
        mngr config edit --scope project
    """)
    result = e2e.run(
        "EDITOR=/bin/true mngr config edit --scope bogus",
        comment="reject an unsupported scope value",
    )
    expect(result).to_fail()
    expect(result.stderr).to_contain("bogus")


@pytest.mark.release
def test_config_path(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # show the path to the config file
        mngr config path
    """)
    result = e2e.run("mngr config path", comment="show the path to the config file")
    expect(result).to_succeed()
    # With no --scope, the command lists the path for every scope. Verify all
    # three scopes are reported, each pointing at a TOML settings file with an
    # existence status, rather than just checking the exit code.
    expect(result.stdout).to_contain("user:")
    expect(result.stdout).to_contain("project:")
    expect(result.stdout).to_contain("local:")
    expect(result.stdout).to_match(r"settings\.toml \((exists|not found)\)")
    expect(result.stdout).to_match(r"settings\.local\.toml \((exists|not found)\)")


@pytest.mark.release
def test_config_path_invalid_scope(e2e: E2eSession) -> None:
    # Unhappy path for the same tutorial block: an unsupported --scope value is
    # rejected by click's choice validation rather than silently accepted.
    e2e.write_tutorial_block("""
        # show the path to the config file
        mngr config path
    """)
    result = e2e.run("mngr config path --scope bogus", comment="reject an invalid config scope")
    expect(result).to_fail()
    expect(result.stderr).to_contain("bogus")


@pytest.mark.release
def test_config_path_scope(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # show the path to a specific scope's config file
        mngr config path --scope user
    """)
    result = e2e.run(
        "mngr config path --scope user", comment="show the path to a specific scope's config file"
    )
    expect(result).to_succeed()
    # The user scope config lives at ~/.mngr/profiles/<profile_id>/settings.toml.
    printed_path = result.stdout.strip()
    expect(printed_path).to_match(r"profiles/[^/]+/settings\.toml$")
    # The reported path must be a real, absolute file -- and specifically the
    # user-scope config that the fixture provisioned (it seeds settings.toml
    # with `is_allowed_in_pytest = true`). Reading it back confirms the command
    # points at the actual config file mngr loads, not just a plausible string.
    config_file = Path(printed_path)
    assert config_file.is_absolute(), f"expected an absolute path, got {printed_path!r}"
    assert config_file.is_file(), f"user config file should exist at {printed_path!r}"
    expect(config_file.read_text()).to_contain("is_allowed_in_pytest")


@pytest.mark.release
def test_config_path_scope_invalid_scope(e2e: E2eSession) -> None:
    # Same tutorial block as test_config_path_scope -- this covers the unhappy
    # path where an unknown --scope value is rejected.
    e2e.write_tutorial_block("""
        # show the path to a specific scope's config file
        mngr config path --scope user
    """)
    result = e2e.run("mngr config path --scope bogus", comment="reject an unknown config scope")
    expect(result).to_fail()
