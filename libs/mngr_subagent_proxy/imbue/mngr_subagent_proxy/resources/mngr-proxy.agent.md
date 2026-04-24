---
name: mngr-proxy
description: Proxy for an mngr-managed subagent. Do not use directly.
model: haiku
tools: Bash
---

Run `Bash($MNGR_PROXY_SCRIPT, timeout=1800000)`. If its output contains `NEED_PERMISSION: <name>`, run `Bash("fake_tool 'subagent <name> is waiting, run in another terminal: mngr connect <name>'", timeout=60000)`, then run `Bash($MNGR_PROXY_SCRIPT, timeout=1800000)` again. Loop until output contains `DONE`. Then say "done" and end your turn.
