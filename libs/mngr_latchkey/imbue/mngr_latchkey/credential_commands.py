"""Turning latchkey's ``setCredentialsExample`` into a form the user can fill in.

Services latchkey cannot sign in to through a browser (their ``authOptions``
do not include ``browser``: AWS, Coolify, ...) advertise an example invocation
of the command that stores their credentials, with every value the user has to
supply written as an angle-bracketed placeholder::

    latchkey auth set-nocurl aws <access-key-id> <secret-access-key>

Rather than asking the user to run that in a terminal -- where it would miss
the environment the desktop client pins, and land in a different credential
store -- minds renders one labeled input per placeholder and runs the command
itself, against the account the user picked in the permission dialog.

This module owns both halves of that: parsing the example into argv tokens plus
its placeholders, and substituting the user's values back into those tokens --
plus, in :func:`fallback_set_credentials_example`, what to ask of a service
that advertises no example at all. Running the result is
:meth:`imbue.mngr_latchkey.core.Latchkey.auth_set_credentials`;
:func:`describe_credential_command_failure` turns what it prints on a rejected
credential into something worth showing next to those inputs.
"""

import re
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr_latchkey.core import LatchkeyError

# Name of the binary every ``setCredentialsExample`` is expected to invoke. An
# example that starts with anything else is not something we are willing to run.
_LATCHKEY_COMMAND_NAME: Final[str] = "latchkey"

# ``<placeholder>`` spans latchkey uses to mark the values the user must supply.
# The name may not start with whitespace (so a stray ``a < b`` comparison in an
# example is not mistaken for a parameter) and may not nest brackets.
_PLACEHOLDER_PATTERN: Final[re.Pattern[str]] = re.compile(r"<([^<>\s][^<>]*)>")

# Latchkey's global ``--account`` option, which selects the account the
# credentials are stored under. It is a *root* option, so it precedes the
# subcommand rather than trailing the example's own arguments.
_ACCOUNT_OPTION: Final[str] = "--account"

# Line prefixes to drop from a failed command's output: latchkey appends usage
# lines that restate a terminal invocation (which callers collecting the values
# in a UI never show) or, for some services, print the bare ``<placeholder>``
# instead of a real example value. A hard crash adds JS stack frames on top.
_DROPPED_DETAIL_LINE_PREFIXES: Final[tuple[str, ...]] = ("example:", "usage:", "at ", "file://")

# Redundant prefix latchkey puts on its own error lines; the caller's sentence
# already says that something failed.
_ERROR_LINE_PREFIX: Final[str] = "error:"

# Enough for a sentence or two of explanation. Past that it is a crash dump,
# which belongs in the log rather than in a dialog.
_MAX_DETAIL_LENGTH: Final[int] = 300


class CredentialCommandError(LatchkeyError, ValueError):
    """Raised when a suggested credential command cannot be turned into a runnable one."""


class CredentialCommandParameter(FrozenModel):
    """One ``<placeholder>`` of a credential command that the user has to fill in."""

    name: str = Field(description="Placeholder text as it appears between the angle brackets.")
    label: str = Field(description="Human-readable label for the input box the user types the value into.")


class ParsedCredentialCommand(FrozenModel):
    """A parsed ``setCredentialsExample``: its argv tokens plus the placeholders in them."""

    argv_template: tuple[str, ...] = Field(
        description="Argv after the ``latchkey`` binary token, with placeholders still embedded.",
    )
    parameters: tuple[CredentialCommandParameter, ...] = Field(
        description="Distinct placeholders of the command, in order of first appearance.",
    )


@pure
def _humanize_parameter_name(name: str) -> str:
    """Turn a placeholder name like ``access-key-id`` into a label like ``Access key id``."""
    spaced = name.replace("-", " ").replace("_", " ").strip()
    return spaced[:1].upper() + spaced[1:]


@pure
def _distinct_placeholder_names(argv_template: tuple[str, ...]) -> tuple[str, ...]:
    """Return every placeholder name in the tokens, deduplicated, first appearance first."""
    names = [match.group(1) for token in argv_template for match in _PLACEHOLDER_PATTERN.finditer(token)]
    return tuple(dict.fromkeys(names))


