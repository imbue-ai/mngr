This is the merge of `minh/auth-provider-lanes` (fixing the workspace deployment tests for the provider-accounts sign-in) into current `main`; see that branch's `minh-auth-provider-lanes.md` entry for the bulk of what changed.

On top of the merge, this PR fixes the merged branch's chat-account binding check in `test_litellm_via_workspace.py`: its in-container script parsed `mngr list --format json` as a bare list and read a `state_dir` field the rows do not carry, so the assertion could never pass. The state dir is now resolved via `mngr config get default_host_dir` plus mngr's `agents/<id>` layout.

It also deduplicates the two lane tests' workspace bring-up/teardown into a `_local_docker_workspace` context manager (which now also owns the template clone whose scratch dir it removes) plus a shared env-ready/skip preamble helper, and refreshes docstrings that still described the pre-provider-accounts sign-in (shared Claude settings write + agent restart) -- including the Electron test's in `test_snapshot_resume.py`.

It also ports `scripts/launch_to_msg_e2e.py`'s sign-in step to the provider chooser: the script still drove the deleted `.claude-login-modal` UI, so the scheduled launch-to-msg health check would have hung at sign-in once the template's chooser landed on main. The helper now mirrors `test_snapshot_resume._sign_in_with_api_key_via_modal`'s `data-e2e` selectors.

Finally, it refreshes `apps/minds/README.md`'s workspace description for the provider-accounts world: chats are created on demand and bind to a provider account (a folder under `~/.minds/accounts`) at creation, rather than a bootstrap-created `Chat-1` on first boot sharing a workspace-wide `~/.claude`; the first-boot initial-chat bullet (gated by `data/.state/initial_chat_created`) is gone with the boot chat itself.

The lane tests' binding check also requires the credential symlink's target to exist: a dangling symlink into the account folder (the codex keyring failure mode the check documents) no longer counts as bound.

Remaining apps/minds documentation that still described the deleted Claude sign-in modal or the boot-created chat is refreshed for the chooser world: `docs/testing-overview.md`'s Electron-test row, `desktop_client/e2e_workspace_runner.py`'s comments and chat-input wait log, `desktop_client/laptop_agent_types_seed.py`'s chat-type description, and the `ai_keys.py` / `agent_creator_test.py` sign-in surface mentions.
