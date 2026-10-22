"""Redaction of credential values from recorded command output.

A session's commands inherit the test runner's environment, so a command that
prints its environment (``env``, ``printenv``) writes every credential the
runner holds into the transcript. Transcripts are saved as artifacts and
rendered in reports, so the values are redacted before they are written out.

The caller names the variables that hold credentials; nothing is inferred from
variable names. Redaction is then by *value*: every occurrence of a named
variable's value in the text is replaced, whichever variable or command printed
it.
"""

import re
from collections.abc import Iterable
from collections.abc import Mapping


def redact_secret_values(text: str, env: Mapping[str, str], secret_names: Iterable[str]) -> str:
    """Replace every occurrence of each named variable's value in ``text`` with ``<redacted NAME>``.

    Names absent from ``env`` or holding an empty value are skipped (an empty
    pattern would match everywhere). When several variables hold the same
    value, the first name in sorted order labels it, so the output is
    deterministic. Longer values are replaced first, so a credential that
    contains another credential as a substring is labeled by its own name
    rather than torn apart.
    """
    value_to_name: dict[str, str] = {}
    for name in sorted(set(secret_names)):
        value = env.get(name, "")
        if value:
            value_to_name.setdefault(value, name)
    if not value_to_name:
        return text
    values_longest_first = sorted(value_to_name, key=lambda value: len(value), reverse=True)
    pattern = re.compile("|".join(re.escape(value) for value in values_longest_first))
    return pattern.sub(lambda match: f"<redacted {value_to_name[match.group(0)]}>", text)
