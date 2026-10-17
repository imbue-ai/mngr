The desktop app no longer posts its "Notifications enabled" banner — the one
reading "minds will notify you here when a machine needs your attention." It
existed to ask macOS for notification permission, which Electron can only do
by posting a notification, and it fired on every launch and every
notification-settings save.

Nothing needed it. Real notifications are posted whenever you have system
delivery selected, without consulting any stored permission state, and the
first of them prompts the OS for permission by itself. So macOS now asks the
first time a machine actually needs your attention, in context, instead of at
launch.

Two smaller changes come with it. Declining permission no longer switches
your delivery style to in-app cards behind your back — your setting is left
as you set it. And the settings panel's "Open System Settings" button now
stands whenever system notifications are selected, rather than appearing only
when the app believed permission was missing; it never had a reliable way to
know that.
