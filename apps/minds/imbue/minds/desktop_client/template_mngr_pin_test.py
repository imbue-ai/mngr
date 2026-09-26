import os
from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.template_mngr_pin import INTERNAL_GIT_TOKEN_ENV_VAR
from imbue.minds.desktop_client.template_mngr_pin import INTERNAL_GIT_TOKEN_REMOTE_PATH
from imbue.minds.desktop_client.template_mngr_pin import INTERNAL_GIT_TOKEN_VAULT_KEY
from imbue.minds.desktop_client.template_mngr_pin import INTERNAL_MNGR_REPO_URL
from imbue.minds.desktop_client.template_mngr_pin import InternalMngrCredentialError
from imbue.minds.desktop_client.template_mngr_pin import MngrPinKind
from imbue.minds.desktop_client.template_mngr_pin import NO_INTERNAL_MNGR_CREDENTIAL
from imbue.minds.desktop_client.template_mngr_pin import PUBLIC_MNGR_REPO_URL
from imbue.minds.desktop_client.template_mngr_pin import TemplateMngrPin
from imbue.minds.desktop_client.template_mngr_pin import TemplateMngrPinError
from imbue.minds.desktop_client.template_mngr_pin import internal_mngr_credential
from imbue.minds.desktop_client.template_mngr_pin import parse_template_mngr_pin
from imbue.minds.desktop_client.template_mngr_pin import read_template_mngr_pin
from imbue.minds.desktop_client.template_mngr_pin import resolve_internal_git_token
from imbue.minds.desktop_client.testing import TEMPLATE_MNGR_PIN_REV_FOR_TEST
from imbue.minds.desktop_client.testing import install_stub_on_path
from imbue.minds.desktop_client.testing import template_pyproject_pinning_mngr
from imbue.minds.desktop_client.testing import write_template_pyproject_pinning_mngr
from imbue.minds.primitives import GitCommitHash
from imbue.minds.primitives import LaunchMode


def test_a_public_pin_parses_as_public() -> None:
    assert parse_template_mngr_pin(template_pyproject_pinning_mngr(PUBLIC_MNGR_REPO_URL)) == TemplateMngrPin(
        kind=MngrPinKind.PUBLIC, rev=GitCommitHash(TEMPLATE_MNGR_PIN_REV_FOR_TEST)
    )


def test_an_internal_pin_parses_as_internal() -> None:
    assert parse_template_mngr_pin(template_pyproject_pinning_mngr(INTERNAL_MNGR_REPO_URL)) == TemplateMngrPin(
        kind=MngrPinKind.INTERNAL, rev=GitCommitHash(TEMPLATE_MNGR_PIN_REV_FOR_TEST)
    )


def test_a_template_without_an_mngr_source_has_no_pin() -> None:
    assert parse_template_mngr_pin('[tool.uv.sources]\ntk = { path = "system/vendor/tk" }\n') is None
    assert parse_template_mngr_pin('[project]\nname = "x"\n') is None


def test_a_pin_on_another_repo_is_not_this_apps_concern() -> None:
    assert parse_template_mngr_pin(template_pyproject_pinning_mngr("https://github.com/someone-else/mngr")) is None


def test_a_pin_at_a_branch_cannot_build_and_says_so() -> None:
    with pytest.raises(TemplateMngrPinError, match="not a full 40-hex commit"):
        parse_template_mngr_pin(template_pyproject_pinning_mngr(PUBLIC_MNGR_REPO_URL, rev="main"))


def test_a_pyproject_that_is_not_toml_cannot_build_and_says_so() -> None:
    with pytest.raises(TemplateMngrPinError, match="not valid TOML"):
        parse_template_mngr_pin("[tool.uv.sources\nimbue-mngr = {")


def test_a_checkout_without_a_pyproject_has_no_pin(tmp_path: Path) -> None:
    assert read_template_mngr_pin(tmp_path) is None


