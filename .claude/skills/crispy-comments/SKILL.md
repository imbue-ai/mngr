---
name: crispy-comments
description: Prune code comments down to what helps future maintainers. Reviews comments on the current branch's diff and removes incidental history, defensive justification, and correctness arguments. Invoke with /crispy-comments.
---

# Crispy Comments

This skill applies only to the current branch of the current repository. If this is invoked on `main` (or `master`), please confirm with the user whether they intend to run this on the entire repository.

Review the comments added or changed in the diff. For each, ask: does this help future maintainers understand the code, or merely restate what the code plainly does or explain today's bug fix? Remove incidental history, defensive justification, and correctness arguments.

Also remove comments that restate facts from the surrounding code that are likely to change — a count of subclasses, a list of variants, the call sites of a function. These pin the comment to a point in time and rot quickly. Follow DRY: Don't Repeat Yourself.

Remove commented-out code outright — version control already remembers it.

Remove ASCII-art banners and box-drawing section dividers. They add visual noise without conveying anything a plain one-line comment does not.
