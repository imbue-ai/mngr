---
description: Investigate the architectural choices of a branch and highlight concerns.
---

look at the /autofix skill in ~/hammer-verify; im writing this as instructions to the subagent but it should actually be framed by instructions to the top level agent

the base branch is by default main, but if any other feature branches were merged in during the session, it should probably be that feature branch. you should diff your current changes vs the base branch and have a subagent skim to see if they look right; if they include changes that seem to not be yours, STOP and ask the user what the base branch is. then the top level agent should give the subagent a worktree checked out on the base branch (with detatched head).

input to this skill (from the top level agent) should include a CONCISE description of the problem that the branch was trying to solve. it should say NOTHING about the 'how' - it should only say the desired behavior for a feature/current bad behavior for a bugfix/code-level problem e.g. 'the code in <> has a confusing inheritance structure' for a refactor, etc.
also it should have the hash of the tip of the feature branch so it can also check that out (with detached head) or diff against the base branch


then for the subagent:

1. read the BASE BRANCH code thoroughly (basically take this from verify-branch.md, e.g. it has 
Also be sure to:
- Understand the existing codebase patterns around the changed files
- Read any relevant instruction files (CLAUDE.md, style_guide.md) that might apply to the changed code

but also it should explicitly mention architecture docs as well)

2. think of at least 3, but prefereably more, top-level approaches to address the problem. each approach should have a medium level of detail - think one or two paragraphs.

3. read the diff/new code thoroughly (check out the feature branch and/or diff it against the base branch)

4. map out the 'how' of the fix at the architectural level - what new functions/classes/dependencies/etc does it introduce? here you should pay attention to the overall 'structure' and 'information flow' - the new objects that are defined, any new imports between parts of the codebase that didn't have a direct dpeendency before, and ESPECIALLY side-information (any new files, env vars, or other types of global state (including e.g. dependencies on the current time - anything that's not @pure (editor, fix this to not say @pure because at this point not everything will be marked with that yet. just put a description of what @pure does))). 

5. examine how the changes integrate with existing code; do they match the way similar things are already done in the codebase? ARE there similar things already done in teh codebase? (even if it's a reasonable thing to do, it's important to note anything that is quite different from teh existing code)

6. think about how the changes relate to the original approaches you suggested; does it match one of them? is there anything it does that seems 'weird' compared to your original suggestions? (note this even if there is good justification for why it's 'weird') does it fully solve the problem at the root, or just patch it (or only solve part of it)?

7. think on a high level about the diff - is this 'the right way' to achieve the original goal? is there another method that's more in line with the style guide and existing code patterns/better respects existing information flow/uses less side information/etc?

8. report back to the top level agent with a detailed report of your findings from 4-6; include
  - the architectural strategy + information flow of the changes from step 4
  - any ways the code deviates from how similar things are done / is unlike things that are done from step 5
  - any notes on how the code is 'weird' from step 6
  - high-level thoughts from step 7

then the top-level agent should return a concise version of the report to the user, focusing on the results from steps 5-7