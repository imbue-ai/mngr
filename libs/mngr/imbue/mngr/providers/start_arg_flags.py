from collections.abc import Sequence

from imbue.imbue_common.pure import pure


@pure
def flag_value_at(start_args: Sequence[str], idx: int, flags: Sequence[str]) -> tuple[str | None, int]:
    """The value one of ``flags`` carries at ``idx`` of a CLI argument list, and how many tokens it spans (0 when not a match).

    Handles ``--flag=value``, ``--flag value``, and for a single-letter flag the
    glued ``-mvalue`` form. A flag that is the last token has no value, so its
    value is None and it spans one token.
    """
    token = start_args[idx]
    for flag in flags:
        if token == flag:
            if idx + 1 < len(start_args):
                return start_args[idx + 1], 2
            return None, 1
        if token.startswith(f"{flag}="):
            return token[len(flag) + 1 :], 1
        is_short_flag = not flag.startswith("--")
        if is_short_flag and token.startswith(flag) and len(token) > len(flag):
            return token[len(flag) :], 1
    return None, 0


@pure
def strip_flags(start_args: Sequence[str], flags: Sequence[str]) -> tuple[str, ...]:
    """``start_args`` without every occurrence of ``flags`` (and their values), in every spelling ``flag_value_at`` reads."""
    kept: list[str] = []
    idx = 0
    while idx < len(start_args):
        _value, span = flag_value_at(start_args, idx, flags)
        if span:
            idx += span
            continue
        kept.append(start_args[idx])
        idx += 1
    return tuple(kept)
