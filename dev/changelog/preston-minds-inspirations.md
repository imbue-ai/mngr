Added the planning documents for the minds "inspirations" feature under
blueprint/minds-inspirations/: the implementation plan and a concise feature
prompt. Inspirations let a running mind publish a clean, bootable snapshot of
the apps it built to a new GitHub repo, and let another mind adapt one into
itself. The plan records the full design evolution from live testing: assembly
delegated to a launch-task worker on an isolated worktree with a strict
no-merge-back invariant, inline-chat confirmation and chat-surfaced GitHub
device-flow auth (the earlier system_interface popups were removed), a
bespoke-thumbnail gate, and the incident fixes (destructive-merge data loss,
GH_TOKEN shadowing, base-ref resolution on multi-root repos, welcome takeover).
The implementation itself lives in the forever-claude-template repo on the
companion branch of the same name.
