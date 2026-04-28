"""Tests for filter_opts module."""

import click
import pytest

from imbue.mngr.cli.filter_opts import AgentFilterCliOptions
from imbue.mngr.cli.filter_opts import build_agent_filter_cel


def _opts(**overrides: object) -> AgentFilterCliOptions:
    """Build an AgentFilterCliOptions with explicit overrides over the field defaults."""
    return AgentFilterCliOptions(**overrides)  # type: ignore[arg-type]


def test_empty_options_produces_empty_filters() -> None:
    include, exclude = build_agent_filter_cel(_opts())
    assert include == ()
    assert exclude == ()


def test_passthrough_include_and_exclude() -> None:
    include, exclude = build_agent_filter_cel(_opts(include=('state == "RUNNING"',), exclude=('state == "DONE"',)))
    assert include == ('state == "RUNNING"',)
    assert exclude == ('state == "DONE"',)


def test_running_alias() -> None:
    include, exclude = build_agent_filter_cel(_opts(running=True))
    assert include == ('state == "RUNNING"',)
    assert exclude == ()


def test_stopped_alias() -> None:
    include, _ = build_agent_filter_cel(_opts(stopped=True))
    assert include == ('state == "STOPPED"',)


def test_archived_alias() -> None:
    include, _ = build_agent_filter_cel(_opts(archived=True))
    assert include == ("has(labels.archived_at)",)


def test_local_alias() -> None:
    include, exclude = build_agent_filter_cel(_opts(local=True))
    assert include == ('host.provider == "local"',)
    assert exclude == ()


def test_remote_alias_goes_into_exclude() -> None:
    include, exclude = build_agent_filter_cel(_opts(remote=True))
    assert include == ()
    assert exclude == ('host.provider == "local"',)


def test_active_alias_excludes_archived_and_unhealthy_hosts() -> None:
    include, exclude = build_agent_filter_cel(_opts(active=True))
    assert exclude == ("has(labels.archived_at)",)
    assert include == (
        'host.state != "CRASHED"',
        'host.state != "FAILED"',
        'host.state != "DESTROYED"',
    )


def test_project_single() -> None:
    include, _ = build_agent_filter_cel(_opts(project=("mngr",)))
    assert include == ('labels.project == "mngr"',)


def test_project_multiple_ors_into_one_filter() -> None:
    include, _ = build_agent_filter_cel(_opts(project=("mngr", "other")))
    assert include == ('labels.project == "mngr" || labels.project == "other"',)


def test_label_kv() -> None:
    include, _ = build_agent_filter_cel(_opts(label=("env=prod",)))
    assert include == ('labels.env == "prod"',)


def test_host_label_kv() -> None:
    include, _ = build_agent_filter_cel(_opts(host_label=("region=us-east",)))
    assert include == ('host.tags.region == "us-east"',)


def test_label_without_equals_raises() -> None:
    with pytest.raises(click.BadParameter):
        build_agent_filter_cel(_opts(label=("noequals",)))


def test_host_label_without_equals_raises() -> None:
    with pytest.raises(click.BadParameter):
        build_agent_filter_cel(_opts(host_label=("noequals",)))


def test_combined_aliases_compose() -> None:
    include, exclude = build_agent_filter_cel(
        _opts(
            include=('name == "foo"',),
            exclude=('id == "bar"',),
            running=True,
            remote=True,
            project=("mngr",),
        )
    )
    assert include == (
        'name == "foo"',
        'state == "RUNNING"',
        'labels.project == "mngr"',
    )
    assert exclude == (
        'id == "bar"',
        'host.provider == "local"',
    )
