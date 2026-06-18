"""Tests for the CONFIGURATION tutorial section.

Each test corresponds 1:1 to a tutorial script block.
"""

import json
import re
import shlex
from pathlib import Path

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


# Two mngr subprocess invocations (set + list) exceed the default 10s
# func-only timeout, so allow more headroom.
@pytest.mark.timeout(60)
@pytest.mark.release
def test_config_list(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list all configuration values
        mngr config list
    """)
    # Persist a known value first so we can verify that `list` actually reflects
    # what's in the config files. The default (non-`--all`) view is filtered
    # down to keys explicitly written to a scope, so a value we just set must
    # show up. `--scope local` writes to the same settings.local.toml the test
    # fixture already opted into pytest, keeping the run valid. `headless` is a
    # top-level scalar that round-trips cleanly through set/list/get.
    expect(
        e2e.run(
            "mngr config set headless true --scope local",
            comment="persist a known config value for verification",
        )
    ).to_succeed()
    result = e2e.run("mngr config list", comment="list all configuration values")
    expect(result).to_succeed()
    # Human output is headed by the merged-scope banner...
    expect(result.stdout).to_contain("Merged configuration (all scopes):")
    # ...and reflects the value we just persisted.
    expect(result.stdout).to_match(r"headless\s*=\s*true")


@pytest.mark.timeout(60)
@pytest.mark.release
def test_config_list_json(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list all configuration values
        mngr config list
    """)
    # Same block as test_config_list, exercising the machine-readable output
    # branch: `--format json` must emit a parseable document that carries the
    # persisted value under a top-level "config" object.
    expect(
        e2e.run(
            "mngr config set headless true --scope local",
            comment="persist a known config value for verification",
        )
    ).to_succeed()
    result = e2e.run("mngr config list --format json", comment="list all configuration values as JSON")
    expect(result).to_succeed()
    payload = json.loads(result.stdout)
    assert payload["config"]["headless"] is True, payload


# Runs three sequential `mngr` subprocesses; each cold-start costs several seconds,
# so the cumulative runtime exceeds the default 10s per-test pytest-timeout.
@pytest.mark.release
@pytest.mark.timeout(60)
def test_config_list_scope(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list configuration at a specific scope (user, project, or local)
        mngr config list --scope user
        mngr config list --scope project
        mngr config list --scope local
    """)
    # Each invocation must succeed and report the scope it actually read from, so a
    # passing test confirms `--scope` selects the right file rather than silently
    # falling back to the merged view. The e2e fixture writes the connect_command
    # value "mngr-e2e-connect" only into the *local* scope file, so it serves as a
    # marker that the scope filter is real: it must appear under `--scope local`
    # and must NOT bleed into the user/project views (which would mean the command
    # silently merged scopes instead of reading just the requested file).
    user_result = e2e.run("mngr config list --scope user", comment="list user scope")
    expect(user_result).to_succeed()
    expect(user_result.stdout).to_contain("Config from user")
    expect(user_result.stdout).not_to_contain("mngr-e2e-connect")

    project_result = e2e.run("mngr config list --scope project", comment="list project scope")
    expect(project_result).to_succeed()
    expect(project_result.stdout).to_contain("Config from project")
    expect(project_result.stdout).not_to_contain("mngr-e2e-connect")

    local_result = e2e.run("mngr config list --scope local", comment="list local scope")
    expect(local_result).to_succeed()
    expect(local_result.stdout).to_contain("Config from local")
    # The local scope is the only one carrying the fixture's connect_command, so a
    # correct scope filter surfaces it here.
    expect(local_result.stdout).to_contain("mngr-e2e-connect")


@pytest.mark.release
# Two mngr subprocesses (set + get) exceed the default 10s per-test timeout, so
# raise it as other multi-command e2e tests do.
@pytest.mark.timeout(60)
def test_config_get(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # get a specific config value
        mngr config get commands.create.provider
    """)
    # `config get` only returns a value for a key that is actually set, so first
    # establish the value, then read it back at the same scope. We use local
    # scope because the e2e fixture already writes `commands.create.connect_command`
    # there; setting `commands.create.provider` in the same layer keeps both keys
    # in one `[commands.create]` table. (Setting it at a different scope would
    # trip the settings-narrowing guard, since both keys live under the merged
    # `commands.create.defaults` dict.) The local config file also already opts
    # into pytest runs, so the read does not fail for an unrelated reason.
    expect(
        e2e.run("mngr config set commands.create.provider modal --scope local", comment="set the value first")
    ).to_succeed()
    result = e2e.run("mngr config get commands.create.provider --scope local", comment="get a specific config value")
    expect(result).to_succeed()
    # Verify the value that comes back is exactly what we set, not just that the
    # command exited zero.
    expect(result.stdout.strip()).to_equal("modal")


