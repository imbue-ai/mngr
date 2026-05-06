"""Constants shared across the host-pool lease flow.

The host-pool flow has two halves living in two different packages, and they
have to agree on a single placeholder value:

- ``apps/remote_service_connector/scripts/create_pool_hosts.py`` writes this
  placeholder into a pool host's host-level env file at provision time so
  that Claude Code's startup-time API-key validation accepts the agent's
  first boot.
- ``apps/minds/imbue/minds/desktop_client/agent_creator.py`` overwrites
  that entry with a real LiteLLM virtual key during lease setup.

Both halves need the same value, so it lives here in imbue_common.
"""

from typing import Final

# Must look like a real Anthropic API key (correct ``sk-ant-api03-`` prefix
# and a realistic length, ~108 chars) so Claude Code's validation accepts
# it during the agent's first boot. The trailing ``0``s are deliberately
# distinctive so the placeholder is easy to spot in env files / proc env.
PLACEHOLDER_ANTHROPIC_API_KEY: Final[str] = (
    "sk-ant-api03-PLACEHOLDER000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
)
