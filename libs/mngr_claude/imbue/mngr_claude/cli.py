"""CLI commands for the Claude plugin."""

from pathlib import Path

import click
from loguru import logger

from imbue.mngr_claude.claude_config import add_claude_trust_for_path
from imbue.mngr_claude.claude_config import auto_dismiss_claude_dialogs
from imbue.mngr_claude.claude_config import complete_onboarding
from imbue.mngr_claude.claude_config import dismiss_effort_callout
from imbue.mngr_claude.claude_config import get_user_claude_config_path


@click.group(name="claude")
def claude_group() -> None:
    """Claude Code configuration utilities."""


@claude_group.group(name="dismiss")
def dismiss_group() -> None:
    """Dismiss Claude Code startup dialogs in ~/.claude.json.

    These dialogs can block automated input when running agents via mngr.
    """


@dismiss_group.command(name="trust")
@click.argument("path", type=click.Path(exists=True, resolve_path=True))
def dismiss_trust(path: str) -> None:
    """Mark a directory as trusted by Claude Code."""
    config_path = get_user_claude_config_path()
    add_claude_trust_for_path(config_path, Path(path))
    logger.info("Marked {} as trusted in {}", path, config_path)


@dismiss_group.command(name="effort-callout")
def dismiss_effort_callout_cmd() -> None:
    """Dismiss the Claude Code effort callout tip."""
    config_path = get_user_claude_config_path()
    dismiss_effort_callout(config_path)
    logger.info("Dismissed effort callout in {}", config_path)


@dismiss_group.command(name="onboarding")
def dismiss_onboarding() -> None:
    """Mark Claude Code onboarding as completed."""
    config_path = get_user_claude_config_path()
    complete_onboarding(config_path)
    logger.info("Marked onboarding as completed in {}", config_path)


@dismiss_group.command(name="all")
@click.argument("path", type=click.Path(exists=True, resolve_path=True))
def dismiss_all(path: str) -> None:
    """Dismiss all known Claude Code startup dialogs.

    PATH is the directory to trust (required for the trust dialog).
    """
    config_path = get_user_claude_config_path()
    auto_dismiss_claude_dialogs(config_path, Path(path))
    logger.info("Dismissed all Claude dialogs for {} in {}", path, config_path)
