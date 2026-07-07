from collections.abc import Collection
from collections.abc import Sequence
from typing import Final

from imbue.imbue_common.pure import pure

# Placeholder substituted for a secret-bearing flag's value when a command is
# rendered for logging.
REDACTED_PLACEHOLDER: Final[str] = "***"


@pure
def redact_secret_flag_values(
    command: Sequence[str],
    *,
    secret_bearing_flags: Sequence[str],
) -> list[str]:
    """Return a copy of command with each secret-bearing flag's value masked for logging.

    Masks both the space-separated form (``--flag value`` -> the ``value``
    token becomes the placeholder) and the joined form (``--flag=value`` ->
    ``--flag=***``). Every occurrence of every listed flag is masked. Used
    only for rendering a command for logs; the real subprocess invocation
    keeps the unredacted command so the child still receives the true values.
    """
    redacted = list(command)
    flags = tuple(secret_bearing_flags)
    # Iterate over the original command so a just-masked value can never be
    # mistaken for a flag on a later pass.
    for idx, token in enumerate(command):
        for flag in flags:
            if token == flag:
                value_idx = idx + 1
                if value_idx < len(redacted):
                    redacted[value_idx] = REDACTED_PLACEHOLDER
            elif token.startswith(f"{flag}="):
                redacted[idx] = f"{flag}={REDACTED_PLACEHOLDER}"
            else:
                # This token is neither the bare flag nor its joined form, so it
                # carries no secret for this flag; leave it untouched.
                pass
    return redacted


@pure
def redact_secret_env_assignments(
    command: Sequence[str],
    *,
    secret_env_var_names: Collection[str],
) -> list[str]:
    """Return a copy of command with secret ``NAME=VALUE`` assignment values masked for logging.

    Masks the value of any ``NAME=VALUE`` token whose ``NAME`` is listed in
    ``secret_env_var_names``, rewriting it to ``NAME=***``. This targets the
    ``--host-env NAME=VALUE`` / ``--env NAME=VALUE`` style used by ``mngr
    create`` (where the assignment is a standalone token following the flag),
    so the flag and the variable name stay visible in the log while only the
    secret value is hidden. Keeping the name means the log still records
    *which* wiring was passed. Used only for rendering a command for logs; the
    real subprocess invocation keeps the unredacted command so the child still
    receives the true values.
    """
    names = frozenset(secret_env_var_names)
    redacted: list[str] = []
    for token in command:
        name, separator, _value = token.partition("=")
        if separator and name in names:
            redacted.append(f"{name}={REDACTED_PLACEHOLDER}")
        else:
            redacted.append(token)
    return redacted
