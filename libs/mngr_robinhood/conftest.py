from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.imbue_common.conftest_hooks import register_marker
from imbue.mngr.utils.logging import suppress_warnings

suppress_warnings()

register_marker(
    "sdk_live: live, opt-in integration tests that exercise the Claude Agent SDK "
    "(claude_agent_sdk) against the real API. They cost money, so they are excluded from "
    "every CI run (via `and not sdk_live` in the offload filters) and are only collected "
    "when RUN_SDK_LIVE_TESTS=1 and ANTHROPIC_API_KEY are both set. Run them with "
    "`just test-sdk-live`."
)

register_conftest_hooks(globals())
