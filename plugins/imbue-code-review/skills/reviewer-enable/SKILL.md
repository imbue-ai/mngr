---
name: reviewer-enable
description: Enable the code review stop hook in .reviewer/settings.local.json. Optionally takes a shell expression for when to enforce (defaults to always).
argument-hint: [shell_expression]
allowed-tools: Bash(jq *)
---

The user may provide a shell expression as an argument. If provided, use that as the `enabled_when` value. If not provided, default to `"true"` (always enforce).

Examples of expressions the user might provide:
- `true` -- always enforce
- `test -n "${CI:-}"` -- only in CI environments
- `test "$(git rev-parse --abbrev-ref HEAD)" != "main"` -- only on feature branches

Run this command, substituting the expression:

```bash
jq -n --argjson existing "$(cat .reviewer/settings.local.json 2>/dev/null || echo '{}')" --arg expr "<expression>" '$existing * {"stop_hook": {"enabled_when": $expr}}' > .reviewer/settings.local.json.tmp && mv .reviewer/settings.local.json.tmp .reviewer/settings.local.json
```

Then confirm that the stop hook has been enabled and show the configured expression.
