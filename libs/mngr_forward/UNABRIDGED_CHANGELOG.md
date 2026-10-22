# Unabridged Changelog - mngr_forward

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_forward/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-10-22

- `mngr forward` no longer rewrites its on-disk service map when an agent re-announces registrations it already knows. Before, an agent replaying a long services history caused one full rewrite of the cache file per line.

- Each agent's services stream now starts with `mngr event ... --latest-by services:service`, so a (re)spawned stream replays each service's latest registration instead of the agent's whole services history, which on one workspace was 13.9 million lines.

## 2026-10-21

The forward's own plain-text answers now declare `text/plain` instead of leaving the type for the browser to guess. This covers `Not authenticated`, `Backend not yet available`, the 502/504 timeouts, and the rest. The change only makes those answers say what they are. The broken workspace downloads that surfaced it (a refused download saved as `download.txt`) are fixed in default-workspace-template, whose pages no longer make the request the forward refused.

## 2026-10-20

Three comments in `mngr forward`'s CLI describe the process that relays its exit report and
stops it as a generic consumer rather than naming the higher-level app. The comment on the
TLS/HTTP-2 setup failure log no longer claims the consumer's listen wait would time out, which
stopped being true once that wait returned on the forward's exit. Comments only; no behaviour
changed.

`mngr forward` now logs why it is exiting: which shutdown signal it received (if any), or the class and raising frame of the exception escaping its serve loop or cleanup, together with the exit status it is about to return. Its SIGINT/SIGTERM handling is unchanged: a signal still triggers the same graceful shutdown.

`mngr forward` now exits with status 0 within a few seconds when stopped by SIGTERM or SIGINT. Previously its SIGHUP watcher thread waited for SIGHUP forever, so every graceful stop waited out the command's 10-second thread-exit timeout and then failed with `StrandTimedOutError` (exit status 1). The minds backend read that status as a crashed discovery/proxy subprocess.