@pytest.mark.release
def test_config_get_missing_key(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # get a specific config value
        mngr config get commands.create.provider
    """)
    # Unhappy path for the same tutorial command: reading a key that has not
    # been set fails with a non-zero exit and a clear "Key not found" message.
    result = e2e.run("mngr config get commands.create.provider", comment="get a specific config value")
    expect(result).to_fail()
    expect(result.stderr).to_contain("Key not found: commands.create.provider")


@pytest.mark.release
@pytest.mark.timeout(60)
def test_config_set(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # set a config value (at the default scope)
        mngr config set commands.create.provider modal
    """)
    result = e2e.run("mngr config set commands.create.provider modal", comment="set a config value")
    expect(result).to_succeed()
    # The command echoes the key and value it wrote, and the scope (default is
    # project).
    expect(result.stdout).to_contain("commands.create.provider")
    expect(result.stdout).to_contain("modal")
    expect(result.stdout).to_contain("project")
    # Verify the value actually landed in the project settings file. The default
    # scope writes to .<root>/settings.toml; read it back the way a human would
    # when debugging rather than via `mngr config get`. (A follow-up mngr command
    # would reload this freshly-written file, which -- unlike the fixture's
    # settings.local.toml -- does not opt into pytest, and would be rejected.)
    settings = e2e.run("cat .$MNGR_ROOT_NAME/settings.toml", comment="inspect the written project settings file")
    expect(settings).to_succeed()
    expect(settings.stdout).to_contain('provider = "modal"')


@pytest.mark.release
@pytest.mark.timeout(60)
def test_config_set_unknown_key_fails(e2e: E2eSession) -> None:
    # Shares the CONFIGURATION `mngr config set` block; this is the unhappy path
    # where the value is rejected because the key is not a known config field.
    e2e.write_tutorial_block("""
        # set a config value (at the default scope)
        mngr config set commands.create.provider modal
    """)
    result = e2e.run("mngr config set totally_unknown_key value", comment="setting an unknown key is rejected")
    expect(result).to_fail()
    expect(result.stderr).to_contain("Unknown configuration fields")
    # The rejected write must not be persisted. The e2e fixture pre-seeds the
    # project settings file with the pytest opt-in key, so the file exists; assert
    # the rejected key was never written into it rather than that the file is absent.
    settings = e2e.run("cat .$MNGR_ROOT_NAME/settings.toml", comment="verify the invalid value was not written")
    expect(settings).to_succeed()
    expect(settings.stdout).not_to_contain("totally_unknown_key")


