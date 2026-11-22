# Top bar

Understanding this behavior corpus calls for the behaviors skill; consult it when reading this file.

The *top bar* is the strip across the top of every main window of the desktop client.
After the window controls, its left side is the *trail*, which says where the window is: the way to the home page, then the window's account, then the workspace the window shows.
Its right side holds the controls that act on what the window shows.
A *switcher* is a button of the trail that names one of those places and opens a menu of the places of its kind, the one it names marked among them: the *account switcher*, whose menu and whose effect on the window are specified in `account-windows/`, and the *workspace switcher*, specified here.

A *flyout* is a surface that hangs from one of the bar's buttons, joined to it, over a dimmed window.
The *workspace on screen* is the workspace the window shows, and it stays the workspace on screen while a flyout or a modal floats over it.

A workspace is the account's *own* when the account owns it.
A workspace is *shared with* the account when another person owns it and a share lets the account open the whole of it, and an *app shared with* the account is one app of another person's workspace that a share lets the account open in its own right.
A share is *listed* once the account has accepted it, or when it is a link the account pasted; a share the account has not answered, or has declined, is an invitation, which `home-page/` specifies.
The switchers and Settings show listed shares only.

The *switcher shortcut* is the key chord that opens the workspace switcher's menu.
The desktop app matches it wherever keyboard focus is, inside the workspace included, and lets the user change it.
A plain browser has no such reach: there the chord is fixed, and works only while keyboard focus is on the app's own page.

## Out of scope

- The account switcher's menu, and where a switch of account lands -- specified in `account-windows/`.
- What Settings shows once it is open -- specified in `settings/`.
- The contents of the notification feed and of Get help -- specified in `notifications/` and `get-help/`.
- The status of a workspace's update, which the bar shows after the workspace switcher -- specified in `workspace-updates/`.
- The bar of the first-run start flow and the bar of a pulled-out window, neither of which has a trail.
