"""Tests for filter_opts module."""

import click
import pytest

from imbue.mngr.cli.filter_opts import AgentFilterCliOptions
from imbue.mngr.cli.filter_opts import build_agent_filter_cel
from imbue.mngr.errors import MngrError


def test_empty_options_produces_empty_filters() -> None:
    include, exclude = build_agent_filter_cel(AgentFilterCliOptions())
    assert include == ()
    assert exclude == ()


def test_passthrough_include_and_exclude() -> None:
    include, exclude = build_agent_filter_cel(
        AgentFilterCliOptions(include=('state == "RUNNING"',), exclude=('state == "DONE"',))
    )
    assert include == ('state == "RUNNING"',)
    assert exclude == ('state == "DONE"',)


def test_running_alias() -> None:
    include, exclude = build_agent_filter_cel(AgentFilterCliOptions(running=True))
    assert include == ('state == "RUNNING"',)
    assert exclude == ()


def test_stopped_alias() -> None:
    include, _ = build_agent_filter_cel(AgentFilterCliOptions(stopped=True))
    assert include == ('state == "STOPPED"',)


def test_archived_alias() -> None:
    include, _ = build_agent_filter_cel(AgentFilterCliOptions(archived=True))
    assert include == ("has(labels.archived_at)",)


def test_local_alias() -> None:
    include, exclude = build_agent_filter_cel(AgentFilterCliOptions(local=True))
    assert include == ('host.provider == "local"',)
    assert exclude == ()


def test_remote_alias_goes_into_exclude() -> None:
    include, exclude = build_agent_filter_cel(AgentFilterCliOptions(remote=True))
    assert include == ()
    assert exclude == ('host.provider == "local"',)


def test_active_alias_excludes_archived_and_unhealthy_hosts() -> None:
    include, exclude = build_agent_filter_cel(AgentFilterCliOptions(active=True))
    assert exclude == ("has(labels.archived_at)",)
    assert include == (
        'host.state != "CRASHED"',
        'host.state != "FAILED"',
        'host.state != "DESTROYED"',
    )


def test_project_single() -> None:
    include, _ = build_agent_filter_cel(AgentFilterCliOptions(project=("mngr",)))
    assert include == ('labels.project == "mngr"',)


def test_project_multiple_ors_into_one_filter() -> None:
    include, _ = build_agent_filter_cel(AgentFilterCliOptions(project=("mngr", "other")))
    assert include == ('labels.project == "mngr" || labels.project == "other"',)


def test_label_kv() -> None:
    include, _ = build_agent_filter_cel(AgentFilterCliOptions(label=("env=prod",)))
    assert include == ('labels.env == "prod"',)


def test_host_label_kv() -> None:
    include, _ = build_agent_filter_cel(AgentFilterCliOptions(host_label=("region=us-east",)))
    assert include == ('host.tags.region == "us-east"',)


def test_label_without_equals_raises() -> None:
    with pytest.raises(click.BadParameter, match="Label must be in KEY=VALUE format"):
        build_agent_filter_cel(AgentFilterCliOptions(label=("noequals",)))


def test_host_label_without_equals_raises_with_host_label_wording() -> None:
    with pytest.raises(click.BadParameter, match="Host label must be in KEY=VALUE format"):
        build_agent_filter_cel(AgentFilterCliOptions(host_label=("noequals",)))


def test_invalid_cel_in_include_fails_fast() -> None:
    with pytest.raises(MngrError):
        build_agent_filter_cel(AgentFilterCliOptions(include=("invalid(",)))


def test_invalid_cel_in_exclude_fails_fast() -> None:
    with pytest.raises(MngrError):
        build_agent_filter_cel(AgentFilterCliOptions(exclude=("invalid(",)))


def test_combined_aliases_compose() -> None:
    include, exclude = build_agent_filter_cel(
        AgentFilterCliOptions(
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
