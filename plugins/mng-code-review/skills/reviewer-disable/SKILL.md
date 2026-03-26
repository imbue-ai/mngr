---
name: reviewer-disable
description: Disable all review gates (autofix, CI, conversation review, and architecture verification) in .reviewer/settings.local.json
allowed-tools: Bash(jq *)
---

Run this command:

```bash
jq -n --argjson existing "$(cat .reviewer/settings.local.json 2>/dev/null || echo '{}')" '$existing * {"autofix": {"is_enabled": false}, "ci": {"is_enabled": false}, "verify_conversation": {"is_enabled": false}, "verify_architecture": {"is_enabled": false}}' > .reviewer/settings.local.json.tmp && mv .reviewer/settings.local.json.tmp .reviewer/settings.local.json
```

Then confirm that all review gates have been disabled.