# Runs several mngr subprocesses (set plus read-backs), so it needs more than the
# default 10s per-test timeout (each mngr invocation costs a few seconds to start up).
@pytest.mark.timeout(60)
@pytest.mark.release
def test_config_set_scope(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # set a config value at a specific scope
        mngr config set headless true --scope user
    """)
    set_result = e2e.run("mngr config set headless true --scope user", comment="set at a specific scope")
    expect(set_result).to_succeed()
    # The set should report that it wrote to the user scope (not the default project scope).
    expect(set_result.stdout).to_contain("user")

    # Verify the value was actually persisted at the user scope by reading it back from that scope.
    user_get = e2e.run("mngr config get headless --scope user", comment="read the value back from the user scope")
    expect(user_get).to_succeed()
    expect(user_get.stdout.strip()).to_equal("true")

    # Verify scope isolation: the value must not have leaked into the project scope, which was
    # never written to. Reading a missing key from a specific scope fails with a non-zero exit code.
    project_get = e2e.run(
        "mngr config get headless --scope project", comment="confirm the value is not set at the project scope"
    )
    expect(project_get).to_fail()


# Runs two mngr subprocesses (the rejected set plus a read-back), so it needs more than
# the default 10s per-test timeout.
@pytest.mark.timeout(60)
@pytest.mark.release
def test_config_set_invalid_scope(e2e: E2eSession) -> None:
    # Unhappy path for the same tutorial block: an unrecognized --scope value is rejected.
    e2e.write_tutorial_block("""
        # set a config value at a specific scope
        mngr config set headless true --scope user
    """)
    result = e2e.run("mngr config set headless true --scope bogus", comment="reject an invalid scope")
    expect(result).to_fail()
    # Verify the failure is specifically the invalid-scope rejection, not some
    # unrelated error (e.g. a malformed config file): the message must name the
    # bad value and enumerate the valid scopes. Without this, any non-zero exit
    # would satisfy `to_fail()` and the test would pass for the wrong reason.
    expect(result.stderr).to_contain("'bogus' is not one of")
    expect(result.stderr).to_contain("'user'")
    expect(result.stderr).to_contain("'project'")
    expect(result.stderr).to_contain("'local'")
    # The value must not have been written to any real scope as a side effect of
    # the rejected command. Reading it back from the user scope fails because the
    # key was never set there -- and the "Key not found" message confirms the
    # read got far enough to actually look (rather than failing for an unrelated
    # reason, which is what a generic `to_fail()` would have masked).
    read_back = e2e.run("mngr config get headless --scope user", comment="confirm nothing was written")
    expect(read_back).to_fail()
    expect(read_back.stderr).to_contain("Key not found: headless")


@pytest.mark.release
@pytest.mark.timeout(60)
def test_config_unset(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # unset a config value
        mngr config unset commands.create.provider
    """)
    # `config unset` only succeeds for a key that is actually present in the
    # target scope (a missing key fails with "Key not found"), so first set the
    # value at the default (project) scope, then unset it the way the tutorial
    # shows -- with no `--scope`, which also resolves to the project scope so
    # both commands touch the same settings.toml.
    expect(e2e.run("mngr config set commands.create.provider modal", comment="set the value first")).to_succeed()
    # Confirm the value really landed in the project settings file before we
    # remove it, the way a human would when debugging.
    settings_before = e2e.run(
        "cat .$MNGR_ROOT_NAME/settings.toml", comment="confirm the value is present before unset"
    )
    expect(settings_before).to_succeed()
    expect(settings_before.stdout).to_contain('provider = "modal"')

    result = e2e.run("mngr config unset commands.create.provider", comment="unset a config value")
    expect(result).to_succeed()
    # The command reports which key it removed and from which scope.
    expect(result.stdout).to_contain("commands.create.provider")
    expect(result.stdout).to_contain("project")

    # Verify the key was actually removed from the project settings file, not
    # just that the command exited zero.
    settings_after = e2e.run("cat .$MNGR_ROOT_NAME/settings.toml", comment="verify the value was removed")
    expect(settings_after).to_succeed()
    expect(settings_after.stdout).not_to_contain('provider = "modal"')


@pytest.mark.release
@pytest.mark.timeout(60)
def test_config_unset_missing_key(e2e: E2eSession, project_config_dir: Path) -> None:
    # Unhappy path for the same tutorial block: unsetting a key that is not
    # present in the target scope fails with a clear "Key not found" error.
    e2e.write_tutorial_block("""
        # unset a config value
        mngr config unset commands.create.provider
    """)
    # Seed a project config that opts into pytest but does NOT define the key.
    settings_path = project_config_dir / "settings.toml"
    settings_path.write_text("is_allowed_in_pytest = true\n")

    result = e2e.run("mngr config unset commands.create.provider", comment="unset a config value")
    expect(result).to_fail()
    # The error must name the specific key that could not be found, not just a
    # generic failure (mirrors test_config_get_missing_key).
    expect(result.stderr).to_contain("Key not found: commands.create.provider")


