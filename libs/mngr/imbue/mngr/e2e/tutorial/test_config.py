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
    # The whole of stdout must be a single parseable JSON document -- no human
    # banner ("Merged configuration...") may leak into the machine-readable
    # output. json.loads on the full stdout fails loudly if anything else is
    # printed alongside the object.
    payload = json.loads(result.stdout)
    # The merged (unscoped) list reports only the config; the scope/path keys are
    # reserved for the `--scope` variant, so their absence here confirms we took
    # the merged branch rather than silently reading a single file.
    assert set(payload) == {"config"}, payload
    config = payload["config"]
    assert isinstance(config, dict), payload
    # The value we just persisted round-trips through the JSON view as a real
    # boolean, not the string "true".
    assert config["headless"] is True, payload


@pytest.mark.timeout(60)
@pytest.mark.release
def test_config_list_json_scope(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list all configuration values
        mngr config list
    """)
    # Same block as test_config_list, but exercises the JSON output of the
    # `--scope` branch: unlike the merged view, a scoped JSON list must annotate
    # the document with which scope it read and the file path it came from. We
    # write to and read back the local scope (the fixture already opted into
    # pytest there).
    expect(
        e2e.run(
            "mngr config set headless true --scope local",
            comment="persist a known config value for verification",
        )
    ).to_succeed()
    result = e2e.run(
        "mngr config list --scope local --format json", comment="list local-scope configuration as JSON"
    )
    expect(result).to_succeed()
    payload = json.loads(result.stdout)
    # The scoped JSON view carries scope/path metadata in addition to the config.
    assert payload["scope"] == "local", payload
    assert payload["path"].endswith("settings.local.toml"), payload
    # The reported path must be the file actually read, and the persisted value
    # must appear under it.
    assert payload["config"]["headless"] is True, payload
    on_disk = e2e.run(f"cat {shlex.quote(payload['path'])}", comment="inspect the file the JSON pointed at")
    expect(on_disk).to_succeed()
    expect(on_disk.stdout).to_contain("headless")


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
    # The user/project scopes read from settings.toml; only the local scope reads
    # settings.local.toml. Asserting the reported filename confirms `--scope`
    # selected the right physical file, not just the right banner label.
    expect(user_result.stdout).to_match(r"Config from user \(\S+/settings\.toml\)")
    expect(user_result.stdout).not_to_contain("mngr-e2e-connect")

    project_result = e2e.run("mngr config list --scope project", comment="list project scope")
    expect(project_result).to_succeed()
    expect(project_result.stdout).to_contain("Config from project")
    expect(project_result.stdout).to_match(r"Config from project \(\S+/settings\.toml\)")
    expect(project_result.stdout).not_to_contain("mngr-e2e-connect")

    local_result = e2e.run("mngr config list --scope local", comment="list local scope")
    expect(local_result).to_succeed()
    expect(local_result.stdout).to_contain("Config from local")
    # The local scope is the distinct file (settings.local.toml), not the
    # settings.toml the other two scopes read from.
    expect(local_result.stdout).to_match(r"Config from local \(\S+/settings\.local\.toml\)")
    # The local scope is the only one carrying the fixture's connect_command, so a
    # correct scope filter surfaces it here.
    expect(local_result.stdout).to_contain("mngr-e2e-connect")


@pytest.mark.release
def test_config_list_invalid_scope(e2e: E2eSession) -> None:
    # Shares the `mngr config list --scope ...` tutorial block: exercises the
    # unhappy path where an unsupported scope is rejected by --scope validation
    # (mirrors test_config_path_invalid_scope for the `config path` command).
    e2e.write_tutorial_block("""
        # list configuration at a specific scope (user, project, or local)
        mngr config list --scope user
        mngr config list --scope project
        mngr config list --scope local
    """)
    result = e2e.run("mngr config list --scope bogus", comment="an unsupported scope is rejected")
    expect(result).to_fail()
    combined = result.stdout + result.stderr
    # Click reports the invalid choice and lists the supported scopes; without
    # this the test would pass for any non-zero exit (e.g. a malformed config).
    expect(combined).to_contain("Invalid value")
    expect(combined).to_contain("--scope")
    expect(combined).to_contain("user")
    # The rejected scope must not produce a config listing: the merged-view banner
    # only appears when no (valid) scope filter is in play, so its absence
    # confirms the command bailed out at argument validation rather than falling
    # back to the merged view.
    expect(combined).not_to_contain("Merged configuration (all scopes):")


@pytest.mark.release
# Three mngr subprocesses (set + scoped get + merged get) exceed the default 10s
# per-test timeout, so raise it as other multi-command e2e tests do.
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

    # The scoped read above hits the raw TOML, where the key lives literally as
    # written (`[commands.create] provider`). The default (merged) view is a typed
    # model, and command-parameter defaults are nested one level deeper under the
    # command's `defaults` map. Reading the value back from the merged view (no
    # --scope) therefore requires the resolved path `...defaults.provider`; this
    # exercises the merged read branch (which the scoped read above never reaches)
    # and confirms the value we set surfaces through the merge.
    merged_result = e2e.run(
        "mngr config get commands.create.defaults.provider", comment="read the same value from the merged view"
    )
    expect(merged_result).to_succeed()
    expect(merged_result.stdout.strip()).to_equal("modal")


@pytest.mark.release
# A single `mngr config get` cold-start invocation can exceed the default 10s
# func-only pytest-timeout, so raise it as the other mngr-subprocess tests do.
@pytest.mark.timeout(60)
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
def test_config_set_overwrites_existing_value(e2e: E2eSession) -> None:
    # Shares the CONFIGURATION `mngr config set` block; this covers the common
    # real-world case of running `set` a second time on a key that already has a
    # value. The write must replace the value in place rather than appending a
    # duplicate or leaving the old value behind.
    e2e.write_tutorial_block("""
        # set a config value (at the default scope)
        mngr config set commands.create.provider modal
    """)
    # Use `--scope local` (rather than the block's default project scope) because
    # this test runs two consecutive `mngr` commands: the e2e fixture seeds the
    # local settings.local.toml with the pytest opt-in and an
    # `allow_settings_key_assignment_narrowing` flag, so the second command can
    # reload it, whereas a freshly-written project settings.toml carries neither
    # and would be rejected by the pytest config guard. The local file already
    # holds a `[commands.create]` table (with connect_command), so setting
    # `commands.create.provider` there mirrors what `test_config_get` does.
    expect(
        e2e.run("mngr config set commands.create.provider modal --scope local", comment="set a config value")
    ).to_succeed()
    # Re-set the same key to a different value at the same scope.
    result = e2e.run(
        "mngr config set commands.create.provider docker --scope local",
        comment="overwrite the existing config value",
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("commands.create.provider")
    expect(result.stdout).to_contain("docker")

    # The settings file must reflect the new value and must no longer carry the
    # old one -- the write updates in place rather than stacking a second entry.
    settings = e2e.run(
        "cat .$MNGR_ROOT_NAME/settings.local.toml", comment="inspect the written local settings file"
    )
    expect(settings).to_succeed()
    expect(settings.stdout).to_contain('provider = "docker"')
    expect(settings.stdout).not_to_contain('provider = "modal"')
    # Only one assignment of the key should exist -- a second occurrence would mean
    # the overwrite duplicated the key instead of replacing it.
    assert settings.stdout.count("provider =") == 1, settings.stdout


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
    # The error must name the specific offending key, not just report a generic
    # "Unknown configuration fields" failure -- otherwise the test would pass for
    # an unrelated rejection (mirrors test_config_get_missing_key).
    expect(result.stderr).to_contain("Unknown configuration fields")
    expect(result.stderr).to_contain("totally_unknown_key")
    # The rejected write must not be persisted. The e2e fixture deliberately
    # leaves the project-scope settings.toml unseeded (see conftest), so this is
    # genuine first-use behavior: `config set` validates the key before writing,
    # and a rejected key must not create the file at all. Read it back the way a
    # human would when debugging -- emitting nothing (and exiting 0) when the
    # file is absent -- and assert the rejected key never landed on disk, whether
    # or not the file exists.
    settings = e2e.run(
        "cat .$MNGR_ROOT_NAME/settings.toml 2>/dev/null || true",
        comment="verify the invalid value was not written",
    )
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
    # The set echoes the key and value it wrote, and reports the user scope (not the
    # default project scope), so a passing assertion confirms `--scope` was honored
    # rather than the write silently landing in the default scope.
    expect(set_result.stdout).to_contain("headless")
    expect(set_result.stdout).to_contain("true")
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
    # Click rejects an invalid `--scope` choice as a usage error, which exits with
    # code 2 (distinct from the exit code 1 a runtime/application error would use).
    # Pinning the exact code confirms the rejection happened at argument validation
    # rather than somewhere deeper for an unrelated reason.
    expect(result).to_have_exit_code(2)
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
def test_config_unset(e2e: E2eSession, project_config_dir: Path) -> None:
    e2e.write_tutorial_block("""
        # unset a config value
        mngr config unset commands.create.provider
    """)
    # `config unset` only succeeds for a key that is actually present in the
    # target scope (a missing key fails with "Key not found"), so first set the
    # value at the default (project) scope, then unset it the way the tutorial
    # shows -- with no `--scope`, which also resolves to the project scope so
    # both commands touch the same settings.toml.
    #
    # The e2e fixture deliberately does NOT seed the project settings.toml, so a
    # bare `mngr config set` would write a fresh file lacking the pytest opt-in.
    # Unlike `test_config_set` (which reads back with `cat`), this test runs a
    # follow-up `mngr config unset` that reloads the merged config -- and that
    # reload would reject the freshly-written project file for not setting
    # `is_allowed_in_pytest = true`. Pre-seed the file with the opt-in so the
    # follow-up command is permitted; `config set` preserves it (tomlkit merges
    # into the existing document rather than overwriting it).
    settings_path = project_config_dir / "settings.toml"
    settings_path.write_text("is_allowed_in_pytest = true\n")

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
    original_contents = "is_allowed_in_pytest = true\n"
    settings_path.write_text(original_contents)

    result = e2e.run("mngr config unset commands.create.provider", comment="unset a config value")
    expect(result).to_fail()
    # The error must name the specific key that could not be found, not just a
    # generic failure (mirrors test_config_get_missing_key).
    expect(result.stderr).to_contain("Key not found: commands.create.provider")
    # Concrete effect: a failed unset must be a no-op. The existing config file
    # must be left byte-for-byte unchanged (not rewritten, truncated, or
    # corrupted) by the failed operation.
    assert settings_path.read_text() == original_contents


@pytest.mark.release
def test_config_unset_missing_file(e2e: E2eSession) -> None:
    # Second unhappy path for the same tutorial block: when the target scope has
    # no config file at all (here the project scope, whose settings.toml is
    # deliberately never seeded by the e2e fixture), unset reports the same
    # "Key not found" error rather than crashing on the missing file. This
    # exercises a distinct code path from test_config_unset_missing_key, which
    # seeds a file that merely lacks the key.
    e2e.write_tutorial_block("""
        # unset a config value
        mngr config unset commands.create.provider
    """)
    result = e2e.run("mngr config unset commands.create.provider", comment="unset a config value")
    expect(result).to_fail()
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
    # The e2e fixture deliberately does NOT seed this file (settings.toml), so it
    # does not yet exist -- this exercises the genuine first-use behavior where
    # `config edit` creates the file from a template before opening the editor.
    path_result = e2e.run("mngr config path --scope project --format json", comment="resolve the project config path")
    expect(path_result).to_succeed()
    config_path = Path(json.loads(path_result.stdout)["path"])
    assert not config_path.exists(), f"expected the project config file to not yet exist: {config_path}"

    # open the config file in your editor
    expect(
        e2e.run(f"EDITOR={fake_editor} mngr config edit", comment="open the config file in your editor")
    ).to_succeed()

    # The editor was invoked on exactly the project config path, and our marker
    # was persisted into that file -- proving `config edit` opened the real file.
    assert recorded_path.exists(), "fake editor was never invoked"
    expect(recorded_path.read_text().strip()).to_equal(str(config_path))
    edited_contents = config_path.read_text()
    expect(edited_contents).to_contain("# edited by fake editor")
    # The file was created from the template before the editor ran, so the
    # template header must also be present alongside our marker.
    expect(edited_contents).to_contain("mngr configuration file")


# A single `mngr config edit` cold-start subprocess can exceed the default 10s
# func-only timeout, so allow more headroom (matching test_config_edit above).
@pytest.mark.timeout(60)
@pytest.mark.release
def test_config_edit_editor_failure(e2e: E2eSession, temp_git_repo: Path) -> None:
    # Shares the `mngr config edit` tutorial block, but covers the unhappy path:
    # when the editor exits non-zero, the command must propagate the failure.
    e2e.write_tutorial_block("""
        # open the config file in your editor
        mngr config edit
    """)
    # A fake editor that exits with a distinctive code stands in for an editor
    # the user aborted or that crashed. We deliberately use 42 rather than
    # /bin/false's 1: exit code 1 collides with the generic abort code, so a
    # regression that coerced every editor failure to `ctx.exit(1)` would slip
    # past a `/bin/false` test. A non-1 code proves the command propagates the
    # editor's *exact* exit code.
    fake_editor = temp_git_repo / "failing_editor.sh"
    fake_editor.write_text("#!/bin/sh\nexit 42\n")
    fake_editor.chmod(0o755)

    result = e2e.run(f"EDITOR={fake_editor} mngr config edit", comment="editor exits with an error")
    # The command must not swallow the editor's failure: it propagates the
    # editor's exact exit code (42), not just some non-zero code.
    expect(result).to_have_exit_code(42)
    expect(result.stderr).to_contain("Editor exited with error")


@pytest.mark.release
# Runs three mngr subprocesses (config path + config edit + cat); each cold start
# costs several seconds, so the cumulative runtime exceeds the default 10s func-only
# timeout.
@pytest.mark.timeout(60)
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
# A single mngr subprocess, but its cold start alone approaches the default 10s
# func-only pytest-timeout, so allow the same headroom as the sibling config tests.
@pytest.mark.timeout(60)
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
    # The handled error must be the *only* thing the user sees: a clean message,
    # not a Python stack trace leaking from an uncaught FileNotFoundError.
    expect(combined_output).not_to_contain("Traceback (most recent call last)")


# Runs `mngr config path` (a multi-second cold start) followed by one `test -e`
# subprocess per scope to verify the reported existence annotations, so the
# cumulative runtime exceeds the default 10s func-only timeout.
@pytest.mark.timeout(60)
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
    # The file must carry the exact value we set, not merely the key name -- this
    # confirms the value round-tripped into the file `config path` pointed at.
    expect(written.stdout).to_match(r"headless\s*=\s*true")


@pytest.mark.release
# A single mngr subprocess cold start can exceed the default 10s per-test
# pytest-timeout, so raise it as the other mngr e2e tests do.
@pytest.mark.timeout(60)
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
    # Click reports the invalid choice, echoes the offending value, and lists
    # every supported scope so the user can correct the typo.
    expect(combined).to_contain("Invalid value")
    expect(combined).to_contain("--scope")
    expect(combined).to_contain("bogus")
    for scope in ("user", "project", "local"):
        expect(combined).to_contain(scope)
