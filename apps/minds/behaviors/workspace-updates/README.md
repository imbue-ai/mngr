# Workspace updates

Understanding this behavior corpus calls for the behaviors skill; consult it when reading this file.

This folder specifies how the desktop client reports that a workspace is running an older workspace template than the app supports, and what it does when asked to fix that.

A workspace's *template version* is the `minds-v*` release of the workspace template it is running.
The app's *supported version* is the template release this build is pinned to; a development build has none.

A workspace is *out of date* only when both versions can be read and the workspace's is on an earlier minor release than the app's; a prerelease belongs to the minor release it precedes.
A workspace whose version sorts below the app's within the same minor release has a *patch available*: it is never prompted for, and is updated only when the user asks for it.
A workspace whose own version sorts below a fixed cutoff (`minds-v0.3.10`) *needs recreation*: no update can be applied to it in place, and the way forward is a new workspace its work is migrated into.
Every other case is *unknown*, and the reading names which side had no version.
Unknown is never a weaker form of out of date.

An update is performed by an agent inside the workspace.
The app dispatches it, reads the status it records, and reports the outcome; the conversation in the workspace is the record of what was done.

An update's *apply step* is the part that lands it.
It deliberately takes the workspace's system services down for longer than the app's threshold for deciding a workspace has stopped responding.

Every update ends with exactly one outcome, recorded by the agent that ran it, or by the workspace when that agent's chat was deleted.
An agent that only stopped, as a workspace restart stops every chat, is started again by the workspace and carries on.
Deleting the update's chat, or telling its agent to stop, is how the user cancels an update under way.
Three of the ways an update can end without changing the workspace are told apart by why it stopped and what happens next.
It is *stuck* when a fault stopped it and the agent running it could not recover from it, or when its chat was deleted before it finished; the update chat, while it exists, says what happened.
It is *refused* when it ran as it should and chose not to apply, the user telling its agent to stop included; nothing is broken, but the update did not land.
It is *waiting for a decision* when the update cannot keep something the user built; the run did its work and kept it for the user's answer, so it is not a failure.
A stuck or refused update, like one that needs recreation, is reported as a failure.
An update waiting for a decision is reported as a decision pending instead, and the user's answer in the update chat decides whether the work it kept lands.
A condition that is not a fault for the run, or one its agent recovers from, never makes an update stuck: e.g. (wlog) an app it cannot reach once it holds the version it was sent, or an agent that comes back when restarted.
