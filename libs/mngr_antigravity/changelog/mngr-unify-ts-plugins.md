Added an end-to-end release test for the antigravity (`agy`) agent on the shared agent
release-lifecycle harness (`imbue.mngr.agents.agent_release_testing`) -- the first e2e
test for this plugin. It seeds the real `~/.gemini` auth (the top-level oauth creds plus
the antigravity-cli token) into the test-redirected HOME so the agent comes up signed in,
and pins a Claude model via the seeded settings (agy's default Gemini model can hit
per-account usage limits). antigravity now joins pi-coding, opencode, and codex on the
shared harness.
