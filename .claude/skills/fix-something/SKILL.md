---
name: fix-something
argument-hint: [fix_scope] [post_fix_file]
description: Fix a random FIXME in the codebase (in the given scope, use "." for the whole codebase)
---

Your task is to make ONE improvement to the codebase by fixing a random thing about the codebase (eg, normally a FIXME).

In order to make this process easier for you, our FIXMEs are specially formatted:

```python
# FIXME(priority)[attempts=N]: (description)
#  (optional additional context)
```

where `description` is a short description of what needs to be fixed, and `N` is the number of prior attempts made to fix it (if any). 
If there have been no prior attempts, the `[attempts=N]` part may be omitted.
The priority is simply an integer, with 0 being the highest priority. priority may or may not be present. 
If not present, assume priority=3

Before doing any of the below, the first step is to check that the tests are passing by running "uv run pytest"

If the tests are not ALL passing (including all linters, ratchets, type checks, etc), *then this is the thing for you to fix*. In particular, you must follow this process:

1. revert the changes from the previous commit by calling "git revert --no-commit HEAD" (so that the changed are in your current working tree, but not yet committed)
2. update the FIXME that was attempted in the previous commit to increment the attempts count by 1 (if there were no prior attempts, add "[attempts=1]", if there were some prior attempts, increment the number by 1). Note that it should then look something like this: `# FIXME[attempts=1]: (original description)` or `# FIXME0[attempts=2]: (original description)`, ie, the `[attempts=N]` part goes before the ":"
3. extend the optional additional context of the FIXME with a brief note about why you were unable to fix it
4. commit all of this.
5. proceed directly to "# The Final Step" below.

If the tests start out passing, then you must select and fix a random FIXME by following this process:

1. Simply run this bash command, and it will give you a random FIXME line: "./scripts/random_fixme.sh $1". If no lines are returned, then there are no more remaining FIXMEs, so use your "think-of-something-to-fix" skill to come up with something else to fix instead.
2. Find that FIXME and be sure to read the surrounding context (the optional additional context lines below the FIXME line may be important). This is the task you will be working on.
3. Go gather all the context for the library that contains that FIXME (per instructions in CLAUDE.md).
4. Think carefully about how best to fix that FIXME
5. Implement the fix
6. Get all the tests passing (use "uv run pytest" to run them, and be sure to fix any issues, no matter how small)

Once the tests are passing, be sure that you have removed ONLY that FIXME, then commit your changes.

If you were unable to fix the issue and get all the tests passing, do the following instead:

1. Revert any changes you made while attempting to fix the issue
2. Update the FIXME to increment the attempts count by 1 (if there were no prior attempts, add "[attempts=1]", if there were some prior attempts, increment the number by 1). Note that it should then look something like this: `# FIXME[attempts=1]: (original description)` or `# FIXME0[attempts=2]: (original description)`, ie, the `[attempts=N]` part goes before the ":"
3. Extend the optional additional context with a brief note about why you were unable to fix it this time.
4. Commit this updated FIXME (be sure that nothing else is being changed!)

# The Final Step

Finally, as the *very* last thing you do (after committing, regardless of whether you succeeded or not), run this command: "touch $2" (to indicate that you have completed your fix task).
