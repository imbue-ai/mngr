---
name: reviewer-verify-architecture-disable
description: Disable the architecture verification gate in .reviewer/settings.local.json
allowed-tools: Bash(jq *)
---

Run this command:

```bash
jq -n --argjson existing "$(cat .reviewer/settings.local.json 2>/dev/null || echo '{}')" '$existing * {"verify_architecture": {"is_enabled": false}}' > .reviewer/settings.local.json.tmp && mv .reviewer/settings.local.json.tmp .reviewer/settings.local.json
```

Then confirm that the architecture verification gate has been disabled.