def test_the_env_var_credential_wins_without_reading_vault(root_concurrency_group: ConcurrencyGroup) -> None:
    token = resolve_internal_git_token(
        {INTERNAL_GIT_TOKEN_ENV_VAR: " tok "},
        is_vault_fallback_allowed=False,
        parent_concurrency_group=root_concurrency_group,
    )

    assert token is not None and token.get_secret_value() == "tok"


def test_no_env_var_and_no_vault_fallback_is_no_credential(root_concurrency_group: ConcurrencyGroup) -> None:
    assert (
        resolve_internal_git_token(
            {INTERNAL_GIT_TOKEN_ENV_VAR: ""},
            is_vault_fallback_allowed=False,
            parent_concurrency_group=root_concurrency_group,
        )
        is None
    )


_VAULT_STUB_LEAF = '{"data": {"data": {"value": " tok-from-vault-70213 "}}}'


def _vault_stub_body(list_output: str, get_output: str, exit_code: int = 0) -> str:
    """A ``vault`` that answers ``kv list`` with ``list_output`` and ``kv get`` with ``get_output``."""
    return f"case \"$2\" in list) echo '{list_output}';; get) echo '{get_output}';; esac\nexit {exit_code}"


@pytest.mark.parametrize("is_parent_given", [True, False])
def test_the_vault_fallback_yields_the_shared_leaf_s_value_under_a_group_of_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_concurrency_group: ConcurrencyGroup, is_parent_given: bool
) -> None:
    install_stub_on_path(
        tmp_path / "bin",
        monkeypatch,
        "vault",
        _vault_stub_body(f'["{INTERNAL_GIT_TOKEN_VAULT_KEY}"]', _VAULT_STUB_LEAF),
    )

    token = resolve_internal_git_token(
        {},
        is_vault_fallback_allowed=True,
        parent_concurrency_group=root_concurrency_group if is_parent_given else None,
    )

    assert token is not None and token.get_secret_value() == "tok-from-vault-70213"


@pytest.mark.parametrize(
    ("script_body", "reason"),
    [
        ("echo 'permission denied' >&2; exit 2", "the entry is absent or unreadable"),
        (_vault_stub_body('["OTHER_KEY"]', _VAULT_STUB_LEAF), "the entry has no token leaf"),
        (
            _vault_stub_body(f'["{INTERNAL_GIT_TOKEN_VAULT_KEY}"]', '{"data": {"data": {"value": "  "}}}'),
            "the leaf is blank",
        ),
    ],
)
def test_a_vault_that_yields_no_token_is_no_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script_body: str, reason: str
) -> None:
    install_stub_on_path(tmp_path / "bin", monkeypatch, "vault", script_body)

    assert resolve_internal_git_token({}, is_vault_fallback_allowed=True, parent_concurrency_group=None) is None, (
        reason
    )


def test_no_vault_at_all_is_no_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    vault_free_path = os.pathsep.join(
        entry for entry in os.environ["PATH"].split(os.pathsep) if not (Path(entry) / "vault").exists()
    )
    monkeypatch.setenv("PATH", vault_free_path)

    assert resolve_internal_git_token({}, is_vault_fallback_allowed=True, parent_concurrency_group=None) is None


