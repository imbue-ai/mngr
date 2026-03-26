---
name: reviewer-verify-conversation-enable
description: Enable the conversation review gate in .reviewer/settings.local.json
allowed-tools: Bash(jq *)
---

Run this command:

```bash
jq -n --argjson existing "$(cat .reviewer/settings.local.json 2>/dev/null || echo '{}')" '$existing * {"verify_conversation": {"is_enabled": true}}' > .reviewer/settings.local.json.tmp && mv .reviewer/settings.local.json.tmp .reviewer/settings.local.json
```

Then confirm that the conversation review gate has been enabled.
