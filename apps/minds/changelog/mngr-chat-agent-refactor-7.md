Paired branch for phase 7 of the default-workspace-template's chat-agent split (`docs/system/blueprint/chat-agent-split/plan-chat-agent-split.md` there), the cleanup: the chat app's `/api/agents/...` aliases are gone, every new chat is named "Chat N", and the browser fleet addresses the chat that holds a browser.

- The LiteLLM-via-workspace deployment test creates its chat through `POST /api/chats/create` and reads the chat's id from `chat_id`. It is a release test that mints a real cloud environment, so it was edited but not run here.

- Merge after the template is tagged, as the app-model arc's paired branch did.