def test_a_public_pin_needs_no_credential_even_when_one_is_around(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    write_template_pyproject_pinning_mngr(tmp_path, PUBLIC_MNGR_REPO_URL)
    with internal_mngr_credential(
        LaunchMode.DOCKER,
        tmp_path,
        {INTERNAL_GIT_TOKEN_ENV_VAR: "tok"},
        is_vault_fallback_allowed=False,
        parent_concurrency_group=root_concurrency_group,
    ) as credential:
        assert credential == NO_INTERNAL_MNGR_CREDENTIAL


def test_a_template_with_no_pin_needs_no_credential(tmp_path: Path, root_concurrency_group: ConcurrencyGroup) -> None:
    with internal_mngr_credential(
        LaunchMode.LIMA,
        tmp_path,
        {},
        is_vault_fallback_allowed=False,
        parent_concurrency_group=root_concurrency_group,
    ) as c:
        assert c == NO_INTERNAL_MNGR_CREDENTIAL


@pytest.mark.parametrize(
    "launch_mode", [LaunchMode.DOCKER, LaunchMode.VULTR, LaunchMode.AWS, LaunchMode.GCP, LaunchMode.AZURE]
)
def test_a_dockerfile_mode_gets_the_token_in_the_env_only(
    tmp_path: Path, launch_mode: LaunchMode, root_concurrency_group: ConcurrencyGroup
) -> None:
    write_template_pyproject_pinning_mngr(tmp_path, INTERNAL_MNGR_REPO_URL)
    with internal_mngr_credential(
        launch_mode,
        tmp_path,
        {INTERNAL_GIT_TOKEN_ENV_VAR: "tok"},
        is_vault_fallback_allowed=False,
        parent_concurrency_group=root_concurrency_group,
    ) as credential:
        assert credential.create_args == ()
        assert credential.subprocess_env() == {INTERNAL_GIT_TOKEN_ENV_VAR: "tok"}


@pytest.mark.parametrize("launch_mode", [LaunchMode.LIMA, LaunchMode.MODAL])
def test_an_ssh_provisioned_mode_also_uploads_the_token_as_a_file_that_is_removed_after(
    tmp_path: Path, launch_mode: LaunchMode, root_concurrency_group: ConcurrencyGroup
) -> None:
    write_template_pyproject_pinning_mngr(tmp_path, INTERNAL_MNGR_REPO_URL)
    with internal_mngr_credential(
        launch_mode,
        tmp_path,
        {INTERNAL_GIT_TOKEN_ENV_VAR: "tok"},
        is_vault_fallback_allowed=False,
        parent_concurrency_group=root_concurrency_group,
    ) as credential:
        assert credential.subprocess_env() == {INTERNAL_GIT_TOKEN_ENV_VAR: "tok"}
        assert credential.create_args[0] == "--upload-file"
        local_path, remote_path = credential.create_args[1].rsplit(":", 1)
        assert remote_path == INTERNAL_GIT_TOKEN_REMOTE_PATH
        token_file = Path(local_path)
        assert token_file.read_text() == "tok"
        assert token_file.stat().st_mode & 0o777 == 0o600
    assert not token_file.exists()


def test_an_internal_pin_with_no_credential_fails_before_any_build_with_the_remedy(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    write_template_pyproject_pinning_mngr(tmp_path, INTERNAL_MNGR_REPO_URL)
    with pytest.raises(InternalMngrCredentialError, match=f"export {INTERNAL_GIT_TOKEN_ENV_VAR}"):
        with internal_mngr_credential(
            LaunchMode.DOCKER,
            tmp_path,
            {},
            is_vault_fallback_allowed=False,
            parent_concurrency_group=root_concurrency_group,
        ):
            raise AssertionError("the create must not start")


def test_a_create_without_a_checkout_forwards_a_credential_it_has_and_never_requires_one(
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    with internal_mngr_credential(
        LaunchMode.IMBUE_CLOUD,
        None,
        {INTERNAL_GIT_TOKEN_ENV_VAR: "tok"},
        is_vault_fallback_allowed=False,
        parent_concurrency_group=root_concurrency_group,
    ) as forwarded:
        assert forwarded.subprocess_env() == {INTERNAL_GIT_TOKEN_ENV_VAR: "tok"}
        assert forwarded.create_args == ()
    with internal_mngr_credential(
        LaunchMode.IMBUE_CLOUD,
        None,
        {},
        is_vault_fallback_allowed=False,
        parent_concurrency_group=root_concurrency_group,
    ) as absent:
        assert absent == NO_INTERNAL_MNGR_CREDENTIAL
