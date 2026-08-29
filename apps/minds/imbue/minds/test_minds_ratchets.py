"""Project-specific ratchets for the minds app.

Lives outside ``test_ratchets.py`` because that file must define the same
test set across every project (enforced by ``test_meta_ratchets.py``).
"""

from pathlib import Path

import pytest
from inline_snapshot import snapshot

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