When an agent host's sshd refuses this device's key, the SSH tunnel layer now logs a warning saying what was offered: the SSH user and port, the exception paramiko raised (a refusal, an authentication timeout, or, for a rejected RSA or ECDSA key file, the load error paramiko re-raises in its place), the SHA256 fingerprint and modification time of the private key (or that the host defers to the user's own SSH setup), and the auth types the server allows when it reports them. The key path itself is not logged. The exception is re-raised unchanged.

## 2026-10-19

`mngr forward` no longer keeps launching `mngr event` processes for agents whose host is stopped. Previously, each agent on a stopped host (for example a stopped Lima VM) got a fresh `mngr event ... --follow` process about once a minute, and every one exited at once with "the host is not online". With several stopped workspaces this caused regular CPU bursts. The agent's events stream is now held back while discovery reports its host as stopping, stopped, paused, crashed, failed or destroyed. It starts as soon as a discovery snapshot or host event reports the host in any other state (such as starting or running), with no leftover backoff delay. Discovery and the agent list are unchanged, and an unknown host state still starts the stream as before.

A dying events stream's respawn backoff no longer resets to its shortest interval by mistake. A stream now counts as healthy only once a discovery snapshot has seen it still running after a minute. Before, the gap between snapshots could be counted as the stream's lifetime.

## 2026-10-13

The group headers in `test_ratchets.py` change from the banner form `# --- Code safety ---` to `# ratchet_group("Code safety")`, a marker the ratchet sync script reads. The script now also keeps every test under the group its check has in `standard_ratchet_checks.py`, and the groups in that file's order. Here that moved the `Modal images` group up to its place after `Process management`. No ratchet, recorded count, or test name changes.

## 2026-10-11

Fixed: a workspace's chat transcripts (and every other server-sent event stream through the forward) silently stopped updating a few hours into a session, until the page was refreshed. hypercorn, which serves the forward's origin, closes a connection after 1000 requests by default. Over HTTP/2 (`--use-http2`) that close is a GOAWAY on the one multiplexed connection the browser holds per origin: the browser moves new requests to fresh connections (so sends and presence kept working), but keeps the drained connection and its open `EventSource` streams, and hypercorn drops every later write to those streams without an error. The proxy's stall guard never fired, the backend saw a healthy consumer, and the browser never reconnected. The chat origin, which posts presence for every open chat each minute, hit the cap first. The forward now runs hypercorn with no per-connection request limit.

## 2026-10-05

The SSH tunnel relay now multiplexes with `poll()` instead of `select()`. `select()` rejects any descriptor numbered 1024 or above, so a forward process holding that many descriptors (from a leak, or from many concurrent tunneled connections) failed every relay with `ValueError: filedescriptor out of range in select()`, which clients saw as a hung-up socket (Sentry MINDS-PRODUCTION-13). This supersedes PR #161. The relay tests, adapted from that PR, now drive a real in-process paramiko channel instead of socket-backed stubs.

The SIGHUP watcher and discovery-tail threads now start through `start_mngr_thread`, so they release their gevent Hub when they finish.

## 2026-09-30

Removed the 73 comment lines that had accumulated inside `libs/mngr_forward/imbue/mngr_forward/test_ratchets.py` explaining why individual ratchet counts had been raised.

A ratchet file records counts, not history. The reason a count moved now goes in the commit message and the changelog entry, the reason a particular code site is allowed goes in that module's docstring, and the set of exempt files goes in the `excluded_patterns=(...)` argument -- all places that stay correct as the code changes, which the comments did not.

## 2026-09-29

An app window no longer stays on "Loading workspace" after the forward's events stream for its workspace loses its reader. The `mngr event` child now exits when its reader dies (it used to keep running without it), so the forward's existing respawn replaces it and new app registrations arrive again.

The "Resolved no backend" warning now says how long the workspace's events stream has run and when it last printed a line, or that none is running. A newly started stream is logged at info.

`mngr forward`'s README and `--help` now call the desktop app Imbue Studio where they named the product.

## 2026-09-23

The plugin now emits a `system_interface_backend_answered` envelope on its forward stream when an agent's shell service answers a forwarded request with a 2xx (or accepts a websocket). It is the success-side counterpart of `system_interface_backend_failure`: an observation for a consumer's own health policy, reported only for the shell target and at most once per agent every five seconds, so an idle agent adds nothing and an agent with a page open adds one line every five seconds. minds uses it to clear a stale unresponsive verdict when its own probes cannot reach the shell but the renderer can.

An exception the plugin's serve loop is handed is now reported as a single log line naming the failure and the frame that raised it, with the context objects and full traceback following at debug. Previously the plugin deferred to asyncio's own handler, which wrote the headline, every context object and the traceback to the `asyncio` stdlib logger that nothing configures: the report went to stderr through logging's last-resort handler, so it never reached the plugin's structured log and cost dozens of stderr lines apiece. Repeats of one failure are also rationed to one report a minute, with the count of what was suppressed carried on the next one that is due -- a proxy pool timing out on a backlog after a network outage wrote 1640 unretrieved-task reports in ninety seconds, which was 94% of a log rotation for the application relaying that stderr. Benign TLS teardown noise is still dropped outright.

This branch carries the sharing identity cleanup merged from `mngr/sharing-cleanup`; that branch's own entry (`mngr-sharing-cleanup.md`) describes the changes in full.

`mngr forward` is now product-neutral about request identity: `--identity-file` and the `X-Imbue-Identity` header it rendered are gone, replaced by a generic `--request-headers-file <path>`. The file is a JSON object mapping agent ids (`agent-<hex>`) to `{header name: value}` objects, with the key `"*"` holding the headers for every agent not listed. On every proxied HTTP request and WebSocket handshake the proxy deletes any inbound header whose name appears anywhere in the file (the union over all entries) and then sets the agent's own entry, else the `"*"` entry, else nothing. Header names must be valid tokens and may not be request framing or hop-by-hop headers. The file is re-read whenever its mtime or size changes and a malformed file is logged once per change and treated as empty; without the flag nothing is stripped or stamped. What the headers mean is the host application's contract with its services (the minds desktop writes its identity header through this file).

Forwarded requests now carry one request-identity header, `X-Imbue-Identity` (a JSON object with an `owner` flag and, when known, the requester's account record), replacing `X-Share-Owner` / `X-Share-Email`. The owner's record is attached only for agents listed in the new `--identity-file` (a JSON map a host application maintains for the agents it has shared, re-read whenever it changes); every other request carries `{"owner":true}`. Any client-supplied copy of the header is dropped before it is set.

## 2026-09-21

Adopted the new repo-wide `raw ConcurrencyGroupExecutor construction` ratchet (`test_prevent_raw_concurrency_group_executor`). It flags production code that builds a `ConcurrencyGroupExecutor` directly instead of using `mngr_executor`, the wrapper that destroys each worker thread's gevent Hub. No production code change in this project.

## 2026-09-15

Tighten the behavior witnesses in `server_test.py` so each traced assertion verifies exactly the clause it is marked for. These are the accepted results of the first `mngr witness` run over the `libs/mngr_forward/behaviors` corpus, reviewed adversarially by the pipeline's review node; the coverage matrix is unchanged at 54 full, 16 partial, 0 none over 70 units.

- `authentication.bridge-destination`: the goto-bridge witness now asserts that the redemption happens on the agent's origin, not only that it lands on the agent path.

- `authentication.signed-out-home`: the signed-out home page test also witnesses the which-agents-exist half of `no-data-without-session`, with a partial note naming what it does not cover.

- `no-data-without-session` and `credential-not-forwarded`: two new witnesses cover the agent-data facet of a signed-out bare-origin request and the WebSocket handshake surface (the backend handshake carries no session cookie).

- `forwarding.service-origin`: the service-origin routing test exercises both example host forms.

- `spent-code-refused`: the refusal assertion is relaxed from `== 403` to `>= 400`, since the step names no exact status.

- Two gold-plated markers removed: `forwarding.never-serves-host-loopback` no longer claims the operator opt-in test, whose assertion did not witness the clause, and `forwarding.default-service-redirect` no longer claims the non-HTML test, whose only assertion said nothing about the redirect.

The behavior-mapper prompt variant for the `libs/mngr_forward` corpus moves from `tmr/behaviors_mapper.j2` (the `mngr tmr-behaviors` variant, left in place until TMR's behaviors recipe is retired) to `witness/mapper.j2`, where `mngr witness --root libs/mngr_forward/behaviors` discovers it positionally. The new variant extends the packaged `mapper.j2` of `imbue-mngr-witness` and fills only project facts: the `starlette.testclient.TestClient` idiom, the loopback rule for real network I/O, the `imbue/mngr_forward/witnesses/` placement of new witnesses, no mocks, and the note that these tests need no credentials or external services. The run-regime rules the old variant carried now live in the packaged contract.

The TMR-era variant `tmr/behaviors_mapper.j2` is removed now that `mngr tmr-behaviors` is retired, and every corpus README's incipit names the `behaviors` skill.

A test's description of the host app follows the desktop app's rename to Mind. No behavior change.

## 2026-09-09

Deflaked `test_subdomain_forward_emits_no_stall_envelope_when_the_backend_answers_in_time`, and dropped the `@pytest.mark.flaky` marker it had been carrying.

It used to race a real 50ms stall timer against the request in wall-clock time, so under CI scheduling jitter the timer could fire before the handler disarmed it and a spurious `STALLED` envelope failed the test. It now runs on an event loop whose clock only advances when the test says so: the request runs to completion (disarming the timer) while the clock is frozen, and only then is the clock moved past the stall deadline. The check is a deterministic fact about the disarm rather than a timing race, and it needs no retries.

No production behavior changed -- the handler always did cancel the timer, and the real stall window is 30s.

An SSH tunnel that a laptop sleep left half-open is now rebuilt before it is handed any traffic.

A sleep kills the TCP connection under a tunnel, but paramiko goes on reporting the transport active and the forward went on handing back the cached tunnel while its accept loop was running. The first requests after the wake each waited out the thirty-second channel-open bound, long enough for a consumer watching those failures to conclude the host was gone. Each SSH connection is now stamped with both clocks when it is built, and every tunnel remembers the connection it was built over; one that predates a suspension is retired along with its accept loop, and the next request builds a fresh one. One host serves a tunnel per agent and per service, and all of them are retired, not just the first one asked for after the wake.

## 2026-09-08

The README notes that the workspace's chat is a registered app at its own `chat` origin, like the terminal, since phase 10 of the workspace app model in default-workspace-template runs it as its own program; the forwarder itself is unchanged.

## 2026-09-01

Fixed the root cause of the flaky `test_tampered_session_cookie_rejected` (MIND-238), which failed about one run in sixteen.

The test walks every character of a valid session cookie, replaces it, and asserts the altered cookie is rejected. Its replacement rule (`"A"` -> `"B"`, else -> `"A"`) was not always a real tamper. itsdangerous renders its HMAC signature (and the timestamp) as base64url, and the final character of such a segment carries padding bits that decoding discards. A canonical cookie leaves those padding bits zero, so the last signature character is `"A"` about one time in sixteen -- and flipping `"A"` -> `"B"` there changes only a padding bit. The altered cookie decodes to the same bytes, so it is the same credential, and the proxy correctly accepted it, tripping the assertion.

The replacement now maps `"A"`-`"D"` (which share their high four bits) to `"Q"` and everything else to `"A"`, so every single-byte alteration changes the significant high bits at every position, including the last character of each segment. Brute force over 500 freshly minted cookies dropped from 32 wrongly-accepted alterations to 0.

Test-only: the proxy's cookie verification is unchanged, and the test's `witnesses` markers (`authentication.tampered-cookie-rejected`, `sessions-unforgeable`) still hold -- the fix strengthens what it witnesses rather than weakening it.

Marked `test_subdomain_forward_emits_no_stall_envelope_when_the_backend_answers_in_time` flaky: its 0.5s poll window racing the stall timer has lost on a contended CI sandbox and passed on retry.

## 2026-08-31

Added the initial `mngr forward` behavior corpus at `libs/mngr_forward/behaviors/` -- the proxy + auth core -- authored in the repo's behavior language and guarded by a live-corpus test (`test_behavior_corpus.py`).

The corpus speaks in host/agent terms: the proxy's addressable unit is the *agent*, served at its `agent-<hex>.localhost` origin, and a *host* is the substrate an agent runs on (the tunnel target, loopback, and the legacy `host-<hex>` coordinate that only ever redirects).

The corpus root carries the cross-cutting invariants: single-use codes, fetch-never-spends, no agent data or backend content without a session, unforgeable and bounded sessions, a single credential, the session cookie never reaching backend code, and no open redirects.

`authentication/` covers one-time-code sign-in, session lifetime and integrity, the two pre-authorized paths an embedding host uses (a preauth cookie and a browser-bridge token, both startup-fixed and reusable), the bare-origin home page (an index of discovered agents), and the goto bridge -- token audience, expiry, and forgery; one hop covering an agent's default service and every service origin through the domain-scoped cookie; coordinate canonicalization; and self-healing direct navigation.

`forwarding/` covers host-header routing across the `[<service>.]agent-<hex>.localhost` origins (plus the default-service redirect and the legacy host-coordinate redirect), HTTP byte-forwarding fidelity, WebSocket forwarding (relay, the post-accept close code, handshake refusal for anything unforwardable, and the no-client-headers invariant), backend error behavior (503 before headers, 502 after, 504 for a wedged or pool-exhausted proxy, and the self-healing loading page), and the host-loopback refusal invariant.

Also hardens startup: the proxy now loads its cookie-signing key eagerly, so a corrupt or unreadable key file fails the process at boot rather than erroring on the first request that needs it.

Deliberately out of scope here and tracked separately: the stdout envelope stream, discovery and backend resolution, and reverse tunnels.

Each folder's prose now lives in a `README.md` opening with the mandated incipit, replacing the `overview.md` files: `overview` is no longer a reserved basename, so such a file is read as a sidecar and rejected for having no matching `.feature`.

Added witnessing tests across `mngr_forward`, so the behavior corpus at `libs/mngr_forward/behaviors/` is now backed by tests that fail if the specified behavior breaks. Every unit was previously unwitnessed; the corpus itself is unchanged.

Sign-in and sessions: opening a fresh login URL signs the user in and lands on the home page, merely fetching that URL spends no code and leaves the user signed out, opening it while already signed in redirects home without spending the code, and a missing, unknown, or spent code is refused without establishing a session. A session survives a proxy restart from the same state directory, and a cookie is treated as signed out when signed under a different state directory, when any single byte is altered, or when older than the thirty-day window.

The goto bridge, driven end to end in process: the bare origin's `/goto/<coordinate>/` route mints a short-lived token and redirects to the agent, whose `/_subdomain_auth` endpoint verifies it and sets the domain-scoped session cookie. Covered are the signed-in roundtrip, one hop covering the default service and service origins, destination-path and service-origin preservation, direct-navigation self-heal, and the signed-out visitor reaching the sign-in prompt. The token's guarantees are pinned at the HTTP surface: refused with no cookie set when presented on another agent's origin, after its validity window, or when forged under a foreign key or altered in transit.

Preauth and the browser bridge: the startup-fixed preauth cookie is accepted on the bare and agent origins and a near-miss value is not, and the browser-bridge token is accepted, refused when forged, and refused when absent.

Forwarding: requests preserve method, path, query, body and the backend-naming Host header; buffered responses preserve status, headers including duplicates, and body; the frame-ancestors CSP is appended alongside the backend's own headers; backend redirects are forwarded rather than followed; event streams arrive incrementally; and a backend's own error status and body pass through unreinterpreted. Host-header routing is covered across the agent and service origins, the default-service-label redirect, and the legacy host-coordinate redirect.

Backend failures, as the client observes them: a backend that never delivers response headers yields 503 across all four rows of its outline, the 503 loading page carries the poll-and-reload script and its reload target answers once the backend is up, a stream whose backend is lost mid-flight simply ends rather than becoming an error status, and the mid-response-loss (502) and wedged-backend (504) cases are pinned.

WebSockets: an authenticated relay connects through at the same path and query, negotiates the offered subprotocol, and relays text and binary both ways; a non-agent host and a session-less connection are refused at the handshake, the latter before any backend contact; and a backend that drops closes the client with code 1011.

Corpus-wide invariants: one-time codes are single-use, no agent data or backend content is served without a session, sessions are unforgeable and bounded, one session reaches every agent without a second credential, the session cookie never reaches backend code, and a cross-origin destination is replaced with `/`.

Also lightened rot-prone documentation across the project, so module docstrings and comments no longer make claims that go stale as the code moves underneath them. Several docstrings pointed readers at an "acceptance test" for coverage they said was not in the file; no such test exists for this project, so those references sent readers looking for something that was never there. `resolver.py` documented a `resolve()` failure mode that does not exist (missing SSH info returns a target, not `None`) and omitted the discovery-membership state that gates every lookup; `server.py`'s route list said "only handles" while omitting `/_bridge`; `data_types.py` referred to "the three reasons below" where seven now follow; `stream_manager.py` and `embedding.py` cited a class and a documentation path that have since moved. Docstrings describing the live routing path in terms of the legacy `host-<hex>` coordinate now say `agent-<hex>`, matching the origin scheme the proxy actually serves.

Added `tmr/behaviors_mapper.j2`, the project's mapper prompt for `mngr tmr-behaviors`, which extends the packaged template with the placement, coverage and verdict guidance for this corpus.

Witnessing that event streams reach the client incrementally required driving the forwarding handler and its async send channel directly, because the claim is about arrival ordering and the synchronous test client only returns once a response is complete. The async/await ratchet records that increase with its rationale.

Tidied the resulting test module, which ten agents wrote in parallel without seeing each other's work. The recipe for building a test app was spelled out 51 times and is now stated once, so adding an argument to `create_forward_app` no longer means editing every site; the two app fixtures, previously identical but for a single keyword, are now one builder apart. The two bridge helpers, which served different subsystems under names a grep could not tell apart, are named for the bridges they build. The eleven tests that each re-derived the same well-formed agent coordinate now share one named constant that also records why the coordinate is deliberately unregistered.

The proxy's origin was two literals that had to agree by hand -- the port the app is told to listen on, and the port inside each test client's base URL -- and is now derived from one. Four families that wrote a single claim out as several near-identical functions (the unusable one-time-code refusals, the frame-ancestors policy, the client-gives-up pair, and the error-response envelopes) are now one parametrized test each, so the axis that varies is visible rather than inferred; none of them carried a behaviour back-link, so no coordinate lost its one-test-per-claim mapping. A helper that took a caller-allocated list to append backend requests to now returns it, since eleven of its thirteen callers passed a list they never read.

Every test client now runs the app lifespan. Three constructors previously handed back a client that had never entered it, so which helper a test happened to use silently decided whether the proxy's real HTTP client existed.

Registering the test agent is likewise stated once. Twenty-nine tests each repeated the same three lines -- mint the shared instance key, add it as a known agent, register a single backend -- varying only in the backend URL. The registrations that are irregular stay written out, because what makes them irregular is what their test is about: those carrying a service-label map, those registering several services, those registering none, and the one that registers two distinct agents.

## 2026-08-27

Dropped the per-agent `requests` event source from the forward stream defaults: nothing has written agent-side request events since latchkey 2.9.0's gateway extension took over the pending-permission queue, so the stream only ever carried silence. The `services` source is unchanged.

## 2026-08-26

Fixes the permanent "Loading workspace" wedge, and makes its recurrence visible in logs.

The service-map cache now persists origin labels alongside service URLs (versioned cache format, v2). App origins route by their unguessable `<name>-<rand>` label, so a fresh forward previously served the shell from the seed but left every app on the 503 loader until the slow per-agent event stream replayed -- forever, if that stream was wedged. Seeded runs now resolve app origins immediately. Legacy cache files still load (services only), and a v2 file read by an older forward degrades to an empty seed.

An unroutable origin now leaves a rate-limited warning in the log (per agent+label, HTTP and websocket paths). Previously the 503 loader was served silently, making a wedged label map invisible in every log bundle. The label in that key comes from the request's own hostname, so the rate limiter forgets keys whose interval has lapsed once it is tracking more than 512 of them, rather than accumulating one per origin ever asked for.

The per-agent `mngr event --follow` exit log now includes the child's last stderr lines (previously debug-only, so returncode=1 exits were undiagnosable from bundles). A child whose target does not exist now exits with a distinct code (`EXIT_CODE_TARGET_NOT_FOUND`, carried by the new `AgentIdNotFoundError` and `NoMatchingHostsError`) rather than the generic error code, so the forward can tell "retrying can never work" from a transient failure without matching on message text. After two such exits with no healthy run in between, it waits a flat 15-minute backoff instead of respawning at the 60s ladder cap forever. A single gone-target exit still takes the ordinary seconds-scale ladder, and a stream that ran long enough to count as healthy clears the count, so nothing that makes a live agent momentarily unfindable can strand it for 15 minutes.

## 2026-08-25

Origins are keyed by agent id: the origin family becomes `[<service>.]agent-<hex>.localhost` and the auth bridge `/goto/<agent-id>/`, so a workspace's URLs survive it moving to a different host. Legacy `host-<hex>` coordinates still parse -- HTML navigations 301 to the canonical agent origin, other requests 404 (a reload heals them), and `/goto/<host-id>/` canonicalizes through the resolver.

Merged the machine/workspace naming cleanup branch (`mngr/fix-machine-workspace-naming`, PR #436) into this branch and resolved the merge conflicts against main. The changes themselves are described in this project's `mngr-fix-machine-workspace-naming` changelog entry.

## 2026-08-24

`SSHTunnelManager` gained `remove_reverse_tunnel(ssh_info, local_port)`, the exact inverse of `setup_reverse_tunnel`: it tears down the single reverse tunnel identified by the same `(host:port, local_port)` key, leaving other tunnels -- including ones tagged with the same agent -- untouched.

This gives the latchkey discovery handler a way to clear a stale desktop-to-container tunnel without also dropping the same agent's desktop-to-VPS tunnel, which the agent-keyed `remove_reverse_tunnels_for_agent` (still used for stop/destroy teardown) was tearing down and re-dialing on every 30s discovery cycle.

## 2026-08-21

`CONNECT_ERROR` no longer conflates three unrelated things. A consumer driving a recovery UI off it could not tell "the workspace did not answer" from "this device could not build a tunnel to it" or "the plugin's own connection pool ran out", so it read all three as the workspace being sick. Three reasons are split out: `TUNNEL_SETUP_FAILED` (a failure against this device's own filesystem and socket table -- no known_hosts file to pin against, no private key to authenticate with, a local socket that could not be created or bound), `POOL_EXHAUSTED` (the pool ran out, so the backend was never dialed), and `BACKEND_NOT_LISTENING` (the SSH tunnel reached the host and the host refused the `direct-tcpip` channel to the inner port -- reachable machine, dead service). `CONNECT_ERROR` keeps everything that reached the network and failed there.

The classification comes from the tunnel layer rather than from the exception type, which cannot support it: paramiko raises the same `SSHException` for a host that vanished and for trust material this machine is missing, and a refused channel reaches the proxy as nothing but the tunnel socket closing. `SSHTunnelError` now carries the phase it was raised from, and the tunnel manager counts refused channel opens so a request can tell its own failure apart from a host it never reached.

A backend that reset the connection before answering was reported as `SSE_EOF` with a 502, the signal for a response that was dropped part-way through, rather than as an unreachable backend with a 503. Whether the reset arrives as `ReadError` or `RemoteProtocolError` is the platform's choice, not a fact about the backend -- a peer that closes a socket still holding the unread request is a clean EOF on macOS and an RST on Linux -- so on Linux this swallowed the whole `CONNECT_ERROR` split above, including `BACKEND_NOT_LISTENING`. The proxy now reads the response headers before the body, and classifies by which of the two failed: nothing delivered is a connect failure, while a body that stops after its headers -- dropped or merely stalled until the read budget ran out -- stays `SSE_EOF`.

Failure envelopes now carry a `detail` field describing the exception behind the observation, so a consumer can show a user what actually broke instead of a category name. Where httpx leaves an exception with no message -- `ReadError` and every `TimeoutException` do, which is exactly the reset and the stalled read above -- the class name is sent rather than an empty string, except for the pool timeout behind `POOL_EXHAUSTED`, which is spelled out because it is the one such detail a consumer shows to a person rather than writing to a log.

The `resolver_snapshot` envelope is removed. Its only consumer was the minds recovery-diagnostics probe, which is deleted in the same release; the resolver's own service-map cache is unaffected and still persists at every mutation.

Both pieces of trust material this device is supposed to hold -- the known_hosts file and the private key -- are now checked before connecting, which is what lets a missing one be reported as this device's fault. A host registered without a key file at all counts as missing one, rather than being sent to the host as an unusable path. paramiko would otherwise open the key only during authentication, after the host has answered, where a missing file is indistinguishable from the host rejecting the key we offered. A key that is present but no longer accepted still cannot be told from a rejection without asking the host, so it stays `CONNECT_ERROR`. The refusal names the paths it checked, and names them once when the explicitly-supplied path and the key's sibling are the same file, which for some providers they are.

The paramiko reverse-tunnel client now supports credential-deferring SSH hosts (an outer host with no mngr-owned key, e.g. `DOCKER_HOST=ssh://...`): it verifies the host key against the user's own `~/.ssh/known_hosts` and lets paramiko run its default key lookup chain, matching the documented convention the pyinfra-based consumers of the same hosts already follow. Previously such hosts were always refused before connecting -- as missing key material ("No SSH key file at .": the pre-connect trust-material check treated the empty key path as a key this device does not hold), and before that check existed, with "No known_hosts file at known_hosts" (a cwd-relative sibling of the empty key path) -- which broke the latchkey VPS gateway exposure in the latchkey e2e release test. The pre-connect missing-key refusal still applies to every host with a producer-owned key path; only the no-key-at-all sentinel is exempt, since its key material is the user's own. Host-key checking stays strict: no trust-on-first-use is introduced.

## 2026-08-20

Comment-only: the stream manager's bare-name fallback for `services` events that omit the origin `label` (rows written before minds-v0.3.12 minted `<name>-<rand>` labels) is now marked with a `CLEANUP:` comment stating when the fallback can be removed.

## 2026-08-18

Fixed a proxy wedge where SSE streams whose client leg went quiet without disconnecting (e.g. a half-open HTTP/2 connection left behind by a laptop sleep) permanently pinned pooled backend connections. The ASGI server has no write timeout for an active stream, so each such stream blocked forever mid-send, holding its httpx pool slot; once 100 accumulated (httpx's default pool ceiling), every proxied request timed out at 30s without ever dialing the backend, and only restarting `mngr forward` recovered. Streamed responses now bound every client write with a 30-second budget of its own and deterministically close the backend stream on every exit path (stall, error, or disconnect) instead of relying on garbage collection -- including a stall on the response headers, which is the first write of all and so happens before there is any stream body to unwind.

The proxying httpx clients now have explicit connection-pool ceilings above the HTTP/2 concurrent-stream limit they used to coincide with, so any future connection leak degrades gradually instead of dead-ending all traffic. The two paths get different ceilings, because a pooled slot costs very different amounts on each: 1000 for the direct loopback client, where a slot is one socket, and 200 for a remote agent's SSH tunnel, where every slot is also a relay thread and an SSH channel, and where the ceiling applies per agent tunnel rather than once for the whole process. Both sit far above any healthy level of concurrency rather than at a calculated breaking point.

Pool exhaustion (`httpx.PoolTimeout`) is now distinguishable from a wedged backend: it logs a distinct warning and returns 504 "Proxy connection pool exhausted" instead of the identical "Backend timed out", while the failure envelope reason stays `CONNECT_ERROR` for compatibility with consumers. `CONNECT_ERROR`'s documented meaning now says so, and warns that under pool exhaustion the backend is never dialed at all -- so a consumer must not read the reason as proof that the agent's own backend is at fault.

An SSE client that disconnects while the backend stream is still being opened now ends the request with a 499 and releases its pooled connection immediately, instead of holding it for the full SSE read budget. This extends the existing client-disconnect race from the buffered path to the streaming path's handoff window; once the stream is open, starlette's own disconnect watcher already covered the response body. A disconnect that arrives at the same moment the stream opens ends the request the same way, rather than starting a stream nothing is left to end.

## 2026-08-16

The forward resolver, stream manager, service-map cache, and `resolver_snapshot` envelope are now keyed by the agent instance (`<agent_id>@<host_id>`) instead of the bare agent id: agent ids are unique per host, not globally, so the same id on two hosts (e.g. mid-migration) keeps independent services, SSH info, and routing.

Per-agent `mngr event` subprocesses address `ID@HOST`, so their streams stay unambiguous when an id exists on multiple hosts.

Old service-map cache entries keyed by bare agent ids are dropped at seed time (a benign one-time startup slowdown that self-corrects once discovery and the event streams repopulate the map).

The forward plugin's discovery stream consumer now uses mngr's deduplicating schema-mismatch warner: discovery lines written by a different mngr version are skipped with one warning per distinct failure (instead of one warning per line), and additive fields from newer versions parse cleanly. Part of the forward-compatible event parsing work (mngr-internal#422).

The forward SSH tunnel now prefers the explicitly-supplied `known_hosts_path` from host SSH info when verifying host keys, falling back to the file next to the SSH key for producers that predate the field. It still refuses to connect when no pin file can be found anywhere (never trust-on-first-use), and the refusal error now names every candidate path it checked.

The repeated per-agent "SSH tunnel setup failed" warning is now rate-limited to one line per agent per minute, reporting the count of suppressed earlier failures, instead of logging once per retry (about once a second while a workspace view is open).

## 2026-08-15

The SSH tunnel manager no longer falls back to trust-on-first-use (paramiko `AutoAddPolicy`) when a host's known_hosts file is missing: connecting without a pinned host key is now a hard `SSHTunnelError`, matching the strict host-key checking used everywhere else.

## 2026-08-13

A slow backend response no longer kills the request. Previously the forward proxy used a flat 30-second `httpx` timeout, so any request that went 30 seconds without a byte arriving was cancelled and turned into a 504. Because a buffered handler sends nothing until it returns, that capped every proxied request -- including a user app's own long-running endpoint on its service origin (`<name>.host-<hex>.localhost`) -- at 30 seconds.

The 30-second mark now emits an advisory `STALLED` backend-failure envelope *without* touching the request, which is all a consumer needed it for: the envelope only enrolls the agent for active probing, and the probe (not this timer) decides whether the workspace is actually wedged. Detection latency for a genuinely wedged backend is unchanged; a merely-slow backend now completes normally.

What ends a proxied request now is its client hanging up, not a clock: the forward races the backend against the client's disconnect and abandons the request as soon as nobody is waiting for it, releasing the pooled connection and -- for a remote agent -- the SSH channel and relay thread behind it. This matters against a wedged backend, where a consumer's own short-timeout health probe would otherwise leave one abandoned request pinned per probe. Behind that, a backend may now stay silent for 10 minutes rather than 30 seconds before the request is cut off; like the old timeout this is a per-read/write budget, so it bounds silence rather than total duration, and nothing caps how long a request whose backend keeps answering may run. The same 10 minutes now applies to pushing a request body into a backend that stopped reading it -- the mirror case, and the one where a large upload relayed over an SSH channel can legitimately be slow. Connect and pool budgets are unchanged. Server-sent-event streams keep the original 30-second read budget, since a stream is expected to heartbeat and a silent one really is evidence of a dead backend.

`SystemInterfaceBackendFailureReason` gains `STALLED`. Unlike every other reason it does not mean the request failed -- it may still succeed -- so a consumer must not treat it as an error. An older consumer that does not know the value logs a warning and drops the envelope rather than failing.

Forwarded requests now carry a request-identity header. Every HTTP and WebSocket request `mngr forward` proxies to a workspace gets `X-Share-Owner: true` (the single authenticated local user is always the workspace owner), and any client-supplied `X-Share-Owner` / `X-Share-Email` is stripped first so an agent-controlled backend page cannot forge it. This matches the header contract a shared workspace gets from its in-container share-gateway, so in-workspace services read request identity the same way locally and over the relay.

Fixed: terminals (and other service panels) in workspaces created before the
default-workspace-template dropped its `/service/` path-prefix proxy no longer
reload forever under the new desktop client. Those workspaces' system_interface
serves service panels at `/service/<name>/...` on its own origin behind a
service-worker bootstrap whose `document.cookie` write the desktop's
partitioned content embedding rejects, so the bootstrap page looped
indefinitely. The forward proxy now intercepts such navigations when they would
route to the shell and 307-redirects them to the service's own origin
(`<name>.host-<hex>.localhost`), where the service is proxied directly with no
bootstrap involved. Label-less legacy registrations resolve via the existing
label-as-name fallback. Scoped to navigations naming a service that actually
resolves; everything else passes through unchanged. CLEANUP-marked for removal
once every pre-update workspace has run `update-self`.

## 2026-08-11

Fix a forward (direct-tcpip) tunnel wedging permanently once its SSH connection silently dies -- usually a laptop resumed from sleep, but any half-open TCP does it. paramiko keeps reporting such a transport `is_active()`, so nothing noticed: `open_channel` blocked on paramiko's 3600s default, and because it ran on the tunnel's single accept loop, every later connection queued behind the stuck one. The tunnel stayed dead for the rest of the session while the host itself remained reachable.

A channel open now waits at most 30s -- one open is a single round trip to sshd, so that is already far beyond what a healthy link needs -- and runs per connection, so one stuck open can neither block the tunnel nor outlive its usefulness. An open that fails because the peer stopped answering drops the cached SSH connection and retires its tunnel, so the next request establishes both fresh; an open that sshd answered and refused -- what a workspace whose service has not finished starting returns -- is left alone.

Each tunnel's Unix socket is now bound and listening before its path is handed to the caller. Previously the caller polled for the socket *file*, which appears at `bind()`, so a request could arrive before the `listen()` and be refused -- surfacing as a spurious unreachable backend.

A websocket whose backend dies during the opening handshake now emits the same `CONNECT_ERROR` envelope as every other unreachable-backend path, and closes the client socket with 1011. Previously it escaped as an unhandled ASGI error, so the consumer never learned the backend had died and the client, given no close frame, waited out its own reconnect timer.

Also adds diagnostic logging when the proxy cannot reach a backend, times out talking to one, or loses the connection mid-response, on both the plain and SSE request paths -- previously these left no plugin-side trace at all. Unreachable-backend logging is at debug and the other two at warning, since a backend that is not listening yet is the expected shape of a workspace still starting up.

## 2026-08-03

Embedding substrate for the minds iframe migration: workspace origins can now be embedded cross-site in an iframe by a trusted host application.

- On the TLS path (`--use-http2`) session cookies become `SameSite=None; Secure; Partitioned` so workspace requests authenticate from inside a cross-site iframe. The plain-HTTP path keeps `Lax` (embedding unsupported there).

- New `/_bridge` route + `--browser-bridge-token <secret>`: a host application 302s an already-authenticated browser to `/_bridge?token=...&next=...` to obtain the bare-origin session cookie without consuming an OTP -- the browser twin of the Electron preauth cookie injection.

- The proxy now owns embedding policy: a `Content-Security-Policy: frame-ancestors` header is APPENDED to every proxied workspace response (never touching bodies or existing headers -- multiple CSP headers compose by intersection). The default denies external embedding (`'self'` + the workspace's own origin family); pass `--embedder-origin <scheme://host[:port]>` (repeatable) to allow specific embedders. **Breaking**: previously no header was sent, so any page could iframe a workspace origin.

- TLS leaves are now minted per startup from a persistent local CA under `$MNGR_HOST_DIR/plugin/forward/ca/` (mkcert-style) instead of a regenerated self-signed cert. `mngr forward --trust-ca` installs the CA into the platform trust stores (macOS login keychain / Linux per-user NSS) so plain browsers get no certificate interstitials; the minds Electron app keeps trusting programmatically with no install.

Service-per-origin local forwarding with host-id workspace coordinates.

Hostnames are now `[<service>.]host-<hex>.localhost:<port>`: the bare origin serves the configured shell service (or the fixed port in `--forward-port` mode), `<service>.` origins reach any agent-registered service directly, and deeper labels route to the same service (its own sub-origin space for multi-origin apps). Replaces the previous `agent-<hex>.localhost` single-origin scheme.

The `/goto/` bridge is keyed by host id, carries an optional `service` label chain so deep links land on the exact origin requested, and sets the session cookie with `Domain=host-<hex>.localhost` so one bridge hop authenticates the shell and every service origin.

Unregistered-but-plausible service origins serve the auto-retrying loading page instead of an error.

TLS/HTTP-2 mode mints per-SNI certificates on first handshake for nested `.localhost` names (a static SAN list cannot cover runtime-discovered host ids or service labels); certs for names longer than the 64-char CN limit are SAN-only.

Follow-up fixes on the self-hosted sharing branch: no user-visible `mngr_forward` changes beyond the sibling `mngr-finish-forwarding` entry (only a stale comment in the goto-redirect test was corrected).

Merge main into the self-hosted sharing branch (no further `mngr_forward` changes beyond the sibling entries in this PR).

Review fixes on top: the debug index no longer renders broken `/goto//` links for agents with no known host, and a discovery-invariant violation during agent setup now logs a warning instead of failing silently.

Second review pass: `/goto/` host ids are validated against the strict `host-<32hex>` shape and lowercased (malformed ids could inject bytes into the redirect and uppercase ids dead-ended in a 403); the per-SNI TLS cert cache evicts its oldest entry at the cap instead of permanently serving the static cert to new origins; and the temp-PEM helper no longer leaks a file descriptor when the write fails.

Third review pass: the /goto/ bridge routes deeper sub-origin labels with the hostname-label charset (only the last label is a service name), so a deep sub-origin like `a--b.svc` completes login instead of 404ing.

Merge main into the mithril-refactor stack (relay-based sharing, iframe desktop shell, Mithril SPA). The changes to this project arrive verbatim from main and are described by their original PRs' changelog entries; no project-specific conflict resolution was needed here.

Route local service origins by an unguessable per-service label. The `services` event now carries a `label` (`<name>-<rand>`); the resolver maps an incoming `<label>.host-<hex>.localhost` origin back to its service (falling back to the name for a label-less/legacy service), and the bare `host-<hex>.localhost` origin now 302-redirects HTML navigations to the shell service's own label origin -- keeping the local grammar identical to a share, where the bare domain cannot be served. Non-HTML requests to the bare origin (e.g. the workspace readiness probe) are still served directly, so probes are unaffected.

Rewrite forwarding around per-service browser origins on host-id workspace coordinates: every registered service gets its own origin (`<label>.host-<hex>.localhost`), labels are minted per service, and the old path-prefix proxying is gone. Adds the `/_bridge` browser entry point (spawn-time secret, the browser twin of Electron's programmatic cookie injection), an `--embedder-origin` flag that appends a `frame-ancestors` CSP so only the desktop shell may iframe workspace content, a local trust helper for the proxy's TLS CA, and HTTP/2 TLS serving with partitioned cookies.

## 2026-08-02

Added the repo-wide `test_prevent_unpinned_modal_pip_install` ratchet (synced into every project's `test_ratchets.py`): Modal images must install from a committed hash-locked requirements export instead of passing bare package names to `pip_install`/`uv_pip_install`. No violations in this project (count 0).

## 2026-07-31

Fix the workspace WebSocket forwarder leaving a client socket half-open forever when the backend leg of a forwarded connection dies (system-interface restart, SSH tunnel blip, sleep/wake). The forwarder now races its two relay tasks instead of awaiting both, and explicitly closes the client leg (code 1011) when the backend side ends -- previously a send-quiet client like the system interface's /api/ws never observed the close, so the browser silently stopped receiving all real-time updates (frozen activity indicators, stale lifecycle states) while agents' layout operations failed with "no client connected" 412s until the page was manually reloaded. Reproduced live against an imbue cloud workspace before the fix.

Also adds diagnostic logging around WS forwarding: an info line when a forward is established and when it ends (agent id + path + which leg ended first).

Per-agent event stream respawns are now paced by an exponential backoff (2s doubling to a 60s cap, reset once a stream survives 60s) instead of retrying at the discovery snapshot cadence. A stream dying instantly against an unreachable host used to respawn every snapshot -- a reconnect storm that, against an sshd with per-source penalties (OpenSSH >= 9.8 defaults), sustained the penalty window forever and locked the machine out of an otherwise healthy host.

## 2026-07-29

Fixed unbounded memory growth in `mngr forward`. It supervises a `mngr observe --discovery-only` child plus one `mngr event --follow` child per agent, all of which stream for as long as the forward runs, and it retained every line they ever emitted.

Those children are now started with `is_output_accumulated=False`, so lines are consumed as they arrive and then dropped. Their stderr continues to be logged.

## 2026-07-23

The shared "Loading workspace" page's building blocks are now exposed for embedding: the card markup (render_loading_card) and its card-level CSS (LOADING_CARD_CSS) are split out of the standalone page so a consumer can host the exact same card inside its own layout (the minds desktop client's workspace-recovery page embeds it under the app titlebar). The standalone proxy loader page is unchanged.

## 2026-07-14

`mngr forward --use-http2`: abandoned TLS connections no longer dump an "Unhandled exception in client_connected_cb / TimeoutError: SSL shutdown timed out" traceback to stderr, and are now force-closed after a bounded few-second wait instead of holding a per-connection task open for asyncio's full 30-second SSL shutdown timeout. The serve loop bounds the wait for the peer's close_notify reply (hypercorn does not expose asyncio's ssl_shutdown_timeout) and drops the benign teardown errors of already-dead connections (the SSL-shutdown TimeoutError and TLS handshake failures) instead of logging them as unhandled exceptions.

## 2026-07-13

Add a `--use-http2` flag to `mngr forward` that terminates TLS and negotiates HTTP/2 (via ALPN) for the workspace origin instead of serving plain HTTP/1.1.

HTTP/2 multiplexes many streams over one connection, so the workspace UI is no longer capped by Chromium's ~6-connection-per-origin HTTP/1.1 limit (which hangs the UI once enough chat/app streams are open). When the flag is set, the proxy generates a fresh self-signed cert at startup (SANs `localhost`, `*.localhost`, `127.0.0.1`; loaded via a transient 0600 temp file, never persisted), serves TLS + h2 via hypercorn (replacing uvicorn), and its client-facing URLs become `https`/`wss` with the `mngr_forward_session` cookie marked `Secure`. WebSockets (terminal, log stream, `/api/ws` state socket) keep working over the TLS connection. hypercorn is the ASGI server whether or not a WebSocket rides the h2 connection (it advertises RFC 8441 Extended CONNECT), and its `websocket.receive` events always include both `text` and `bytes` keys with the unused one `None` (uvicorn omitted it), so the client->backend forwarder now selects each frame's payload by value rather than key presence.

The flag is off by default, so a human running `mngr forward` still gets plain HTTP/1.1 with no cert friction; only clients that can trust the self-signed cert (the minds desktop app) should enable it. There is no runtime HTTP fallback -- if TLS setup fails the proxy exits during startup with a log line naming TLS/HTTP-2 as the cause.

`mngr forward` now persists its per-agent service map to a small on-disk cache (`$MNGR_HOST_DIR/plugin/forward/service_map.json`) while running, and seeds the resolver from it at startup. A restored window onto a remote mind whose container is up becomes routable as soon as discovery supplies membership + SSH info, instead of waiting on the slow per-agent `mngr event ... services` stream (measured ~10s cold, ~50s under spawn contention). The live stream still runs and overwrites the seed as soon as it delivers, so a stale seed self-corrects within one stream connect. An empty, missing, or corrupt cache is a no-op: startup behaves exactly as before.

## 2026-07-10

The forward stream manager no longer logs a "Discovery error from ..." warning on every poll cycle for a provider stuck on the same failure (e.g. missing credentials): provider-level discovery errors are now logged once per process via the shared `DiscoveryErrorLogSuppressor`, with an info-level recovery line (and re-armed suppression) when the provider's discovery next succeeds. Host- and agent-attributed discovery errors keep logging on every occurrence.

## 2026-07-06

The "Loading workspace" proxy loader now re-attempts the workspace by polling in the background and reloading once it answers, instead of full-reloading itself every second via a `<meta http-equiv="refresh">`. The old full reload stole OS focus from any other view layered over the loader (in the minds app, the bug-report modal), which made the modal's text field impossible to type into -- the focus was cleared every second. Polling leaves the loader (and any overlay focused above it) untouched while waiting, and also keeps the spinner from visibly jumping on each tick.

Changed: `mngr forward` now consumes the new per-provider discovery model. The plugin's stream manager feeds every parsed discovery event into the shared, span-aware `DiscoveryStateAggregator` and reacts to the returned membership delta (newly present agents get a tunnel + `mngr event` stream, newly absent agents are torn down). Handling of the legacy global `FullDiscoverySnapshotEvent` (`DISCOVERY_FULL`) has been removed; the plugin now routes `ProviderDiscoverySnapshotEvent` (`DISCOVERY_PROVIDER`) plus the incremental agent/host events. Retain-on-provider-error semantics are unchanged, now provided by the aggregator's per-provider scoping.

Fixed a span-awareness gap: the periodic snapshot's per-agent events-stream retry no longer respawns a stream for an agent destroyed during that snapshot's discovery span (the retry is now restricted to agents the aggregator still tracks).

Updated the `mngr forward` help example for filtering by label to use `has(agent.labels.is_primary)` instead of the removed `workspace` label, matching the new workspace-discovery filter.

Integrates the "simple names" work: the `mngr forward` label-filter help example now uses `has(agent.labels.is_primary)` instead of the removed `workspace` label, matching the new workspace-discovery filter.

## 2026-07-01

Added a new async/await ratchet (`test_prevent_async_await`) that freezes the current amount of `async def` / `await` usage in this project and fails if new async code is added. We strongly prefer synchronous code: it is far easier to debug, and our software is intentionally low-scale, so async provides no benefit. Existing usage is grandfathered in at its current count; the count can only decrease.

## 2026-06-28

Respawns a dead per-agent events stream instead of skipping it forever, fixing a case where the forward got permanently stuck serving the "Loading workspace" / 503 page after an agent's host restarted.

Each tracked agent has a long-lived `mngr event <id> services requests --follow` child whose output is the only thing that populates the resolver's per-agent service map; with the service-forwarding strategy, `resolve` returns None (a 503) until that map contains the requested service URL. When the agent's host restarts (e.g. after a reboot), the `--follow` connection breaks and the child exits non-zero, but the old `_start_events_stream` guard skipped any agent already present in `_events_processes` -- including the now-dead entry -- so the stream was never respawned, the service map stayed empty, and the proxy returned 503 indefinitely even though discovery (including fresh SSH info) had fully recovered. `_start_events_stream` now treats a dead entry as absent: it drops the exited child, logs the respawn, and starts a fresh stream. Respawns ride the periodic discovery snapshot, which rate-limits them to the snapshot cadence, and already-known services are preserved across the respawn.

## 2026-06-23

The websocket forward path now emits a `system_interface_backend_failure` envelope when the backend connection fails (unresolved target, SSH-tunnel setup failure, refused host-loopback dial, or a connect-time backend-websocket failure), matching what the HTTP/SSE paths already did. Previously only HTTP failures emitted this envelope, so a consumer like minds could go blind to a dead system interface whose only live channel was a websocket: an already-loaded SPA whose backend died would silently retry its websocket forever, never enrolling the agent as a recovery probe suspect, so the recovery redirect never fired and the user was stranded on a frozen workspace. The websocket path now feeds the same recovery signal as HTTP.

## 2026-06-22

Added a `mngr forward --on-error {abort,continue}` flag (default `abort`). Under
`continue`, the `--no-observe` startup snapshot tolerates an
unauthenticated/unreachable provider: it runs `mngr list --on-error continue` and
forwards the agents the healthy providers reported instead of failing to start.
The flag affects only `--no-observe`; the observe and `--observe-via-file` modes
already tolerate provider errors and are unchanged.

## 2026-06-19

Removed the now-vestigial `ForwardPluginConfig.merge_with` override; the config merge is routed through the overlay pipeline, which reproduces the same assign-by-default semantics. No user-visible behavior change.

Trimmed the README to user-relevant content and tightened it for concision.

## 2026-06-11

Hardened reverse SSH tunnel teardown so a half-dead connection no longer orphans the forwarded port on the remote sshd (which made the next run's port forward request get denied):

- `SSHTunnelManager.cleanup()` now attempts to cancel each reverse port forward unconditionally, instead of skipping the cancel when the connection looks inactive.

- Every tunnel SSH connection now sends periodic keepalives, so an idle reverse tunnel does not silently die unnoticed and the health check can repair a dead connection promptly.

## 2026-06-10

Raised the stale coverage floor from 50% to 70% to match the coverage CI already measures (~75%).

## 2026-06-08

Tests now isolate $HOME the same way as every other mngr plugin: the project
conftest calls `register_plugin_test_fixtures(globals())`, which brings in the
autouse `setup_test_mngr_env` fixture. Previously this plugin's tests did not
redirect $HOME, so running them on their own could read or write the real
`~/.mngr` / `~/.claude.json`. Internal test-infrastructure change only; no
user-facing behavior change.

- Now auto-discovered as a publishable package by the release tooling (it is a standalone `mngr forward` plugin, usable outside the minds bundle). It will be offered for first publication to PyPI on the next release. Its previously-unpinned internal deps (`imbue-mngr`, `imbue-common`, `concurrency-group`) are now pinned with `==` to their current workspace versions, as a published wheel requires. No runtime change.

## 2026-06-05

- Added to the release tooling's publish graph (`scripts/utils.py`). It will be offered for first publication to PyPI on the next release. Its previously-unpinned internal deps (`imbue-mngr`, `imbue-common`, `concurrency-group`) are now pinned with `==` to their current workspace versions, as a published wheel requires. No runtime change.

## 2026-06-04

`mngr forward --observe-via-file` makes the forward server consume discovery by tailing the shared discovery events file in-process rather than spawning its own `mngr observe --discovery-only` subprocess. Per-agent `mngr event` streams are still spawned for discovered agents. The flag is mutually exclusive with `--no-observe` and works with either `--service` or `--forward-port`. In this mode SIGHUP is a no-op (there is no observe child to bounce; the file's own writer re-emits snapshots that the tailer picks up automatically).

Adopted the new repo-wide `per-file host uploads inside loops` ratchet check (flags write_file/write_text_file/put_file calls inside loops, which should use a single rsync via host.copy_directory instead). No production code change in this project.

## 2026-06-04

`mngr forward` no longer drops a live agent when its provider's discovery merely errored on a poll. The stream manager now retains agents whose provider is in the snapshot's `error_by_provider_name`, keeping their service mapping and per-agent events stream alive (logged at debug). A retained agent is only torn down on an explicit destroy or a later successful snapshot that omits it. A provider that succeeds and simply returns fewer agents still drops the missing ones as before.

## 2026-06-03

Stop following the per-agent `refresh` event source in `ForwardStreamManager`: the default event sources are now just `services` and `requests`.

This is part of tearing out the unused refresh-event plumbing across `mngr_forward` and `minds.desktop_client`. The refresh-via-desktop-client mechanism has been superseded by an `open_tab` WebSocket broadcast from the workspace server.

## 2026-06-02

The forwarding plugin now reports an unreachable backend as a backend
failure instead of flashing a raw error, and reports HTTP error responses
with a single generic reason that consumers interpret themselves.

- An SSH-tunnel setup failure and a refused host-loopback dial (no SSH
  tunnel available) are both treated as backend failures: the plugin
  emits a `CONNECT_ERROR` `system_interface_backend_failure` envelope and
  serves the styled "Loading workspace" loader to HTML callers, the same
  as other unreachable-backend cases. A consumer of the envelope stream
  can use this to drive its own recovery UI.
- The "Loading workspace" loader no longer shows the explanatory "This
  page will reload automatically..." line -- it just shows the heading,
  vertically centered against the spinner.
- HTTP error handling is simplified. The plugin no longer special-cases
  which status codes matter (it previously tagged only 502/503/504 as
  `FIVEXX_RESPONSE` and 404-on-`GET` as `NOT_FOUND_RESPONSE`). It now
  forwards every response unchanged and emits a single `ERROR_RESPONSE`
  reason -- carrying the `status_code` -- for any non-2xx response,
  leaving the policy decision (which statuses warrant action, and what
  action) entirely to the consumer.
- New `resolver_snapshot` envelope: the plugin emits the full per-agent
  service map on every mutation of that map -- both `update_services`
  (set/replace for one agent) and the destruction paths
  (`remove_known_agent` and `update_known_agents` when they drop an agent
  that had services) -- so a consumer's mirror does not retain stale
  entries for destroyed agents. The full map is sent on every change (no
  per-agent diff) so a late-attaching consumer only needs the most recent
  envelope to be in sync. No periodic flushes, no debouncing, no initial
  empty emission; the first envelope is sent on the first real services
  event. A consumer older than this change transparently drops the new
  payload; a consumer running against an older plugin simply sees no
  `resolver_snapshot` -- the same transient as a fresh plugin startup.

## 2026-05-28

# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

No user-facing behavior change.

## 2026-05-27

# ty 0.0.39 / paramiko 4.0 type fixes

- Converted bracketed `# type: ignore[...]` suppressions to `# ty: ignore[...]` (test files), since `ty` 0.0.39 no longer honors the mypy-style bracketed form.
- The new `types-paramiko` stubs (pulled in by the paramiko 4.0 bump) surfaced an intentional Liskov-violating `get_transport` override in the SSH-tunnel test fake (`FakeSSHClient`); this is now annotated with `# ty: ignore[invalid-method-override]`.

- Tightened this project's `test_ratchets.py` violation counts to their exact current values (`--inline-snapshot=trim`).

Test-only changes; no user-facing behavior change.

## 2026-05-26

# `SSHTunnelManager` is now the single SSH tunneling implementation

`mngr_forward/ssh_tunnel.py`'s `SSHTunnelManager` (and `RemoteSSHInfo`,
`ReverseTunnelInfo`, `SSHTunnelError`) absorbed the latchkey package's
parallel copy and is now the only SSH tunneling implementation in the
monorepo. Both the plugin's own forward (direct-tcpip) and reverse
(`--reverse REMOTE:LOCAL`) paths as well as the `mngr latchkey forward`
supervisor use it.

Behavior changes the existing forward-plugin callers will see:

- `ReverseTunnelInfo` gained an optional `agent_id: str | None = None`
  field; `setup_reverse_tunnel` gained an optional `agent_id`
  parameter. Existing callers can ignore both -- the default `None`
  matches the pre-change behavior.
- The reverse-tunnel repair loop now uses per-tunnel exponential
  backoff (1s, 2s, 4s, ..., capped at 5min) instead of the previous
  flat 30s cadence. A healthy target sees the same recovery latency;
  a permanently-gone target costs one paramiko handshake every five
  minutes instead of every 30s. Failures clear on a successful repair
  (or when a sibling tunnel on the same SSH host gets repaired and
  the connection comes back).
- New `remove_reverse_tunnels_for_agent(agent_id)` method tears down
  every reverse tunnel tagged with a given `agent_id`. It is careful
  not to close an SSH client out from under any live *forward* tunnel
  that shares the same host -- the two flavors of tunnel can coexist
  on one connection.

Public API additions are backward-compatible. The deleted
`mngr_latchkey/ssh_tunnel.py` is now re-exported transparently from
this module's existing public surface.

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

Adopted the `PREVENT_BARE_TMUX_TARGETS` ratchet rule (added in `imbue_common`) via
`rc.check_bare_tmux_targets(_DIR, snapshot(0))` in this project's `test_ratchets.py`.
This ratchet prevents new occurrences of `tmux <subcmd> -t '<bare-name>'` -- targets
without a leading `=` exact-match prefix, which can silently route commands to a
sibling session whose name shares a prefix with the intended one. No production code
changes in this project; the adopting test starts at a baseline of zero violations.

## 2026-05-22

`mngr forward` no longer crashes when its bind port is already in use. `--port` is now optional: when omitted, the server tries its default (8421) and falls back to an OS-assigned port if that is taken; when supplied explicitly, it still binds exactly that port and fails fast (with a clean error) if it is unavailable. The server binds its listen socket up front and hands it to uvicorn, so the `listening` envelope always reports the port actually bound.

## Discovery schema bump

- `mngr_forward` parses `FullDiscoverySnapshotEvent` lines from its inner `mngr observe --discovery-only` subprocess. The event grew two additional fields (`providers` and `error_by_provider_name`) in `libs/mngr`. This build picks them up transparently -- older `mngr_forward` builds running against new snapshots will raise `DiscoverySchemaChangedError` and must be rebuilt.

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

## 2026-05-20

Renamed the workspace-server envelope contract to system-interface in lockstep with the mngr-side rename: `WorkspaceBackendFailure*` → `SystemInterfaceBackendFailure*`, the envelope type literal `workspace_backend_failure` → `system_interface_backend_failure`, and the plugin's 503 loader page now reads "System interface starting".

Workspace-server restart and health-recovery support on the `mngr_forward` plugin (consumed by minds).

- The plugin emits `workspace_backend_failure` envelopes when it sees connection errors, mid-SSE EOF, or 5xx responses from the workspace backend. Consumers (minds) can track these as a per-agent health state machine to trigger a recovery UI.
- The plugin's 503 fallback page (shown while the workspace server is
  unreachable) is now a styled card with a loading spinner instead of the
  blank "Backend not yet available. Retrying..." page. It still auto-refreshes
  every second.
- The "Workspace server starting" loader spinner's animation duration now
  matches the page's 1-second auto-refresh interval, so the spinner is at
  the cycle boundary (rather than 90 degrees past it) when the reload fires.

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

## 2026-05-09

- Fixed: the `mngr forward` subprocess no longer pegs ~130% CPU after agents disconnect. The bidirectional relay loop (now lifted to a single shared module at `libs/mngr_forward/imbue/mngr_forward/relay.py`, used by both the desktop client's reverse tunnels and `mngr forward`'s direct-tcpip forwards) terminates when the paramiko channel has received EOF; previously `select.select` would mark the channel readable but `recv_ready()` returned False, falling through and spinning the loop at ~1M iters/sec on each half-closed channel.

## 2026-05-06

# mngr_forward plugin

A new `mngr_forward` plugin (in `libs/mngr_forward/`) lands the auth +
subdomain-forwarding logic that used to live inside the minds desktop
client. The plugin runs as a standalone tool:

```bash
mngr plugin enable forward
mngr forward --service system_interface
```

What you get:

- Local proxy on `127.0.0.1:8421` that serves
  `<agent-id>.localhost:8421/*` and byte-forwards each HTTP and WebSocket
  request to the agent's `system_interface` URL via SSH tunnels for
  remote agents.
- One-time login URL printed to stderr (or emitted as a JSONL `login_url`
  event in `--format jsonl`); the resulting cookie is signed with a key
  persisted under `$MNGR_HOST_DIR/plugin/forward/` so browser sessions
  survive plugin restarts.
- `--reverse <remote-port>:<local-port>` (repeatable) sets up reverse SSH
  tunnels for every discovered remote agent. `<remote-port>` may be `0`
  for sshd-assigned ports; the actual bound port is reported via a
  `forward.reverse_tunnel_established` envelope event.
- `--no-observe --forward-port REMOTE_PORT` mode runs `mngr list` once
  and forwards a fixed snapshot. `--no-observe --service NAME` is rejected
  as a CLI usage error.
- `--agent-include` / `--agent-exclude` / `--event-include` /
  `--event-exclude` CEL filters control which agents and event sources
  the plugin tracks.
- `SIGHUP` bounces only the `mngr observe` child subprocess; SSH tunnels,
  per-agent event subprocesses, browser sessions, and the FastAPI app
  stay alive.
