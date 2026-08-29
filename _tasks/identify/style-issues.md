# Style issues in the mngr library (identified on 2026-05-11)

Run as part of tkt-run-identify-style-issues-agai-ycnl. Scope: `libs/mngr/imbue/mngr/api/`, `libs/mngr/imbue/mngr/interfaces/`, plus spot checks in `agents/`, `hosts/`, `providers/`. Out of scope per the SKILL: anything already covered by `libs/mngr/imbue/mngr/utils/test_ratchets.py` (TODOs, builtin-exception raises, monkeypatch.setattr, namedtuple, `num` prefix, model_copy, fstring logging, logger.exception, broad exception catch, inline imports, init-file code, cast, assert_isinstance, etc.). Also out of scope: docstrings/comments/doc-code drift (other identify-* tickets).

`non_issues.md` consulted: the "default arguments in api/*.py top-level command functions" exemption applies to one near-miss below; the "missing `is_` prefix" exemption applies to CLI data classes only, not to the internal-function parameter cases noted here.

## 1. Data classes scattered across `api/*.py` files instead of `api/data_types.py`

Description: The style guide says: "Frozen objects should be contained in a file named `data_types.py` at the root of the package. If the file gets too large (> 500 lines), it can be converted to a `data_types` module instead." `imbue/mngr/api/data_types.py` exists (127 lines, well under the split threshold) and contains some result/option models, but many other FrozenModel / MutableModel data classes live in the action-named files alongside their implementing functions:

- `api/agent_addr.py:26` -- `AgentAddress`
- `api/find.py:36, :301, :583` -- `ParsedSourceLocation`, `ResolvedSource`, `AgentMatch`
- `api/exec.py:41, :56, :65, :75` -- `SkippedAgent`, `ExecResult`, `OuterExecResult`, `MultiExecResult`
- `api/list.py:43, :58, :73, :88, :103, :110` -- `ErrorInfo`, `ProviderErrorInfo`, `HostErrorInfo`, `AgentErrorInfo`, `ListResult`, `_ListAgentsParams`
- `api/message.py:34` -- `MessageResult`
- `api/sync.py:86, :112` -- `SyncFilesResult`, `SyncGitResult`
- `api/events.py:57, :86, :100, :110` -- `EventsTarget`, `EventRecord`, `EventSourceInfo`, `_AllEventsStreamState`
- `api/observe.py:218, :317, :324` -- `_TrackedState`, `_KnownHost`, `AgentObserver`

This is structural: callers that want to import e.g. `ExecResult` reach into `api.exec`, which also imports the actual `exec_command_on_agents` function -- so import-pulling the type drags in the implementation module. The style-guide rule exists to prevent circular imports and to keep "what data exists" answerable from one place.

Recommendation: Move all `FrozenModel` definitions in `api/` into `api/data_types.py` (or, if it exceeds 500 lines after the move, into a `api/data_types/` module split by topic -- `exec.py`, `list.py`, `events.py`, etc.). Leave the implementation files importing from there. Treat `MutableModel` results (`ListResult`, `MultiExecResult`, `MessageResult`) the same way. Underscore-prefixed helpers used only within one module (`_ListAgentsParams`, `_TrackedState`, `_KnownHost`, `_AllEventsStreamState`) are arguably file-local state and can stay where they are -- mark that judgement call in the data_types module.

Decision: Accept


## 2. Interface + Implementations sitting inside `api/sync.py`

Description: `api/sync.py` defines `GitContextInterface` (abstract, `MutableModel + ABC`, three abstract methods) and two implementations `LocalGitContext` and `RemoteGitContext`, alongside the sync function itself (api/sync.py:143-282).

The style guide is explicit on this: "If you are creating an interface class, create it in a file named `interfaces.py` at the root of the package (helps avoid circular imports)" and "Implementation classes should be contained within their own named module off of the root of the package (ex: if the package is `foobar` and the interface being implemented is `DatabaseInterface`, then the implementations should go in `foobar.database`)."

So the well-followed pattern would be: declare `GitContextInterface` in `imbue/mngr/interfaces/git_context.py` (or under `interfaces/`), and put the implementations in `imbue/mngr/git_context/local.py` and `.../remote.py`. The function that picks between them (`sync.py:459-463`) just imports from those.

Recommendation: Extract `GitContextInterface` into `imbue/mngr/interfaces/git_context.py` and the two implementations into a new `imbue/mngr/git_context/` module (`local.py`, `remote.py`). Have `api/sync.py` import them. This matches the existing pattern used for `HostInterface` (in `interfaces/host.py`) + `Host` (in `hosts/host.py`).

Decision: Accept


## 3. `logger.info` in library / API code

Description: The style guide says "Reserve `logger.info` for CLI/user-facing code where messages will be shown to users by default. Library and API code should use `logger.debug` for normal operations" and recommends `log_span` for actions that are about to happen. Several `api/*.py` and `providers/*` files violate this:

- `api/connect.py:214` -- `logger.info("Connecting to agent...")`
- `api/connect.py:262` -- `logger.info("Running post-disconnect action: {}", argv)`
- `api/find.py:420` -- `logger.info("Host is offline, starting it...", host_id=..., provider=...)`
- `api/find.py:447` -- `logger.info("Agent {} is stopped, starting it", agent.name)`
- `api/create.py:236, :241, :252` -- `logger.info("Starting agent {} ...", agent.name)` (three near-identical calls in adjacent branches)
- `api/create.py:248` -- `logger.info("Sending initial message...")`
- `api/provision.py:102` -- `logger.info("Provisioned agent: {}", agent.name)`
- `providers/docker/instance.py:907` -- `logger.info("Creating host {} in {} ...", name, self.name)`

These are the `api/` layer (not `cli/`), so per the rule they should be `log_span(...)` blocks or `logger.debug` calls. There may be a legitimate exception: `api/create.py` and `api/connect.py` are entry points called from both the CLI and from agent-management scripts, and the "Starting agent" messages are the primary feedback the user sees during a (sometimes-slow) `mngr create`. If that is intentional, the style guide should explicitly carve out these "user-facing API entrypoints" rather than leaving them in apparent violation.

Recommendation: Two-step:
1. Convert the clear "about to do a thing" calls to `log_span` so timing also shows up at trace level: e.g. `with log_span("Starting agent {}", agent.name): host.start_agents([agent.id])` in the three sibling branches at create.py:236/241/252. These three branches are also a candidate for refactoring into a single helper (they all do "log + start_agents" with the same shape).
2. For provisioning and connect messages that are deliberately visible to the user during a `mngr create`, either move them up to `cli/create.py` and pass the agent reference back through `CreateAgentResult`, OR amend the style guide to permit `logger.info` in `api/` entry points that are routinely invoked from CLI.

Decision: Accept (the refactor) / Defer the style-guide amendment to Josh


## 4. `list[T]` in function input types instead of `Sequence[T]`

Description: The style guide says input parameters should be typed with the immutable abstract collection types: "Use `Sequence[T]` instead of `list[T]`... in inputs". Several places in `api/` declare inputs as `list[T]`:

- `api/cleanup.py:47` -- `def execute_cleanup(..., agents: list[AgentDetails], ...)`
- `api/events.py:342` -- `def _stream_events_from_sources(..., sources: list[EventSourceInfo], ...)`
- `api/events.py:787` -- `def _wait_for_initial_events_then_process(..., all_events: list[EventRecord], ...)`
- `api/events.py:1111` -- `def _setup_event_streaming(..., target_holder: list[EventsTarget], ...)`
- `api/events.py:1118, :1178, :1251` -- `tail_threads: list[threading.Thread]` (three signatures)
- `api/events.py:1245` -- another `target_holder: list[EventsTarget]`

Two distinct sub-cases:

- **Genuine read-only inputs** (`agents`, `sources`, `all_events`): straightforward fix -- change `list[T]` to `Sequence[T]`. Type checker catches accidental mutation.
- **Mutable holders intentionally passed for the callee to push to** (`target_holder`, `tail_threads`): these use the list as a poor man's out-parameter, which is itself a style red flag. The cleaner shape is to return the values from the function and have the caller compose them, or wrap them in a small `MutableModel` (e.g. a `_StreamState`) and pass that. If the holder pattern must remain, leaving it as `list[T]` and adding a brief comment is the lesser evil; the type doesn't really claim immutability there.

Recommendation: Tighten `cleanup.py:47` and the read-only `events.py` parameters to `Sequence[T]` immediately. For the holder cases, file a follow-up to redesign `_setup_event_streaming` / its companions so out-parameter lists are not part of the signature (the function returns a small state model and the caller threads it through).

Decision: Accept


## 5. Identical `logger.info` block repeated three times in `create.py`

Description: `api/create.py:234-253` contains three branches of an `if/elif/else` that each independently emit `logger.info("Starting agent {} ...", agent.name)` and then call `host.start_agents([agent.id])`. The branching point is the initial-message handling (staged vs sent vs none), not whether to start; the start call is shared logic that is being copy-pasted. This is the kind of thing the style guide flags under "Functions and methods should be relatively short (10-50 lines)" / "written in blocks, where each block is prefixed with a comment explaining that block of code". The current branching makes the function harder to read and harder to maintain (a future change to the start log shape must be repeated three times).

Recommendation: Extract a `_start_agent_with_log(agent, host)` private helper that does the log + start. The three branches then just do their message-handling and call the helper.

Decision: Accept (small)


## 6. (Borderline) Style-guide silence on whether `api/` is "library" or "user-facing"

Description: The style guide divides logging by audience: CLI = info, library/API = debug/log_span. mngr's `api/` package is in practice user-facing (`mngr create`, `mngr connect`, `mngr message`, ...) -- it just happens to be implemented as Python functions rather than CLI plumbing. The result is that finding #3 above is recurring across `api/`, suggesting the rule is mis-fitting rather than the code being wrong.

Recommendation: Add a paragraph to the style guide's "Logging" section that names `imbue.mngr.api` as an entry-point layer where `logger.info` is acceptable for major lifecycle steps the user must see during a long-running operation, while reserving `logger.debug` / `log_span` for internal helpers. This codifies what the codebase appears to already do and removes a recurring false-positive style finding for future cleanup passes.

Decision: Defer to Josh (style-guide change rather than code change)
