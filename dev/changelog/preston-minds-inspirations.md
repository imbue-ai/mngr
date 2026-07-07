Added the planning documents for the minds "inspirations" feature under
blueprint/minds-inspirations/: the implementation plan and a concise feature
prompt. Inspirations let a running mind publish a clean, bootable snapshot of
the apps it built to a new GitHub repo, and let another mind adapt one into
itself. The plan records the full design evolution from live testing: assembly
delegated to a launch-task worker on an isolated worktree with a strict
no-merge-back invariant, inline-chat confirmation, latchkey GitHub
permissioning end-to-end -- REST API calls via latchkey curl and the git push
through the latchkey gateway's native git smart-HTTP proxying (the earlier
system_interface popups, gh device flow, and interim GH_TOKEN-authenticated
push were all removed) -- a bespoke-thumbnail gate, and the incident fixes
(destructive-merge data loss, GH_TOKEN shadowing, base-ref resolution on
multi-root repos, welcome takeover).
The implementation itself lives in the forever-claude-template repo on the
companion branch of the same name.
