A folder sync whose workspace was not running no longer stays "stopped" until you press "Try again".

- At launch, a remembered sync whose workspace is not running (quitting often stops it) waits for it instead of failing, and starts as soon as Imbue Studio reports the workspace running again, which is usually seconds after launch, when Studio starts it. A sync with a workspace left running while Studio was closed starts at once, as before.

- When a workspace comes back, every sync with it that had stopped is started once more: when Studio sees the workspace's machine start (for any kind of workspace, local or cloud), and when a workspace that stopped answering answers again. A sync you turned off is left off, and a sync that stops again for a reason of its own stays stopped until you retry it.
