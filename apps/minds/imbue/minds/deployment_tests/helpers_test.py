from pathlib import Path

import pytest

from imbue.minds.deployment_tests.helpers import pool_lease_published_ports
from imbue.mngr.primitives import InvalidPublishedPortError


def _write_settings(tmp_path: Path, body: str) -> Path:
    template = tmp_path / "template"
    (template / ".mngr").mkdir(parents=True)
    (template / ".mngr" / "settings.toml").write_text(body)
    return template


def test_pool_lease_published_ports_reads_the_bake_templates_extended_declarations(tmp_path: Path) -> None:
    template = _write_settings(
        tmp_path,
        '[create_templates.main]\npublish = [8080]\n\n[create_templates.pool_host]\npublish__extend = ["59000/udp", "59000/tcp"]\n',
    )

    assert pool_lease_published_ports(template) == [
        {"guest_port": 8080, "protocol": "tcp"},
        {"guest_port": 59000, "protocol": "udp"},
        {"guest_port": 59000, "protocol": "tcp"},
    ]


def test_pool_lease_published_ports_lets_the_pool_host_template_replace_the_main_list(tmp_path: Path) -> None:
    template = _write_settings(
        tmp_path,
        '[create_templates.main]\npublish = [8080]\n\n[create_templates.pool_host]\npublish = ["443/tcp"]\n',
    )

    assert pool_lease_published_ports(template) == [{"guest_port": 443, "protocol": "tcp"}]


def test_pool_lease_published_ports_is_empty_for_a_template_declaring_none(tmp_path: Path) -> None:
    template = _write_settings(tmp_path, "[create_templates.main]\nname = 'main'\n")

    assert pool_lease_published_ports(template) == []


def test_pool_lease_published_ports_refuses_a_duplicate_declaration(tmp_path: Path) -> None:
    template = _write_settings(
        tmp_path,
        '[create_templates.main]\npublish = ["59000/tcp"]\n\n[create_templates.pool_host]\npublish__extend = ["59000/tcp"]\n',
    )

    with pytest.raises(InvalidPublishedPortError):
        pool_lease_published_ports(template)
