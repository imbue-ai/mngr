The Electron e2e runner starts a chat and opens a terminal from the desktop's launcher menu rather than its retired tiles: it presses the menu's rows (`[data-launch="chat:new"]`, `[data-launch="terminal:new"]`), and the chat's `new` row is the menu's primary free-text row, which with nothing typed starts an empty chat in the chat's pinned window. The glossary lists Getting Started among the built-in apps.

The launcher's field became a text area (Shift+Enter breaks the line), so the runner opens the launcher through `[data-launcher-field] textarea`.