@pytest.mark.release
# Runs two mngr subprocesses (config path + config edit); each cold start costs
# several seconds, so the cumulative runtime exceeds the default 10s func-only timeout.
@pytest.mark.timeout(60)
def test_config_edit(e2e: E2eSession, temp_git_repo: Path) -> None:
    e2e.write_tutorial_block("""
        # open the config file in your editor
        mngr config edit
    """)
    # `mngr config edit` opens the config file in $EDITOR. Rather than just
    # checking the command exits 0, drive it with a fake editor that records the
    # path it was handed and stamps a marker into that file. This lets us verify
    # the command actually opened the real project config file.
    recorded_path = temp_git_repo / "editor_target.txt"
    fake_editor = temp_git_repo / "fake_editor.sh"
    fake_editor.write_text(
        "#!/bin/sh\n"
        # Record the path the editor was invoked on, then append a marker so we
        # can confirm afterwards that this is the real config file.
        f'printf "%s" "$1" > "{recorded_path}"\n'
        'printf "\\n# edited by fake editor\\n" >> "$1"\n'
    )
    fake_editor.chmod(0o755)

    # Resolve the project-scope config path (the default scope for `config edit`).
    # The e2e fixture seeds this file (settings.toml) with the pytest opt-in, so
    # it already exists; the marker we stamp in below is what proves the editor
    # was handed this exact file.
    path_result = e2e.run("mngr config path --scope project --format json", comment="resolve the project config path")
    expect(path_result).to_succeed()
    config_path = Path(json.loads(path_result.stdout)["path"])
    assert config_path.exists(), f"expected the fixture to have seeded {config_path}"
    assert "# edited by fake editor" not in config_path.read_text(), "marker must not be present before editing"

    # open the config file in your editor
    expect(
        e2e.run(f"EDITOR={fake_editor} mngr config edit", comment="open the config file in your editor")
    ).to_succeed()

    # The editor was invoked on exactly the project config path, and our marker
    # was persisted into that file -- proving `config edit` opened the real file.
    assert recorded_path.exists(), "fake editor was never invoked"
    expect(recorded_path.read_text().strip()).to_equal(str(config_path))
    expect(config_path.read_text()).to_contain("# edited by fake editor")


@pytest.mark.release
def test_config_edit_editor_failure(e2e: E2eSession) -> None:
    # Shares the `mngr config edit` tutorial block, but covers the unhappy path:
    # when the editor exits non-zero, the command must propagate the failure.
    e2e.write_tutorial_block("""
        # open the config file in your editor
        mngr config edit
    """)
    # /bin/false exits 1, standing in for an editor that the user aborted or
    # that crashed.
    result = e2e.run("EDITOR=/bin/false mngr config edit", comment="editor exits with an error")
    # The command must not swallow the editor's failure: it propagates the
    # editor's exact exit code (1 from /bin/false), not just some non-zero code.
    expect(result).to_have_exit_code(1)
    expect(result.stderr).to_contain("Editor exited with error")


