"""Guard the published ``imbue-studio-skills`` plugin against drift from the live API and CLI.

The skills under ``apps/minds/skills_plugin/`` are exported verbatim into the public
``imbue-ai/imbue-skills`` marketplace. Every API path and every ``mngr <subcommand>``
they name inside a code span or fenced block must exist: the paths in the OpenAPI
document the desktop client generates for the transport the example uses, and the
subcommands on the CLI this checkout builds. A route or command rename then fails
here, in the pull request that makes it, rather than in an agent's session later.
"""

import re
from pathlib import Path
from typing import Final

import pytest
from flask import Flask

from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.api_schema import build_openapi_document
from imbue.minds.desktop_client.api_transport import ApiTransport
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.mngr.main import cli
from imbue.mngr.utils.agent_skills import code_regions
from imbue.mngr.utils.agent_skills import mngr_subcommands_referenced
from imbue.mngr.utils.agent_skills import skill_files
from imbue.mngr.utils.agent_skills import skill_shape_problems

SKILLS_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "skills_plugin" / "skills"

# How a skill example reaches the API decides which schema document must list its path.
_GATEWAY_PREFIX: Final[str] = "http://latchkey-self.invalid/minds-api-proxy"
_SOCKET_PREFIX: Final[str] = "http://localhost"
# An API path as a skill writes it: optionally behind one of the prefixes above,
# then ``/api/...`` up to the end of the token.
# A bare path must not sit inside another one, so ``/ui/api/...`` (the desktop
# UI's own routes) stays out of the match.
_API_PATH: Final[re.Pattern[str]] = re.compile(
    r"(?:(?P<prefix>http://latchkey-self\.invalid/minds-api-proxy|http://localhost)|(?<![\w/]))"
    r"(?P<path>/api/[A-Za-z0-9_./<>{}-]*)"
)
# Both placeholder spellings a skill may use for a path parameter.
_PLACEHOLDER: Final[re.Pattern[str]] = re.compile(r"<[^/<>]+>|\{[^/{}]+\}")
# The query-string and trailing-punctuation characters a token may carry past the path.
_PATH_TRAILERS: Final[str] = ".,;:)"


def _normalized_path(raw: str) -> str:
    """Return the path with every placeholder spelled the OpenAPI way, as ``{param}``."""
    return _PLACEHOLDER.sub("{param}", raw.rstrip(_PATH_TRAILERS))


def _schema_paths_normalized(document_paths: set[str]) -> set[str]:
    return {_normalized_path(path) for path in document_paths}


def _desktop_client_app(tmp_path: Path) -> Flask:
    return create_desktop_client(
        auth_store=FileAuthStore(data_directory=tmp_path / "auth"),
        backend_resolver=make_resolver_with_data(),
        http_client=None,
        paths=InstallationPaths(data_dir=tmp_path / "minds"),
        minds_api_key="test-minds-api-key",
    )


def _documented_paths(app: Flask, transport: ApiTransport) -> set[str]:
    rules = [
        (rule.rule, frozenset(rule.methods or set()), tuple(rule.arguments), rule.endpoint)
        for rule in app.url_map.iter_rules()
    ]
    document = build_openapi_document(rules, app.view_functions, transport=transport)
    return set(document["paths"].keys())


def _api_paths_by_transport(markdown: str) -> dict[ApiTransport, set[str]]:
    """Return the API paths a skill names, grouped by the transport its example uses.

    A bare path (no prefix) is held to the socket document, which lists every
    route, so a path reachable over either transport passes and one reachable over
    neither fails.
    """
    paths_by_transport: dict[ApiTransport, set[str]] = {ApiTransport.GATEWAY: set(), ApiTransport.LOCAL_SOCKET: set()}
    for region in code_regions(markdown):
        for match in _API_PATH.finditer(region):
            transport = ApiTransport.GATEWAY if match.group("prefix") == _GATEWAY_PREFIX else ApiTransport.LOCAL_SOCKET
            paths_by_transport[transport].add(_normalized_path(match.group("path")))
    return paths_by_transport


def _skill_ids() -> list[str]:
    return [path.parent.name for path in skill_files(SKILLS_DIR)]


def test_plugin_directory_has_the_published_shape() -> None:
    plugin_dir = SKILLS_DIR.parent
    assert (plugin_dir / ".claude-plugin" / "plugin.json").is_file()
    assert (plugin_dir / "README.md").is_file()
    assert "imbue-studio" in _skill_ids()


@pytest.mark.parametrize("skill_name", _skill_ids())
def test_skill_has_valid_shape(skill_name: str) -> None:
    assert skill_shape_problems(SKILLS_DIR / skill_name / "SKILL.md") == ()


@pytest.mark.parametrize("skill_name", _skill_ids())
def test_skill_mngr_subcommands_exist_on_the_live_cli(skill_name: str) -> None:
    referenced = mngr_subcommands_referenced((SKILLS_DIR / skill_name / "SKILL.md").read_text(encoding="utf-8"))
    missing = referenced - set(cli.commands)
    assert not missing, f"{skill_name} names mngr subcommands the live CLI does not have: {sorted(missing)}"


@pytest.mark.parametrize("skill_name", _skill_ids())
def test_skill_api_paths_exist_in_the_schema_for_their_transport(skill_name: str, tmp_path: Path) -> None:
    markdown = (SKILLS_DIR / skill_name / "SKILL.md").read_text(encoding="utf-8")
    paths_by_transport = _api_paths_by_transport(markdown)
    # The studio skill exists to teach the API, so a scan finding no path means the regex broke.
    assert any(paths_by_transport.values()), f"{skill_name} names no API path in any code region"

    app = _desktop_client_app(tmp_path)
    for transport, referenced in paths_by_transport.items():
        documented = _schema_paths_normalized(_documented_paths(app, transport))
        missing = referenced - documented
        assert not missing, f"{skill_name} names paths the {transport.value} schema does not list: {sorted(missing)}"
