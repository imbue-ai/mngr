The workspace layout doc's `system/supervisord.conf` example shows the terminal program as `terminal-app` (phase 3 of the workspace app model in default-workspace-template turned the terminal into a Python package installed as its own uv tool) instead of the deleted `run_ttyd.sh` launcher script.

The same doc lists `chat` among the tab-openable apps under `system/apps/` and shows its `[program:chat]` program (`chat-app`, which registers from inside the process like the terminal) in the `system/supervisord.conf` example, since phase 10 of the workspace app model runs the chat as its own app and program. Its "How apps register ports" section also shows the manifest form of a registration (`forward_port.py --manifest <app.toml> --url <url>`, which registers under the manifest's name), alongside the `--name` form.

The e2e workspace runner's create-flow budget (`_CREATE_FORM_TIMEOUT_SECONDS` in `e2e_workspace_runner.py`) is 1200 seconds, above the 900 seconds the snapshot script now gives the workspace image's `docker build`, so the container boot after the build still fits inside it.

Phase 6 of the workspace app model (chat as a document, in the paired default-workspace-template branch): every chat renders inside its own frame at the workspace's `chat` origin, so the Electron flow runner (`e2e_workspace_runner.py`) drives the composer and transcript through that frame. `_send_message_and_await_reply` resolves the chat frame first (`_chat_frame`: the workspace frame's child whose URL path is the chat's agent id, waited for within the first-chat timeout) and fills, presses, and waits inside it; the workspace-level selectors it used before would find no chat markup in the shell document any more.

The litellm deployment test (`apps/minds/deployment_tests/test_litellm_via_workspace.py`) drives the workspace's sign-in, accounts, and create-chat routes on the chat app's port (8010) rather than the shell's, following phase 10 of the workspace app model.

The Electron create-and-chat e2e (`test_snapshot_resume.py`), the full workspace flow (`run_full_workspace_flow`, behind `just minds-test-electron-flow`), and the launch-to-message script start the workspace's first chat from the New Tab page's tile (`start_new_chat_from_new_tab` in `e2e_workspace_runner.py`) and drive the provider chooser inside that chat's own frame, since phase 10 of the workspace app model moved the chooser out of the shell: a fresh workspace lands on New Tab with no chat, and the chat minted from the tile waits for an account and shows the chooser in its page.

The launch-to-message script's reload history check and Slack flow read and drive the chat through that same docked frame (`find_docked_chat`, re-resolved each poll pass), rather than through the workspace shell frame, whose text and markup no longer include the chat's.

The e2e workspace runner's terminal step (`_open_terminal`, step 3 of the full workspace flow) opens a terminal from the New Tab page's `terminal:new` tile, the way the chat step does, instead of expecting the dockview add button to open a dropdown with "New terminal", which phase 10 of the workspace app model removed; the New Tab step is shared as `_press_new_tab_tile` and the terminal helper is `open_terminal_from_new_tab`.

The overview doc notes that the workspace's chat is a registered app at its own origin, and that the system interface frames app pages in its tabs.

The workspace template doc's `system/supervisord.conf` example registers the system interface through its manifest (`forward_port.py --manifest system/apps/system_interface/app.toml`), the way the template does.