@pytest.mark.release
def test_config_edit_scope(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # open a specific scope's config file
        mngr config edit --scope project
    """)
    # Resolve the project-scoped config path up front so we can verify that
    # `config edit --scope project` actually targets the project file (and not
    # the user or local scope).
    path_result = e2e.run("mngr config path --scope project", comment="resolve the project-scoped config path")
    expect(path_result).to_succeed()
    project_config_path = path_result.stdout.strip()
    assert project_config_path, "expected `config path --scope project` to print a path"

    # `mngr config edit` spawns $EDITOR; force it to /bin/true so the command
    # returns immediately with success instead of blocking on a real editor.
    result = e2e.run(
        "EDITOR=/bin/true mngr config edit --scope project",
        comment="open a specific scope's config file",
    )
    expect(result).to_succeed()
    # The command announces which file it opened; it must be the project file,
    # confirming the --scope flag selected the right scope.
    expect(result.stdout).to_contain("Opening")
    expect(result.stdout).to_contain(project_config_path)

    # Editing a not-yet-existing scope file creates it from a template, so the
    # project config file should now exist on disk with the template header.
    created = e2e.run(f"cat {shlex.quote(project_config_path)}", comment="inspect the created project config file")
    expect(created).to_succeed()
    expect(created.stdout).to_contain("mngr configuration file")


@pytest.mark.release
def test_config_edit_scope_missing_editor(e2e: E2eSession) -> None:
    """Unhappy path for `config edit --scope project`: a missing editor fails cleanly."""
    e2e.write_tutorial_block("""
        # open a specific scope's config file
        mngr config edit --scope project
    """)
    # Point both $VISUAL and $EDITOR at a program that does not exist. The
    # command should fail with a non-zero exit code and a helpful error message
    # rather than hanging or crashing with a traceback.
    result = e2e.run(
        "VISUAL= EDITOR=/nonexistent/definitely-not-a-real-editor mngr config edit --scope project",
        comment="config edit with a missing editor",
    )
    expect(result).to_fail()
    combined_output = result.stdout + result.stderr
    expect(combined_output).to_contain("Editor not found")
    # The error names the missing editor and points the user at the env vars to
    # set, so the failure is actionable rather than a bare traceback.
    expect(combined_output).to_contain("/nonexistent/definitely-not-a-real-editor")
    expect(combined_output).to_contain("$EDITOR")


@pytest.mark.release
def test_config_path(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # show the path to the config file
        mngr config path
    """)
    result = e2e.run("mngr config path", comment="show the path to the config file")
    expect(result).to_succeed()
    # Without a scope, the command resolves the config file for every scope and
    # annotates each with whether it currently exists on disk.
    expect(result.stdout).to_contain("user:")
    expect(result.stdout).to_contain("project:")
    expect(result.stdout).to_contain("local:")
    # The user/local scopes resolve to settings.toml / settings.local.toml.
    expect(result.stdout).to_match(r"user:\s+\S+settings\.toml ")
    expect(result.stdout).to_match(r"local:\s+\S+settings\.local\.toml ")
    # The (exists)/(not found) annotation must reflect reality: parse each line
    # and confirm the on-disk state matches what was reported, the way a human
    # would double-check by looking at the actual files.
    line_pattern = re.compile(r"^\s*(user|project|local):\s+(\S+)\s+\((exists|not found)\)\s*$")
    checked_scopes = set()
    for line in result.stdout.splitlines():
        match = line_pattern.match(line)
        if match is None:
            continue
        scope, path, status = match.groups()
        checked_scopes.add(scope)
        existence_check = e2e.run(f"test -e {shlex.quote(path)}", comment=f"verify {scope} config existence")
        if status == "exists":
            expect(existence_check).to_succeed()
        else:
            expect(existence_check).to_fail()
    # All three scopes must have produced a parseable, verified path line.
    assert checked_scopes == {"user", "project", "local"}, (
        f"expected all scopes to be reported with a path, got {checked_scopes}\n{result.stdout}"
    )


# Runs several mngr subprocesses (path, set, cat), so it needs more than the
# default 10s per-test budget.
@pytest.mark.timeout(60)
@pytest.mark.release
def test_config_path_scope(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # show the path to a specific scope's config file
        mngr config path --scope user
    """)
    result = e2e.run("mngr config path --scope user", comment="show the path to a specific scope's config file")
    expect(result).to_succeed()
    # The command prints exactly the user-scope config path: an absolute path to
    # the profile directory's settings.toml.
    config_path = result.stdout.strip()
    expect(config_path).to_match(r"^/.*profiles/.*/settings\.toml$")
    # Verify the reported path really is the file that user-scope writes land in:
    # set a value at user scope and confirm it appears in exactly that file.
    expect(
        e2e.run("mngr config set headless true --scope user", comment="write a value at the user scope")
    ).to_succeed()
    written = e2e.run(f"cat {config_path}", comment="the path points to the file that user-scope writes land in")
    expect(written).to_succeed()
    expect(written.stdout).to_contain("headless")


@pytest.mark.release
def test_config_path_invalid_scope(e2e: E2eSession) -> None:
    # Shares the `mngr config path --scope ...` tutorial block: exercises the
    # unhappy path where an unsupported scope is rejected by --scope validation.
    e2e.write_tutorial_block("""
        # show the path to a specific scope's config file
        mngr config path --scope user
    """)
    result = e2e.run("mngr config path --scope bogus", comment="an unsupported scope is rejected")
    expect(result).to_fail()
    combined = result.stdout + result.stderr
    # Click reports the invalid choice and lists the supported scopes.
    expect(combined).to_contain("Invalid value")
    expect(combined).to_contain("--scope")
    expect(combined).to_contain("user")
