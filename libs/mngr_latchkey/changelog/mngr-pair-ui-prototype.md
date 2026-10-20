`POST /permission-requests` now accepts an optional absolute `target`: the permissions file an approved request writes its effect into.

Without it the effect always lands in whichever file the *caller's* extension context names, which is right for an agent asking for something and wrong for the desktop client asking on a workspace's behalf — that put file-sharing grants into the desktop's own admin permissions file, which declares no `latchkey-self` scope schema, so every later request against that file failed its permission check with a 403.

The override is honoured only when the caller's own context is the desktop client's admin permissions file. An agent's context names its own workspace's file and never matches, so one workspace cannot grant itself access through another's.

The rule that only the desktop client may file a permission request against another permissions file is now tested, in both directions: a caller running as itself is refused with a 403, and the desktop client's own context is honoured. That rule is the only thing standing between one workspace's agent and another workspace's permissions, and nothing exercised it.