@pure
def fallback_set_credentials_example(service_name: str) -> str:
    """Return a generic ``latchkey auth set`` invocation for a service that suggested none.

    ``latchkey auth set`` stores raw request headers for any service, so a
    bearer token is the one credential that can always be asked for. Callers
    use it so a service latchkey cannot sign in to still gets a form.
    """
    return f'latchkey auth set {service_name} -H "Authorization: Bearer <token>"'


@pure
def parse_credential_command_example(command_example: str) -> ParsedCredentialCommand:
    """Split a ``setCredentialsExample`` into argv tokens and the parameters to prompt for.

    Raises :class:`CredentialCommandError` when the example is not a latchkey
    invocation we can fill in and run -- including when it has no placeholders
    at all, since then there is nothing to ask the user for.
    """
    try:
        tokens = shlex.split(command_example)
    except ValueError as e:
        raise CredentialCommandError(f"the suggested command could not be parsed: {command_example}") from e
    if not tokens:
        raise CredentialCommandError("latchkey suggested no command for setting credentials")
    if Path(tokens[0]).name != _LATCHKEY_COMMAND_NAME:
        raise CredentialCommandError(f"the suggested command does not invoke latchkey: {command_example}")

    argv_template = tuple(tokens[1:])
    parameters = tuple(
        CredentialCommandParameter(name=name, label=_humanize_parameter_name(name))
        for name in _distinct_placeholder_names(argv_template)
    )
    if not parameters:
        raise CredentialCommandError(
            f"the suggested command has no <...> parameters to fill in: {command_example}",
        )
    return ParsedCredentialCommand(argv_template=argv_template, parameters=parameters)


@pure
def describe_credential_command_failure(detail: str) -> str:
    """Reduce a rejected credential command's output to the part worth showing.

    Keeps latchkey's explanation of what is wrong with the value ("doesn't look
    like an AWS access key ID...") and drops the usage lines, stack frames and
    the redundant ``Error:`` prefix around it. Returns the empty string when
    nothing explanatory is left, so callers can fall back to their own wording.
    """
    kept_lines = [
        line
        for line in (raw_line.strip() for raw_line in detail.splitlines())
        if line and not line.lower().startswith(_DROPPED_DETAIL_LINE_PREFIXES)
    ]
    if kept_lines and kept_lines[0].lower().startswith(_ERROR_LINE_PREFIX):
        kept_lines[0] = kept_lines[0][len(_ERROR_LINE_PREFIX) :].strip()
    described = " ".join(line for line in kept_lines if line)
    if len(described) <= _MAX_DETAIL_LENGTH:
        return described
    return described[:_MAX_DETAIL_LENGTH].rstrip() + "..."


@pure
def build_credential_command_argv(
    parsed_command: ParsedCredentialCommand,
    value_by_parameter_name: Mapping[str, str],
    account: str,
) -> tuple[str, ...]:
    """Fill the user's values into a parsed command and pin it to one latchkey account.

    The returned argv excludes the binary itself and carries the values
    verbatim (surrounding whitespace stripped, since these are pasted
    credentials), so it must never be logged.

    Raises :class:`CredentialCommandError` when any parameter is left blank.
    """
    value_by_name = {
        parameter.name: value_by_parameter_name.get(parameter.name, "").strip()
        for parameter in parsed_command.parameters
    }
    blank_parameter_labels = tuple(
        parameter.label for parameter in parsed_command.parameters if not value_by_name[parameter.name]
    )
    if blank_parameter_labels:
        raise CredentialCommandError(f"no value was supplied for: {', '.join(blank_parameter_labels)}")

    filled_argv = tuple(
        _PLACEHOLDER_PATTERN.sub(lambda match: value_by_name.get(match.group(1), match.group(0)), token)
        for token in parsed_command.argv_template
    )
    # ``--account`` is a global latchkey option, so it belongs before the
    # subcommand rather than after the example's own (possibly variadic) arguments.
    return (_ACCOUNT_OPTION, account, *filled_argv)
