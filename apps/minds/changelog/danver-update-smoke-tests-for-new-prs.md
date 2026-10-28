- The smoke journey turns the create form's sharing switch off when it creates its workspace, so its share step publishes from the Share tab. With sharing on by default under an account, the step's click unpublished the workspace and timed out waiting for the publish confirm.

- The smoke journey sends its prompt by typing it into the desktop's launcher and pressing Enter, which delivers it to the workspace's main chat (the welcome chat) and launches that chat's agent. The 0.9 template's launcher no longer offers a "New Chat" row with nothing typed. `e2e_workspace_runner.py` gains `send_to_main_chat_from_launcher` and `find_chat_frame`.

- `apps/minds/docs/testing-overview.md` section 1.10 names the start flow's "I have an account (log in)" aside and takes the dev-checkout build's Node version from `apps/minds/.nvmrc`.
