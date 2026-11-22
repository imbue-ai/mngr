# Remote compatibility

Understanding this behavior corpus calls for the behaviors skill; consult it when reading this file.

This folder specifies how the desktop client behaves against imbue cloud servers deployed after it shipped.
The connector deploys continuously while installed clients update on their own cadence, so an already-released client routinely receives responses whose shape postdates it.
The contract throughout: additive server changes degrade gracefully or invisibly, never break an installed client, and anything the client cannot interpret is shown but not acted on.

The same contract covers a workspace's machine, whose software another of the user's installed clients may have replaced with a build this one cannot drive.
A machine records the *generation* of the software installed on it, a number that changes only when that software stops serving what a client of the generation before sends it.
A machine is *newer* than a client when its generation exceeds the client's own.

Two version markers appear in this folder's behaviors.
A workspace record's *record format* versions the server-visible semantics of a synced workspace record; a secrets blob's *payload format* versions the encrypted payload's contents, which only clients can read.
A record or blob is *too new* for a client when its marker exceeds the newest format that client understands.
