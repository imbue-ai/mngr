Fixed a macOS bug where approving (or denying) a latchkey permission request could silently fail to notify the waiting agent, leaving it blocked with no confirmation in the chat.

The in-app `mngr` caller forks a child from a pre-warmed forkserver; on macOS that child crashed (SIGSEGV) when `mngr` startup probed the system HTTP proxy, because the underlying Apple frameworks are not fork-safe. The proxy configuration is now resolved once in the parent process and reused in the child, so the child never makes the fork-unsafe call.

Permission/file-sharing nudges are now also confirmed and retried: delivery is verified from `mngr message`'s structured output and retried within a short budget, so a transient failure no longer drops the notification silently.
