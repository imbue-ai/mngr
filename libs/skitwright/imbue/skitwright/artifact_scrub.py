"""Scrubbing of credential material from files that are about to be published.

CI jobs that run agents upload what the agents produced (transcripts, logs,
rendered reports) as workflow artifacts or mirror it to shared storage. Those
files inherit whatever the job's environment held, so before anything leaves
the runner every text file is scrubbed in place: the values of the variables
the job knows hold credentials are redacted by value (see
:mod:`imbue.skitwright.redaction`), and anything else shaped like a credential
is redacted by pattern. A pattern redaction is reported back, since it means a
credential the job did not know it held got into the files.
"""

import re
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.skitwright.redaction import redact_secret_values


class SecretPattern(FrozenModel):
    """One shape of credential the scrub redacts by pattern."""

    name: str = Field(description="Short identifier, used in the replacement text and reported with every redaction")
    regex: str = Field(description="Regular expression matching the whole credential")


# Anthropic keys of every shape (api03 keys, admin keys, OAuth tokens) share the
# prefix; the body floor keeps short documentation placeholders out.
_ANTHROPIC_KEY: Final[SecretPattern] = SecretPattern(name="anthropic-key", regex=r"sk-ant-[A-Za-z0-9_-]{24,}")
# LiteLLM virtual keys are ``sk-`` plus the 22 URL-safe characters of
# ``secrets.token_urlsafe(16)``; OpenAI keys share the prefix and are longer.
_SK_KEY: Final[SecretPattern] = SecretPattern(name="sk-key", regex=r"\bsk-(?!ant-)[A-Za-z0-9_-]{20,}")
_MODAL_TOKEN: Final[SecretPattern] = SecretPattern(name="modal-token", regex=r"\b(?:ak|as)-[A-Za-z0-9]{20,}\b")
_GITHUB_TOKEN: Final[SecretPattern] = SecretPattern(name="github-token", regex=r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")
_GITHUB_PAT: Final[SecretPattern] = SecretPattern(name="github-pat", regex=r"\bgithub_pat_[A-Za-z0-9_]{80,}\b")
_AWS_ACCESS_KEY_ID: Final[SecretPattern] = SecretPattern(
    name="aws-access-key-id", regex=r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"
)
# A PEM block is redacted whole; the footer is optional so a truncated block
# still loses its header.
_PRIVATE_KEY: Final[SecretPattern] = SecretPattern(
    name="private-key",
    regex=r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----)?",
)

SECRET_PATTERNS: Final[tuple[SecretPattern, ...]] = (
    _ANTHROPIC_KEY,
    _SK_KEY,
    _MODAL_TOKEN,
    _GITHUB_TOKEN,
    _GITHUB_PAT,
    _AWS_ACCESS_KEY_ID,
    _PRIVATE_KEY,
)

# Documentation and test fixtures spell credentials with telltale words or long
# runs of one character; a real credential is random, so neither appears in one.
_PLACEHOLDER_WORDS: Final[tuple[str, ...]] = ("example", "placeholder", "redacted", "dummy", "fake", "test", "xxxx")
_REPEATED_CHARACTER_RUN: Final[re.Pattern[str]] = re.compile(r"(.)\1{7,}")

# How much of a redacted value is shown in a report: enough to recognize it, not
# enough to use it.
_PREVIEW_LENGTH: Final[int] = 8

# The environment variables that list, comma-separated, the names of the
# variables holding credentials: the one mngr's own tooling reads, and the one
# the use-vault-secrets action exports with the names it injected into a job.
SECRET_NAME_LIST_VARS: Final[tuple[str, ...]] = ("MNGR_SECRET_ENV_NAMES", "VAULT_SECRET_ENV_NAMES")


class SecretMatch(FrozenModel):
    """A credential-shaped string that was replaced in some text."""

    pattern_name: str = Field(description="The SecretPattern that matched")
    value: str = Field(description="The replaced text")
    line_number: int = Field(description="1-based line of the text the value started on")


class PatternRedaction(FrozenModel):
    """A credential-shaped string replaced in a file, located and previewed for a report."""

    path: Path = Field(description="The file it was in")
    line_number: int = Field(description="1-based line it was on")
    pattern_name: str = Field(description="The SecretPattern that matched")
    preview: str = Field(description="The first characters of the value, the rest elided")


class ScrubReport(FrozenModel):
    """What scrubbing a set of paths changed."""

    scanned_files: tuple[Path, ...] = Field(description="Every text file that was scrubbed")
    redacted_files: tuple[Path, ...] = Field(description="The scanned files whose contents changed")
    skipped_files: tuple[Path, ...] = Field(description="Files left untouched because they are not UTF-8 text")
    pattern_redactions: tuple[PatternRedaction, ...] = Field(
        description="Credential-shaped strings that no named variable accounted for"
    )


