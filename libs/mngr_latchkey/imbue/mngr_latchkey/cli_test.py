"""Unit tests for :mod:`imbue.mngr_latchkey.cli`.

Focused on the pure pieces: settings-precedence resolution, JSON output
shape from ``create-agent-env``, and the symlink side effect of
``link-permissions``. The end-to-end ``forward`` subcommand is too
heavy to drive from a unit test (it spawns ``mngr observe`` and the
shared gateway); we cover the underlying dispatch logic in
``discovery_stream_test.py`` instead.
"""

import contextlib
import hashlib
import json
import subprocess
import sys
from collections.abc import Iterator
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final
from uuid import uuid4

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import PluginConfig
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import PluginName
from imbue.mngr_latchkey.cli import ENV_LATCHKEY_BINARY
from imbue.mngr_latchkey.cli import ENV_LATCHKEY_DIRECTORY
from imbue.mngr_latchkey.cli import _DEFAULT_LATCHKEY_DIRECTORY
from imbue.mngr_latchkey.cli import _resolve_latchkey_settings
from imbue.mngr_latchkey.cli import latchkey
from imbue.mngr_latchkey.config import LatchkeyPluginConfig
from imbue.mngr_latchkey.core import LATCHKEY_BINARY
from imbue.mngr_latchkey.core import LATCHKEY_MIN_VERSION
from imbue.mngr_latchkey.store import LatchkeyForwardInfo
from imbue.mngr_latchkey.store import load_forward_info
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import plugin_data_dir
from imbue.mngr_latchkey.store import save_forward_info

# A version string the upstream ``Latchkey.initialize`` is happy with.
# Pinned to ``LATCHKEY_MIN_VERSION`` so the fake binary we drop on $PATH
# always satisfies the floor, even when the floor is bumped.
_FAKE_LATCHKEY_VERSION: Final[str] = LATCHKEY_MIN_VERSION

# Globally-unique deterministic host IDs (matches the convention in
# ``mngr_forward/testing.py`` so test output is stable). The 32-char
# hex constraint is enforced by ``HostId``.
_HOST_ID_ONE: Final[HostId] = HostId("host-" + "0" * 31 + "1")


# -- Fixtures ---------------------------------------------------------------


@pytest.fixture
def latchkey_root(tmp_path: Path) -> Path:
    """Per-test root for the plugin's data subtree."""
    root = tmp_path / "latchkey-data"
    root.mkdir()
    return root


@pytest.fixture
def fake_latchkey_binary(tmp_path: Path) -> Path:
    """Drop a ``latchkey`` shell script that satisfies the CLI's read-side calls.

    Implements only ``--version``, ``ensure-browser``, and
    ``gateway create-jwt``: enough for ``Latchkey.initialize`` plus
    ``prepare_agent_latchkey`` to succeed without touching a real
    ``latchkey`` binary. Mirrors the helper in ``core_test.py`` so
    these tests can run on machines where the real upstream CLI is
    unavailable.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    script_path = bin_dir / "latchkey"
    script_path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        'if sys.argv[1] == "--version":\n'
        f"    print({_FAKE_LATCHKEY_VERSION!r})\n"
        "    sys.exit(0)\n"
        'if sys.argv[1] == "ensure-browser":\n'
        "    sys.exit(0)\n"
        'if sys.argv[1:3] == ["gateway", "create-jwt"]:\n'
        "    args = [a for a in sys.argv[3:] if not a.startswith('--')]\n"
        "    print(f'fake-jwt-for:{args[0]}' if args else 'fake-jwt')\n"
        "    sys.exit(0)\n"
        "sys.exit(99)\n"
    )
    script_path.chmod(0o755)
    return script_path


@pytest.fixture
def clean_latchkey_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip ``MNGR_LATCHKEY_*`` env vars so tests opt into them explicitly."""
    monkeypatch.delenv(ENV_LATCHKEY_DIRECTORY, raising=False)
    monkeypatch.delenv(ENV_LATCHKEY_BINARY, raising=False)
    yield


