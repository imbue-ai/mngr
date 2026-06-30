# Inconsistencies in the mngr library (identified on 2026-05-11)

Run as part of tkt-run-identify-inconsistencies-a-k81i. Scope: `libs/mngr/imbue/mngr/api/`, `libs/mngr/imbue/mngr/interfaces/` (sampled), supporting reads in `libs/mngr/imbue/mngr/primitives.py`. Out of scope per the SKILL: docstrings, comments, doc-vs-code (those belong to tkt-run-identify-outdated-docstrin-fv1z/tkt-run-identify-doc-code-disagree-9xmt). Sampling pass, not exhaustive; subsequent runs can broaden to providers/, hosts/, agents/, plugins.

`non_issues.md` consulted; the "missing 'is_' prefix in CLI functions and data classes" exemption applies to public CLI surfaces but not to internal cross-function API parameters where this pass found drift.

## 1. `dry_run` (gc.py) vs `is_dry_run` (everywhere else)

Description: Within `libs/mngr/imbue/mngr/api/`, the dry-run parameter is named `is_dry_run` in `cleanup.py`, `pull.py`, `push.py`, and `sync.py` (~20 functions and Field declarations total). `gc.py` is the lone deviant -- all 11 of its public/internal signatures use bare `dry_run` (`gc.py:49`, `:183`, `:208`, `:266`, `:300`, `:436`, `:507`, `:562`, `:644`, `:735`, plus the inner kwargs at `:80`, `:92`, `:103`, `:114`, `:126`, `:138`). Trace log at `:66` even uses `dry_run={}`. The two styles are not wired together by any public callsite that I could find, so renaming gc.py's `dry_run` -> `is_dry_run` should be a mechanical change.

Recommendation: Rename `dry_run` to `is_dry_run` throughout `libs/mngr/imbue/mngr/api/gc.py` (and any callers/tests). Internal-only parameters; no CLI surface change needed.

Decision: Accept


## 2. Three different shapes for "errors from a multi-target operation"

Description: API result types disagree on how to represent per-target errors. Across `libs/mngr/imbue/mngr/api/`:

- **Typed model hierarchy** -- `list.py` defines `ErrorInfo`/`ProviderErrorInfo`/`HostErrorInfo`/`AgentErrorInfo` (all FrozenModel) with explicit `exception_type`, `message`, and the resource id, and `ListResult.errors: list[ErrorInfo]` (list.py:43-107).
- **Untyped 2-tuple** -- `message.py:MessageResult.failed_agents: list[tuple[str, str]]` with the comment "List of (agent_name, error_message) tuples" (message.py:40-42); same shape in `exec.py:MultiExecResult.failed_agents: list[tuple[str, str]]` (exec.py:81-84).
- **Plain string list** -- `data_types.py:GcResult.errors: list[str]` (data_types.py:107-110) and `data_types.py:CleanupResult.errors: list[str]` (data_types.py:124-127). Build-site code constructs the string by f-stringing the exception in-line, then logs and appends (e.g. cleanup.py:125, gc.py:971).

Same concept ("things that went wrong on individual targets while a bulk operation continued"), three incompatible representations. Downstream consumers (CLI formatters, tests, plugins) have to special-case each shape. Tuples lose the exception type; strings lose both type and resource identity.

