"""Project-specific ratchets for the minds app.

Lives outside ``test_ratchets.py`` because that file must define the same
test set across every project (enforced by ``test_meta_ratchets.py``).
"""

import re
from pathlib import Path
from typing import Final

import pytest
from inline_snapshot import snapshot

from imbue.imbue_common.pure import pure
from imbue.imbue_common.ratchet_testing.common_ratchets import RatchetRuleInfo
from imbue.imbue_common.ratchet_testing.core import FileExtension
from imbue.imbue_common.ratchet_testing.core import RegexPattern
from imbue.imbue_common.ratchet_testing.core import check_regex_ratchet

_DIR = Path(__file__).parent.parent.parent

pytestmark = pytest.mark.xdist_group(name="ratchets")

_RAW_POST_MESSAGE_RULE = RatchetRuleInfo(
    rule_name="raw postMessage / message-listener usages outside the embed contract",
    rule_description=(
        "All chrome<->workspace (and chrome<->modal-iframe) messaging must flow through the embed "
        "contract module (desktop_client/static/embed_contract.js) or the shell bridge it backs, so "
        "the whole message surface stays in one auditable place with the contract's source checks "
        "and payload validation applied (see docs/embed-contract.md). Do not call postMessage or "
        "register 'message' listeners directly -- extend the contract instead."
    ),
)

# Allowlist-by-file: any NEW file that touches the raw primitives fails
# immediately (the snapshot is pinned at 0 with these exclusions).
_ALLOWED_POST_MESSAGE_FILES = (
    # The contract itself: the one sanctioned home for the primitives.
    "embed_contract.js",
    # Vendored third-party bundle (Sentry's browser SDK).
    "sentry.browser.min.js",
    # Tests stand in windows/listeners to exercise the boundary itself.
    "test/unit/*",
    "test/e2e/*",
)

_POST_MESSAGE_PATTERN = RegexPattern(r"""postMessage\(|addEventListener\(\s*["']message["']""", multiline=False)


def test_prevent_raw_post_message_outside_embed_contract() -> None:
    chunks = []
    for extension in (".js", ".html", ".jinja"):
        chunks.extend(
            check_regex_ratchet(_DIR, FileExtension(extension), _POST_MESSAGE_PATTERN, _ALLOWED_POST_MESSAGE_FILES)
        )
    assert len(chunks) <= snapshot(0), _RAW_POST_MESSAGE_RULE.format_failure(tuple(chunks))


_HOST_LIFECYCLE_ARGV_RULE = RatchetRuleInfo(
    rule_name="mngr host start/stop argv assembled outside the shared host action",
    rule_description=(
        "A host start or stop must go through perform_mind_host_action (desktop_client/"
        "workspace_lifecycle.py), which is what keeps the optimistic host-state override and the "
        "unattended-recovery marks in step with the machine. A route that assembles its own argv "
        "skips both by omission: a start that does not clear the intentional-stop mark leaves the "
        "machine excluded from unattended recovery for the rest of the process's life, and nothing "
        "else can clear it -- the only other clear is a successful probe, and the probe loop polls "
        "agents that are already unhealthy. Call the shared action instead of shelling out."
    ),
)

_ALLOWED_HOST_LIFECYCLE_FILES = (
    # The shared action itself: the one sanctioned home for these argvs.
    "workspace_lifecycle.py",
    # The recovery worker drives the health lifecycle directly (mark_recovering /
    # record_probe_success), so it owns the marks the shared action would set.
    "workspace_recovery.py",
    # Quit-time bulk stop: one mngr call over many agents, which the per-workspace
    # action cannot express. It marks each agent itself.
    "desktop_control.py",
    # Not host lifecycle at all: this module is python3 script text that runs
    # *inside* an already-running container, and its `mngr stop` stops a chat
    # agent there. There is no host to override the state of and no recovery
    # mark to keep in step, and the module holds no desktop-side code that
    # could ever acquire one.
    "backup_workspace_scripts.py",
)

# Both shapes a host lifecycle call takes: a subprocess argv led by the resolved binary,
# and an MngrCaller argv, which omits it.
_HOST_LIFECYCLE_ARGV_PATTERN = RegexPattern(
    r"""mngr_binary,\s*["'](?:start|stop)["']|\[\s*["'](?:start|stop)["']\s*,""", multiline=False
)


def test_prevent_host_lifecycle_argv_outside_the_shared_action() -> None:
    chunks = check_regex_ratchet(
        _DIR, FileExtension(".py"), _HOST_LIFECYCLE_ARGV_PATTERN, _ALLOWED_HOST_LIFECYCLE_FILES
    )
    assert len(chunks) <= snapshot(0), _HOST_LIFECYCLE_ARGV_RULE.format_failure(tuple(chunks))


