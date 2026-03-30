---
name: reviewer-init-categories
description: Copy the default issue categories to .reviewer/ for customization. Use when you want to edit the code review or conversation review categories for your project.
allowed-tools: Bash(cp *), Bash(mkdir *), Read
---

Copy the default issue category files to `.reviewer/` so you can customize them for your project.

1. Create the directory if needed: `mkdir -p .reviewer`
2. Copy the defaults:
   - `cp ${CLAUDE_PLUGIN_ROOT}/agents/categories/code-issue-categories.md .reviewer/code-issue-categories.md`
   - `cp ${CLAUDE_PLUGIN_ROOT}/agents/categories/conversation-issue-categories.md .reviewer/conversation-issue-categories.md`

If the files already exist in `.reviewer/`, ask the user whether to overwrite them.

After copying, confirm that the files are in place and let the user know they can edit them directly. The autofix and verify-conversation skills will automatically use the `.reviewer/` versions when they exist.
