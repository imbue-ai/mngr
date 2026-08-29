# Workspace updates

This folder specifies how the desktop client reports that a workspace is running an older workspace template than the app supports, and what it does when asked to fix that.

A workspace's *template version* is the `minds-v*` release of the workspace template it is running.
The app's *supported version* is the template release this build is pinned to; a development build has none.

A workspace is *out of date* only when both versions can be read and the workspace's sorts below the app's.
A workspace whose own version sorts below a fixed cutoff (`minds-v0.3.10`) *needs recreation*: no update can be applied to it in place, and the way forward is a new workspace its work is migrated into.
Every other case is *unknown*, and the reading names which side had no version.
Unknown is never a weaker form of out of date.

An update is performed by an agent inside the workspace.
The app dispatches it, reads the status it records, and reports the outcome; the conversation in the workspace is the record of what was done.

An update's *apply step* is the part that lands it.
It deliberately takes the workspace's system services down for longer than the app's threshold for deciding a workspace has stopped responding.