def _make_ctx(plugins: dict[PluginName, LatchkeyPluginConfig] | None = None) -> MngrContext:
    """Build a minimal :class:`MngrContext` for the precedence-resolver tests.

    ``_resolve_latchkey_settings`` only reads ``ctx.config.plugins``, so
    the ``pm`` / ``profile_dir`` fields can be any well-typed placeholders.
    """
    # MngrConfig.plugins is typed as ``dict[PluginName, PluginConfig]``;
    # widening the local dict here keeps the type checker happy without
    # forcing every caller to widen their fixtures.
    plugins_widened: dict[PluginName, PluginConfig] = dict(plugins) if plugins else {}
    config = MngrConfig(plugins=plugins_widened)
    placeholder_profile_dir = Path(f"/tmp/mngr-latchkey-cli-test-{uuid4().hex}")
    return MngrContext(
        config=config,
        pm=pluggy.PluginManager("mngr"),
        profile_dir=placeholder_profile_dir,
    )


# -- _resolve_latchkey_settings ---------------------------------------------


def test_resolve_falls_back_to_built_in_defaults(clean_latchkey_env: None) -> None:
    del clean_latchkey_env
    ctx = _make_ctx()
    directory, binary = _resolve_latchkey_settings(ctx, cli_directory=None, cli_binary=None)
    assert directory == _DEFAULT_LATCHKEY_DIRECTORY.expanduser()
    assert binary == LATCHKEY_BINARY


def test_resolve_reads_settings_toml(clean_latchkey_env: None) -> None:
    del clean_latchkey_env
    ctx = _make_ctx(
        plugins={
            PluginName("latchkey"): LatchkeyPluginConfig(
                directory=Path("/from/settings"),
                latchkey_binary="/from/settings/bin",
            )
        }
    )
    directory, binary = _resolve_latchkey_settings(ctx, cli_directory=None, cli_binary=None)
    assert directory == Path("/from/settings")
    assert binary == "/from/settings/bin"


