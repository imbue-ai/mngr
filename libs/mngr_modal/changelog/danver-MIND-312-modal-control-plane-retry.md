A transient Modal failure during discovery no longer fails the command (MIND-312).

`mngr list`, `mngr create` and anything else that discovers Modal hosts read the Modal control plane on every run -- listing sandboxes, reading each sandbox's tags, looking up the app. None of those reads were retried, so a single transient status from Modal produced `Error: Discovery failed for provider 'modal': please contact support@modal.com (Error code: ...)`. Those reads now wait out a blip for up to 60 seconds; see the modal-proxy changelog for the details.

The trade is latency for survival: while Modal is erroring, a command that used to fail in a second can now take up to a minute instead. Each retry logs a warning naming the error and the elapsed budget, so a slow run says why it is slow.
