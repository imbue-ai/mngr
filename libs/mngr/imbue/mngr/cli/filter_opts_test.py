"""Tests for filter_opts module."""

import click
import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.cli.filter_opts import AgentFilterCliOptions
from imbue.mngr.cli.filter_opts import build_agent_filter_cel
from imbue.mngr.errors import MngrError


def test_empty_options_produces_empty_filters(cg: ConcurrencyGroup) -> None:
    include, exclude = build_agent_filter_cel(AgentFilterCliOptions(), cg)
    assert include == ()
    assert exclude == ()


def test_passthrough_include_and_exclude(cg: ConcurrencyGroup) -> None:
    include, exclude = build_agent_filter_cel(
        AgentFilterCliOptions(include=('state == "RUNNING"',), exclude=('state == "DONE"',)), cg
    )
    assert include == ('state == "RUNNING"',)
    assert exclude == ('state == "DONE"',)


def test_running_alias(cg: ConcurrencyGroup) -> None:
    include, exclude = build_agent_filter_cel(AgentFilterCliOptions(running=True), cg)
    assert include == ('state == "RUNNING"',)
    assert exclude == ()


def test_stopped_alias(cg: ConcurrencyGroup) -> None:
    include, _ = build_agent_filter_cel(AgentFilterCliOptions(stopped=True), cg)
    assert include == ('state == "STOPPED"',)


def test_archived_alias(cg: ConcurrencyGroup) -> None:
    include, _ = build_agent_filter_cel(AgentFilterCliOptions(archived=True), cg)
    assert include == ("has(labels.archived_at)",)


def test_local_alias(cg: ConcurrencyGroup) -> None:
    include, exclude = build_agent_filter_cel(AgentFilterCliOptions(local=True), cg)
    assert include == ('host.provider == "local"',)
    assert exclude == ()


def test_remote_alias_goes_into_exclude(cg: ConcurrencyGroup) -> None:
    include, exclude = build_agent_filter_cel(AgentFilterCliOptions(remote=True), cg)
    assert include == ()
    assert exclude == ('host.provider == "local"',)


def test_active_alias_excludes_archived_and_unhealthy_hosts(cg: ConcurrencyGroup) -> None:
    include, exclude = build_agent_filter_cel(AgentFilterCliOptions(active=True), cg)
    assert exclude == ("has(labels.archived_at)",)
    assert include == (
        'host.state != "CRASHED"',
        'host.state != "FAILED"',
        'host.state != "DESTROYED"',
    )


def test_project_single(cg: ConcurrencyGroup) -> None:
    include, _ = build_agent_filter_cel(AgentFilterCliOptions(project=("mngr",)), cg)
    assert include == ('labels.project == "mngr"',)


def test_project_multiple_ors_into_one_filter(cg: ConcurrencyGroup) -> None:
    include, _ = build_agent_filter_cel(AgentFilterCliOptions(project=("mngr", "other")), cg)
    assert include == ('labels.project == "mngr" || labels.project == "other"',)


def test_label_kv(cg: ConcurrencyGroup) -> None:
    include, _ = build_agent_filter_cel(AgentFilterCliOptions(label=("env=prod",)), cg)
    assert include == ('labels.env == "prod"',)


def test_host_label_kv(cg: ConcurrencyGroup) -> None:
    include, _ = build_agent_filter_cel(AgentFilterCliOptions(host_label=("region=us-east",)), cg)
    assert include == ('host.tags.region == "us-east"',)


def test_label_without_equals_raises(cg: ConcurrencyGroup) -> None:
    with pytest.raises(click.BadParameter, match="Label must be in KEY=VALUE format"):
        build_agent_filter_cel(AgentFilterCliOptions(label=("noequals",)), cg)


def test_host_label_without_equals_raises_with_host_label_wording(cg: ConcurrencyGroup) -> None:
    with pytest.raises(click.BadParameter, match="Host label must be in KEY=VALUE format"):
        build_agent_filter_cel(AgentFilterCliOptions(host_label=("noequals",)), cg)


def test_invalid_cel_in_include_fails_fast(cg: ConcurrencyGroup) -> None:
    with pytest.raises(MngrError):
        build_agent_filter_cel(AgentFilterCliOptions(include=("invalid(",)), cg)


def test_invalid_cel_in_exclude_fails_fast(cg: ConcurrencyGroup) -> None:
    with pytest.raises(MngrError):
        build_agent_filter_cel(AgentFilterCliOptions(exclude=("invalid(",)), cg)


def test_combined_aliases_compose(cg: ConcurrencyGroup) -> None:
    include, exclude = build_agent_filter_cel(
        AgentFilterCliOptions(
            include=('name == "foo"',),
            exclude=('id == "bar"',),
            running=True,
            remote=True,
            project=("mngr",),
        ),
        cg,
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