_NEXT_DEPLOY_CHECKLIST_PATH: Final[Path] = _DIR / "docs" / "deploy" / "next_deploy.md"
# Generous for a checklist; a rollout's night-by-night narrative belongs in
# docs/deploy/history/rollouts/, not here.
_NEXT_DEPLOY_MAX_LINE_COUNT: Final[int] = 400
_SENTENCE_END_CHARACTERS: Final[str] = ".:?!"
_TRAILING_MARKDOWN_EMPHASIS: Final[str] = "*_`"
_MID_LINE_SENTENCE_BREAK: Final[re.Pattern[str]] = re.compile(r"[.!?]\s+[A-Z]")
_INLINE_CODE_SPAN: Final[re.Pattern[str]] = re.compile(r"`[^`]*`")
_MARKDOWN_LINK_TARGET: Final[re.Pattern[str]] = re.compile(r"\]\([^)]*\)")
_CHECKED_CHECKLIST_ITEM: Final[re.Pattern[str]] = re.compile(r"^[-*]\s+\[[xX]\]")
_VAULT_TOKEN_FILE_REFERENCE: Final[re.Pattern[str]] = re.compile(r"vault[-_]?tokens?/|\.vault-token|\.token\b")
_CODE_FENCE: Final[str] = "```"

_NEXT_DEPLOY_CHECKLIST_RULE: Final[RatchetRuleInfo] = RatchetRuleInfo(
    rule_name="next_deploy.md drifting from a one-sentence-per-line checklist",
    rule_description=(
        "apps/minds/docs/deploy/next_deploy.md is the queue for the next deployment, not an archive. "
        "Its preamble states the rules this test enforces: one sentence per line (so a diff shows the "
        "sentence that changed), every prose line ending a sentence, no checked items (a shipped item "
        "belongs in that release's history entry), a hard size cap (a rollout's night-by-night log "
        "belongs in docs/deploy/history/rollouts/), a `Last reset:` stamp, and no Vault token file "
        "named (token locations are machine-specific)."
    ),
)


@pure
def _next_deploy_prose_lines(text: str) -> tuple[tuple[int, str], ...]:
    """Every line that must read as one sentence: not blank, not a heading, not a table row, not inside a code fence."""
    prose_lines: list[tuple[int, str]] = []
    is_inside_code_fence = False
    for line_idx, line in enumerate(text.splitlines(), start=1):
        if line.strip().startswith(_CODE_FENCE):
            is_inside_code_fence = not is_inside_code_fence
            continue
        if is_inside_code_fence or not line.strip() or line.startswith("#") or line.lstrip().startswith("|"):
            continue
        prose_lines.append((line_idx, line))
    return tuple(prose_lines)


@pure
def _next_deploy_checklist_problems(text: str) -> tuple[str, ...]:
    problems: list[str] = []
    lines = text.splitlines()
    if not any(line.startswith("Last reset:") for line in lines):
        problems.append("no `Last reset:` line; the reset at the end of a release stamps one")
    if len(lines) > _NEXT_DEPLOY_MAX_LINE_COUNT:
        problems.append(
            f"{len(lines)} lines exceeds the {_NEXT_DEPLOY_MAX_LINE_COUNT}-line cap; move narrative to history/"
        )
    for line_idx, line in _next_deploy_prose_lines(text):
        if _CHECKED_CHECKLIST_ITEM.match(line.lstrip()):
            problems.append(f"line {line_idx}: a checked item belongs in a history entry, not here")
        if line.rstrip().rstrip(_TRAILING_MARKDOWN_EMPHASIS)[-1:] not in _SENTENCE_END_CHARACTERS:
            problems.append(f"line {line_idx}: does not end a sentence (wrap each sentence on its own line)")
        without_code_or_links = _INLINE_CODE_SPAN.sub("", _MARKDOWN_LINK_TARGET.sub("]", line))
        if _MID_LINE_SENTENCE_BREAK.search(without_code_or_links):
            problems.append(f"line {line_idx}: more than one sentence on the line")
        if _VAULT_TOKEN_FILE_REFERENCE.search(line):
            problems.append(f"line {line_idx}: names a Vault token file; token locations are machine-specific")
    return tuple(problems)


def test_next_deploy_checklist_stays_a_checklist() -> None:
    if not _NEXT_DEPLOY_CHECKLIST_PATH.exists():
        pytest.skip("apps/minds/docs/deploy is excluded from the public mirror")
    problems = _next_deploy_checklist_problems(_NEXT_DEPLOY_CHECKLIST_PATH.read_text())
    failure_message = "\n".join(
        (_NEXT_DEPLOY_CHECKLIST_RULE.rule_name, _NEXT_DEPLOY_CHECKLIST_RULE.rule_description, *problems)
    )
    assert problems == (), failure_message
