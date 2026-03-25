---
name: reviewer-autofix-ignore-minor-issues
description: Configure autofix to only fix MAJOR and CRITICAL issues (unattended mode)
allowed-tools: Bash(jq *)
---

Run this command:

```bash
jq -n --argjson existing "$(cat .reviewer/settings.local.json 2>/dev/null || echo '{}')" '$existing * {"autofix": {"append_to_prompt": "Please autofix as normal, except: 1. Never ask questions. You are running unattended and the user is not there to answer your questions. Instead, think hard about whether to accept each given patch. If you decide *not* to accept it, then create a *new* branch with that fix commit. Call the branch (current_branch_name)___(fix_description) *and be sure to push it remotely* then by sure to check the normal branch back out when you'\''re done. Also be sure to tell the user that you did this.  2. You only *have* to fix MAJOR and CRITICAL issues. If there are issues that you do NOT fix, append the json object(s) for those error(s) that were *not* fixed into ~/temp/issues/<current-git-hash>.jsonl  If so, be sure to mention those issues in your final summary as well."}}' > .reviewer/settings.local.json.tmp && mv .reviewer/settings.local.json.tmp .reviewer/settings.local.json
```

Then confirm that autofix has been configured to only fix MAJOR and CRITICAL issues.
