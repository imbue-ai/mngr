No behavior change. This provider still reports an agent blocked on a tool-approval dialog (or a question put to the user) as `RUNNING` rather than `WAITING`, where local, docker, ssh and lima agents now report `WAITING`.

Its listing builds agent state from a batched script that stats marker files by fixed name and has no agent object to ask, so it cannot see a per-agent-type block signal. It now says so explicitly at the call site rather than by omission.