@pure
def secret_names_from_env(env: Mapping[str, str]) -> list[str]:
    """The credential-holding variable names every list variable in ``env`` names, deduplicated and sorted."""
    names: set[str] = set()
    for list_var in SECRET_NAME_LIST_VARS:
        names.update(name for name in env.get(list_var, "").split(",") if name)
    return sorted(names)


@pure
def is_placeholder_credential(value: str) -> bool:
    """Whether ``value`` is spelled like documentation rather than minted.

    Only the first line is judged: the body of a PEM block is base64, which can
    spell a short telltale word by chance and encodes zero bytes as runs of ``A``.
    """
    first_line = value.partition("\n")[0]
    if any(word in first_line.lower() for word in _PLACEHOLDER_WORDS):
        return True
    return _REPEATED_CHARACTER_RUN.search(first_line) is not None


@pure
def redact_secret_like_strings(text: str) -> tuple[str, list[SecretMatch]]:
    """Replace every credential-shaped string in ``text`` with ``<redacted PATTERN>``; returns the text and what was replaced.

    Where matches of different patterns overlap, the earliest (then longest) one wins.
    """
    candidates = sorted(
        (match.start(), -match.end(), pattern_idx)
        for pattern_idx, pattern in enumerate(SECRET_PATTERNS)
        for match in re.finditer(pattern.regex, text)
    )
    matches: list[SecretMatch] = []
    pieces: list[str] = []
    last_end = 0
    for start, negative_end, pattern_idx in candidates:
        end = -negative_end
        value = text[start:end]
        if start < last_end or is_placeholder_credential(value):
            continue
        pattern = SECRET_PATTERNS[pattern_idx]
        matches.append(SecretMatch(pattern_name=pattern.name, value=value, line_number=text.count("\n", 0, start) + 1))
        pieces.append(text[last_end:start])
        pieces.append(f"<redacted {pattern.name}>")
        last_end = end
    pieces.append(text[last_end:])
    return "".join(pieces), matches


@pure
def _preview_of(value: str) -> str:
    return value[:_PREVIEW_LENGTH] + "..."


@pure
def _scrub_text(
    path: Path, text: str, env: Mapping[str, str], secret_names: Sequence[str]
) -> tuple[str, list[PatternRedaction]]:
    """Redact the named values, then anything else credential-shaped."""
    scrubbed, matches = redact_secret_like_strings(redact_secret_values(text, env, secret_names))
    redactions = [
        PatternRedaction(
            path=path,
            line_number=match.line_number,
            pattern_name=match.pattern_name,
            preview=_preview_of(match.value),
        )
        for match in matches
    ]
    return scrubbed, redactions


def _expand_to_files(paths: Sequence[Path]) -> list[Path]:
    """Every regular file under ``paths``, directories walked recursively, missing paths ignored."""
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(candidate for candidate in path.rglob("*") if candidate.is_file()))
        elif path.is_file():
            files.append(path)
        else:
            pass
    return files


def scrub_paths(paths: Sequence[Path], env: Mapping[str, str], secret_names: Iterable[str]) -> ScrubReport:
    """Scrub every text file under ``paths`` in place.

    Files that are not UTF-8 text are left untouched and reported as skipped;
    a credential inside a compressed or binary file cannot be redacted here.
    """
    names = tuple(secret_names)
    scanned: list[Path] = []
    redacted: list[Path] = []
    skipped: list[Path] = []
    pattern_redactions: list[PatternRedaction] = []
    for file_path in _expand_to_files(paths):
        # Bytes in, bytes out, so a file's own line endings survive the rewrite.
        try:
            original = file_path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            skipped.append(file_path)
            continue
        scrubbed, redactions = _scrub_text(file_path, original, env, names)
        if scrubbed != original:
            file_path.write_bytes(scrubbed.encode("utf-8"))
            redacted.append(file_path)
        scanned.append(file_path)
        pattern_redactions.extend(redactions)
    return ScrubReport(
        scanned_files=tuple(scanned),
        redacted_files=tuple(redacted),
        skipped_files=tuple(skipped),
        pattern_redactions=tuple(pattern_redactions),
    )


@pure
def format_pattern_redactions(redactions: Sequence[PatternRedaction]) -> str:
    return "\n".join(
        f"{redaction.path}:{redaction.line_number}: {redaction.pattern_name} {redaction.preview}"
        for redaction in redactions
    )