Recommendation: Standardize on the list.py model hierarchy. Move `ErrorInfo` and its subclasses out of `list.py` into `data_types.py` (or `interfaces/data_types.py`) so all five result types can refer to them. Replace `failed_agents: list[tuple[str, str]]` with `failed_agents: list[AgentErrorInfo]` (using AgentName-typed identifiers per inconsistency #3 below). Replace `errors: list[str]` in GcResult/CleanupResult with `list[ErrorInfo]` or a richer subclass. This is a breaking API change for any in-tree consumers; deferring as one PR per result type may be easier to land.

Decision: Accept


## 3. `list[AgentName]` vs `list[str]` for what is semantically the same list

Description: Result fields that store collections of agent names disagree on typing. `data_types.py:CleanupResult` declares `destroyed_agents: list[AgentName]` and `stopped_agents: list[AgentName]` (data_types.py:116, 120), correctly using the project's `AgentName` newtype. `message.py:MessageResult.successful_agents: list[str]` (message.py:37-39) flattens the same concept to plain strings, even though the implementing call site at message.py:279 starts from `agent_name = str(agent.name)` -- i.e. it deliberately strips the type. The `failed_agents` tuples in MessageResult and MultiExecResult have the same problem (the agent name half is `str`).

This weakens the type checker: callers receiving a `MessageResult` cannot use type-system guarantees about valid agent names, while callers receiving a `CleanupResult` can. It also forces converters at every consumer that wants to feed a result into anything taking `AgentName`.

Recommendation: Change `MessageResult.successful_agents` to `list[AgentName]` and update the constructor sites in message.py to not stringify (i.e. `result.successful_agents.append(agent.name)` instead of `result.successful_agents.append(agent_name)`). Same treatment for any agent-name field in `MultiExecResult` once the failed_agents shape is fixed per #2.

Decision: Accept


## 4. Error logging idiom split: `logger.warning(str_with_e)` vs `logger.opt(exception=e).error(...)`

Description: Two patterns coexist for the same kind of caught exception (a per-target failure during async fan-out, where the exception is recorded in the result and the work continues unless `ErrorBehavior.ABORT`).

- `logger.warning(error_msg)` where `error_msg = f"Error ...: {e}"`: cleanup.py:126, :142, :150, :180, :189, :195, :229; create.py:112, :376; message.py (host-offline warn at line 104, error access warn at line 238). This pattern stringifies the exception, losing the traceback.
- `logger.opt(exception=e).error(...)`: list.py:265, :482, :570; discovery_events.py:733, :828, :922; gc.py:971; observe.py:393. This preserves the full chained traceback in the log output.

Both forms are caught at structurally similar sites (provider discovery error, host processing error, single-agent processing error). The `.opt(exception=e).error(...)` form is strictly more informative; the `.warning("...{}".format(e))` form makes post-mortem debugging from logs harder.

Severity choice (warning vs error) is also inconsistent across these sites for what looks like the same kind of "we caught a per-target failure and recorded it" situation.

Recommendation: Adopt `logger.opt(exception=e).error("...")` as the standard for caught per-target exceptions across `api/`. Reserve `logger.warning(...)` for known-recoverable situations where there is no exception object to attach (e.g. "host is offline" branches that are reached before any exception is raised). Update the call sites listed above. As a soft secondary, decide whether per-target ABORT-mode failures should be `.error` and CONTINUE-mode failures `.warning`, or all `.error` -- and apply uniformly.

Decision: Accept


## 5. `all_agents` (message.py) vs `is_all` (exec.py) for the "match-everything" boolean

Description: Two CLI-level fan-out APIs accept a "do this to all matching agents" flag:

- `message.py:send_message_to_agents(..., all_agents: bool = False, ...)` (message.py:54), threaded through `_process_host_for_messaging(..., all_agents: bool, ...)` (message.py:142).
- `exec.py:exec_command_on_outer_hosts(..., is_all: bool, ...)` (exec.py:312) and `exec.py:exec_command_on_agents(..., is_all: bool, ...)` (exec.py:424).

Same semantics, two names. Per `non_issues.md`, missing `is_` prefix on CLI options is fine -- but these are *internal API function parameters*, not CLI options, so the non-issue exemption does not apply. And even on the CLI side they likely surface as `--all`, so the bool name lives only in this layer.

Recommendation: Pick one and apply it. `is_all` matches the broader internal convention (`is_dry_run`, `is_start_desired`, `is_streaming`, etc.) in this codebase, so renaming `all_agents` -> `is_all` is the smaller change and locally consistent.

Decision: Accept


## 6. `reset_caches: bool` without `is_` prefix

Description: `list.py` carries a boolean `reset_caches` parameter through every fan-out helper (`list_agents` at :144, `_construct_and_discover_for_provider` at :245, `_construct_and_discover_all_providers` at :292, `_list_agents_batch` at :338, `_list_agents_streaming` at :396, `_construct_discover_and_emit_for_provider` at :434 -- six declarations). Adjacent files keep to `is_X` style for booleans (e.g. `is_streaming` right next to it on `list_agents`).

This is a lower-severity instance of #5 -- the inconsistency is consistent *within* list.py but inconsistent *with* the surrounding convention. Slightly subjective because `reset_caches` reads naturally as an imperative "do this thing"; `is_reset_caches` reads awkwardly. An acceptable alternative would be `is_cache_reset_requested` or simply moving it to a `cache_policy: CacheResetPolicy` enum if you wanted to spend bytes on it.

Recommendation: Either rename `reset_caches` -> `is_cache_reset` (or similar `is_X` form) across list.py, OR leave it alone and accept that imperative bools without `is_` are tolerated when they read more naturally. Pick one rule and document the choice in the style guide so future contributors don't have to re-litigate.

Decision: Accept (low priority)


## 7. (Borderline) Build-method naming in ErrorInfo hierarchy

Description: `list.py:ErrorInfo` and its subclasses use four different `build*` classmethod names:

- `ErrorInfo.build(exception)` -- the bare base case (list.py:53)
- `ProviderErrorInfo.build_for_provider(exception, provider_name)` (list.py:64)
- `HostErrorInfo.build_for_host(exception, host_id)` (list.py:79)
- `AgentErrorInfo.build_for_agent(exception, agent_id)` (list.py:94)

The `build_for_X` suffix duplicates the class name (`HostErrorInfo.build_for_host`). At call sites this is mildly noisy: `HostErrorInfo.build_for_host(e, host_id)` could be `HostErrorInfo.build(e, host_id)` and still be unambiguous. Mainly a polish point; not breakage.

Recommendation: Rename `build_for_provider` / `build_for_host` / `build_for_agent` to just `build` (overriding the base method's signature with the resource id). Reduces the redundancy and keeps the convention discoverable: every `ErrorInfo` subclass has a `.build(exception, resource_id)` classmethod.

Decision: Defer (low impact; nice-to-have after #2 lands)
