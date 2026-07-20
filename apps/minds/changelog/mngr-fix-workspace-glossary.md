Fixed: documentation drift around what a workspace is. The glossary described a workspace as a single mngr *agent* labeled `workspace=<name>`, but a workspace is a mngr *host* holding several agents, and the `workspace` label was removed some time ago (discovery keys off `is_primary`).

The glossary now defines a workspace as a host, and adds entries for the four agent kinds that live in one: the primary `system-services` agent, chat agents, worktree agents, and worker agents.

Fixed: the launch mode glossary entry listed a nonexistent `CLOUD` mode (the real one is `VULTR`) and omitted `AWS` and `MODAL`.

Fixed: `design.md` and `user_story.md` showed a `mngr create` command with the removed `--label workspace=<name>` flag and no `--new-host`, so following them would not reproduce what minds actually runs.
