# Settings

Understanding this behavior corpus calls for the behaviors skill; consult it when reading this file.

*Settings* is the one surface of the desktop client that holds every setting: the app's own, each account's, and each workspace's, sharing included.
It is a flyout of the top bar (specified in `top-bar/`, which also defines the workspace on screen, a workspace or app shared with the account, and a listed share), so it floats over what the window shows, and a workspace on screen stays on screen behind it.
Settings has three *tabs*, and each tab shows a *rail* of entries at its left beside the page of the entry selected.

The Workspace & apps tab speaks for one *pick* at a time: a workspace of the account's own, or a workspace or an app shared with the account through a listed share.
The pick is chosen with the *picker* at the top of that tab's rail, and it need not be the workspace on screen.

Where Settings is -- its tab, the entry selected in each rail, and the pick -- is carried by its address, "/settings" and its query parameters.
A link, a reload, and every control that opens Settings on a place therefore all land on that place by naming it.
A *legacy address* is an address of a surface that Settings replaced.

*Sharing* is the group of pages under a workspace's Sharing entry, one page per share target: the whole workspace, or one of its apps.
What publishing a workspace and granting access do is defined in the [workspace glossary](../../docs/workspace/glossary.md) and is not specified here.

## Out of scope

- The contents of the Studio tab's sections, of an account's entry, and of a workspace's General, Backup, and Updates pages; each belongs to the folder that specifies that behavior (e.g. (wlog) `machine-size/` and `workspace-updates/`).
- What a workspace's permissions grant, and how a permission request is reviewed.
- What publishing and granting do, and what a visitor sees; this folder specifies how the Sharing pages are reached and what each holds.
- Answering an invitation, and the home page's cards for a share -- specified in `home-page/`.
- The switcher shortcut's own settings -- specified in `top-bar/`.