def test_resolve_env_overrides_settings(clean_latchkey_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    del clean_latchkey_env
    monkeypatch.setenv(ENV_LATCHKEY_DIRECTORY, "/from/env")
    monkeypatch.setenv(ENV_LATCHKEY_BINARY, "/from/env/bin")
    ctx = _make_ctx(
        plugins={
            PluginName("latchkey"): LatchkeyPluginConfig(
                directory=Path("/from/settings"),
                latchkey_binary="/from/settings/bin",
            )
        }
    )
    directory, binary = _resolve_latchkey_settings(ctx, cli_directory=None, cli_binary=None)
    assert directory == Path("/from/env")
    assert binary == "/from/env/bin"


def test_resolve_cli_overrides_env_and_settings(clean_latchkey_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    del clean_latchkey_env
    monkeypatch.setenv(ENV_LATCHKEY_DIRECTORY, "/from/env")
    monkeypatch.setenv(ENV_LATCHKEY_BINARY, "/from/env/bin")
    ctx = _make_ctx(
        plugins={
            PluginName("latchkey"): LatchkeyPluginConfig(
                directory=Path("/from/settings"),
                latchkey_binary="/from/settings/bin",
            )
        }
    )
    directory, binary = _resolve_latchkey_settings(
        ctx,
        cli_directory="/from/cli",
        cli_binary="/from/cli/bin",
    )
    assert directory == Path("/from/cli")
    assert binary == "/from/cli/bin"


def test_resolve_expands_user_in_settings(clean_latchkey_env: None) -> None:
    """Tilde paths from settings.toml are expanded before they're returned."""
    del clean_latchkey_env
    ctx = _make_ctx(plugins={PluginName("latchkey"): LatchkeyPluginConfig(directory=Path("~/lk-test"))})
    directory, _binary = _resolve_latchkey_settings(ctx, cli_directory=None, cli_binary=None)
    assert "~" not in str(directory)
    assert directory == Path("~/lk-test").expanduser()


# -- create-agent-env --------------------------------------------------------


def test_create_agent_env_emits_expected_json_shape(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    latchkey_root: Path,
    fake_latchkey_binary: Path,
    clean_latchkey_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """End-to-end happy path: ``create-agent-env`` prints the contracted JSON shape on stdout."""
    del clean_latchkey_env
    monkeypatch.setenv(ENV_LATCHKEY_DIRECTORY, str(latchkey_root))
    monkeypatch.setenv(ENV_LATCHKEY_BINARY, str(fake_latchkey_binary))
    monkeypatch.setenv("HOME", str(tmp_path))

    result = cli_runner.invoke(
        latchkey,
        ["create-agent-env"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload.keys()) == {"env", "opaque_permissions_path"}
    env = payload["env"]
    assert env["LATCHKEY_GATEWAY"] == "http://127.0.0.1:1989"
    # Secondary (per-VPS) gateway URL, on a distinct in-container port.
    assert env["LATCHKEY_GATEWAY_SECONDARY"] == "http://127.0.0.1:1990"
    assert env["LATCHKEY_DISABLE_COUNTING"] == "1"
    assert env["LATCHKEY_GATEWAY_PASSWORD"]
    assert env["LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE"].startswith("fake-jwt-for:")
    opaque = Path(payload["opaque_permissions_path"])
    assert opaque.is_file()
    assert opaque.parent == plugin_data_dir(latchkey_root) / "permissions"


def test_create_agent_env_exits_nonzero_when_binary_missing(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    latchkey_root: Path,
    clean_latchkey_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A missing latchkey binary surfaces as a non-zero exit; no JSON on stdout."""
    del clean_latchkey_env
    nonexistent = tmp_path / f"missing-{uuid4().hex}"
    monkeypatch.setenv(ENV_LATCHKEY_DIRECTORY, str(latchkey_root))
    monkeypatch.setenv(ENV_LATCHKEY_BINARY, str(nonexistent))
    monkeypatch.setenv("HOME", str(tmp_path))

    result = cli_runner.invoke(
        latchkey,
        ["create-agent-env"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code != 0
    assert "latchkey initialization failed" in result.output
    # Nothing should land on stdout under failure -- the JSON contract
    # is only honoured on the happy path.
    assert "LATCHKEY_GATEWAY" not in result.output


# -- admin-jwt --------------------------------------------------------------


def test_admin_jwt_prints_jwt_and_creates_admin_file(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    latchkey_root: Path,
    fake_latchkey_binary: Path,
    clean_latchkey_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``admin-jwt`` materializes the wildcard admin permissions file and prints the JWT."""
    del clean_latchkey_env
    monkeypatch.setenv(ENV_LATCHKEY_DIRECTORY, str(latchkey_root))
    monkeypatch.setenv(ENV_LATCHKEY_BINARY, str(fake_latchkey_binary))
    monkeypatch.setenv("HOME", str(tmp_path))

    result = cli_runner.invoke(
        latchkey,
        ["admin-jwt"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    output = result.output.strip()
    assert output.startswith("fake-jwt-for:")
    # The path embedded in the (fake) JWT must be the admin permissions
    # path under ``plugin_data_dir``.
    pdd = plugin_data_dir(latchkey_root)
    admin_path = pdd / "latchkey_admin_permissions.json"
    assert admin_path.is_file()
    assert output == f"fake-jwt-for:{admin_path}"
    on_disk = json.loads(admin_path.read_text())
    assert on_disk == {"rules": [{"any": ["any"]}]}


# -- gateway-info -----------------------------------------------------------


@contextlib.contextmanager
def _fake_running_supervisor() -> Iterator[int]:
    """Yield the PID of a sleeping subprocess whose cmdline passes the supervisor liveness check.

    The subprocess's argv is shaped like ``[python, -c, ..., "mngr",
    "latchkey", "forward"]`` so
    :func:`_cmdline_looks_like_mngr_latchkey_forward` accepts it.
    Terminated on context exit.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "import signal; signal.pause()", "mngr", "latchkey", "forward"],
        start_new_session=True,
    )
    try:
        yield proc.pid
    finally:
        proc.kill()
        proc.wait(timeout=5.0)


def _build_forward_info(*, pid: int, gateway_port: int | None) -> LatchkeyForwardInfo:
    return LatchkeyForwardInfo(
        pid=pid,
        started_at=datetime.now(timezone.utc),
        gateway_port=gateway_port,
    )


def test_gateway_info_prints_url_and_password_when_supervisor_record_is_ready(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    latchkey_root: Path,
    fake_latchkey_binary: Path,
    clean_latchkey_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With a live supervisor + complete record, the subcommand emits ``{url, password}``."""
    del clean_latchkey_env
    monkeypatch.setenv(ENV_LATCHKEY_DIRECTORY, str(latchkey_root))
    monkeypatch.setenv(ENV_LATCHKEY_BINARY, str(fake_latchkey_binary))
    monkeypatch.setenv("HOME", str(tmp_path))

    with _fake_running_supervisor() as pid:
        save_forward_info(plugin_data_dir(latchkey_root), _build_forward_info(pid=pid, gateway_port=32867))
        result = cli_runner.invoke(
            latchkey,
            ["gateway-info"],
            obj=plugin_manager,
            catch_exceptions=False,
        )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    expected_password = hashlib.sha256(b"fake-jwt-for:/__minds_gateway_password__/sentinel").hexdigest()
    assert payload == {
        "url": "http://127.0.0.1:32867",
        "password": expected_password,
    }


def test_gateway_info_exits_nonzero_when_no_supervisor_record(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    latchkey_root: Path,
    fake_latchkey_binary: Path,
    clean_latchkey_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No on-disk supervisor record => loud non-zero exit, no JSON."""
    del clean_latchkey_env
    monkeypatch.setenv(ENV_LATCHKEY_DIRECTORY, str(latchkey_root))
    monkeypatch.setenv(ENV_LATCHKEY_BINARY, str(fake_latchkey_binary))
    monkeypatch.setenv("HOME", str(tmp_path))

    result = cli_runner.invoke(
        latchkey,
        ["gateway-info"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code != 0
    assert "no ``mngr latchkey forward`` supervisor is running" in result.output.lower()


def test_gateway_info_exits_nonzero_when_supervisor_record_is_stale(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    latchkey_root: Path,
    fake_latchkey_binary: Path,
    clean_latchkey_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Record exists but its PID is a stranger (or dead) => non-zero exit, same message as 'no record'.

    Picks PID 1 (init) on Linux: alive, but its cmdline is not ours, so
    :func:`is_forward_info_alive` rejects it -- the exact PID-reuse case
    we want the subcommand to handle, not propagate as 'still warming up'.
    """
    del clean_latchkey_env
    monkeypatch.setenv(ENV_LATCHKEY_DIRECTORY, str(latchkey_root))
    monkeypatch.setenv(ENV_LATCHKEY_BINARY, str(fake_latchkey_binary))
    monkeypatch.setenv("HOME", str(tmp_path))
    save_forward_info(plugin_data_dir(latchkey_root), _build_forward_info(pid=1, gateway_port=32867))

    result = cli_runner.invoke(
        latchkey,
        ["gateway-info"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code != 0
    assert "no ``mngr latchkey forward`` supervisor is running" in result.output.lower()


def test_gateway_info_exits_nonzero_while_supervisor_still_warming_up(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    latchkey_root: Path,
    fake_latchkey_binary: Path,
    clean_latchkey_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Live supervisor but no port stamped yet => 'still warming up' message."""
    del clean_latchkey_env
    monkeypatch.setenv(ENV_LATCHKEY_DIRECTORY, str(latchkey_root))
    monkeypatch.setenv(ENV_LATCHKEY_BINARY, str(fake_latchkey_binary))
    monkeypatch.setenv("HOME", str(tmp_path))

    with _fake_running_supervisor() as pid:
        save_forward_info(plugin_data_dir(latchkey_root), _build_forward_info(pid=pid, gateway_port=None))
        result = cli_runner.invoke(
            latchkey,
            ["gateway-info"],
            obj=plugin_manager,
            catch_exceptions=False,
        )
    assert result.exit_code != 0
    assert "has not finished binding" in result.output


# -- link-permissions -------------------------------------------------------


def test_link_permissions_replaces_opaque_with_symlink_to_canonical(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    latchkey_root: Path,
    fake_latchkey_binary: Path,
    clean_latchkey_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """End-to-end: after ``link-permissions`` the opaque path is a symlink to the canonical host path."""
    del clean_latchkey_env
    monkeypatch.setenv(ENV_LATCHKEY_DIRECTORY, str(latchkey_root))
    monkeypatch.setenv(ENV_LATCHKEY_BINARY, str(fake_latchkey_binary))
    monkeypatch.setenv("HOME", str(tmp_path))

    # Step 1: create-agent-env to materialize the opaque handle.
    create_result = cli_runner.invoke(
        latchkey,
        ["create-agent-env"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert create_result.exit_code == 0, create_result.output
    opaque_path = Path(json.loads(create_result.output)["opaque_permissions_path"])

    # Step 2: link-permissions swings the symlink.
    link_result = cli_runner.invoke(
        latchkey,
        [
            "link-permissions",
            "--host-id",
            str(_HOST_ID_ONE),
            "--opaque-path",
            str(opaque_path),
        ],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert link_result.exit_code == 0, link_result.output
    assert opaque_path.is_symlink()
    canonical = permissions_path_for_host(plugin_data_dir(latchkey_root), _HOST_ID_ONE)
    assert opaque_path.resolve() == canonical.resolve()
    assert canonical.is_file()


def test_link_permissions_rejects_invalid_host_id(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    latchkey_root: Path,
    fake_latchkey_binary: Path,
    clean_latchkey_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An ``--host-id`` that doesn't conform to :class:`HostId` exits non-zero with a usage error."""
    del clean_latchkey_env
    monkeypatch.setenv(ENV_LATCHKEY_DIRECTORY, str(latchkey_root))
    monkeypatch.setenv(ENV_LATCHKEY_BINARY, str(fake_latchkey_binary))
    monkeypatch.setenv("HOME", str(tmp_path))

    bogus_path = tmp_path / "bogus.json"
    bogus_path.write_text("{}")

    result = cli_runner.invoke(
        latchkey,
        [
            "link-permissions",
            "--host-id",
            "not-a-host-id",
            "--opaque-path",
            str(bogus_path),
        ],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code != 0


def test_link_permissions_rejects_missing_opaque_path(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    latchkey_root: Path,
    fake_latchkey_binary: Path,
    clean_latchkey_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del clean_latchkey_env
    monkeypatch.setenv(ENV_LATCHKEY_DIRECTORY, str(latchkey_root))
    monkeypatch.setenv(ENV_LATCHKEY_BINARY, str(fake_latchkey_binary))
    monkeypatch.setenv("HOME", str(tmp_path))

    nonexistent = tmp_path / f"missing-{uuid4().hex}.json"
    result = cli_runner.invoke(
        latchkey,
        [
            "link-permissions",
            "--host-id",
            str(_HOST_ID_ONE),
            "--opaque-path",
            str(nonexistent),
        ],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code != 0
    assert "does not exist" in result.output


# -- forward ----------------------------------------------------------------


def test_forward_refuses_to_start_when_another_supervisor_is_alive(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    latchkey_root: Path,
    fake_latchkey_binary: Path,
    clean_latchkey_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A live competing forward record => clean ClickException, no second gateway spawn."""
    del clean_latchkey_env
    monkeypatch.setenv(ENV_LATCHKEY_DIRECTORY, str(latchkey_root))
    monkeypatch.setenv(ENV_LATCHKEY_BINARY, str(fake_latchkey_binary))
    monkeypatch.setenv("HOME", str(tmp_path))

    with _fake_running_supervisor() as pid:
        save_forward_info(plugin_data_dir(latchkey_root), _build_forward_info(pid=pid, gateway_port=12345))
        result = cli_runner.invoke(
            latchkey,
            ["forward"],
            obj=plugin_manager,
            catch_exceptions=False,
        )
    assert result.exit_code != 0
    assert "already running" in result.output.lower()
    assert str(pid) in result.output
    # The competing record must be preserved verbatim -- the failing
    # ``forward`` invocation must not clobber the live supervisor's
    # PID.
    persisted = load_forward_info(plugin_data_dir(latchkey_root))
    assert persisted is not None
    assert persisted.pid == pid
    assert persisted.gateway_port == 12345


# -- Group wiring -----------------------------------------------------------


def test_group_exposes_documented_subcommands() -> None:
    """The ``mngr latchkey`` group exposes the documented subcommands."""
    assert set(latchkey.commands.keys()) == {
        "create-agent-env",
        "link-permissions",
        "register-agent",
        "forward",
        "admin-jwt",
        "gateway-info",
    }


def test_help_text_lists_subcommands(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(latchkey, ["--help"], catch_exceptions=False)
    assert result.exit_code == 0
    for subcommand in (
        "create-agent-env",
        "link-permissions",
        "register-agent",
        "forward",
        "admin-jwt",
        "gateway-info",
    ):
        assert subcommand in result.output
