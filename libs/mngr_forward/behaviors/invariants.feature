Feature: Forward proxy invariants
  These properties hold across every origin the proxy serves, every route, and all interleavings of requests -- including flows no scenario in this corpus describes.

  @single-use-codes
  Rule: A one-time code grants at most one session, ever
    Over its whole lifetime, each one-time code is spent at most once, and only by the authentication step that establishes a session.
    Every later presentation of an already-spent code is refused, under any interleaving of requests.
    Rationale: the login URL is written in plain text to the proxy's own output, so single use bounds the damage of that exposure.

    @spent-code-refused
    Example: A spent code cannot sign anyone in again
      Given the login URL has already been used to sign in
      When anyone presents the same code for authentication again
      Then authentication is refused
      And no session is established

  @fetch-never-spends
  Rule: Merely fetching a URL never spends a code
    Spending a code requires executing the sign-in page's script.
    Every URL the proxy hands out is inert under plain fetching, so software that fetches a URL without executing its scripts cannot consume a code on the user's behalf.

  @no-data-without-session
  Rule: No agent data or backend content without a session
    No request lacking a valid session ever observes which agents the proxy knows about, or any byte of any backend's response.
    Whatever an unauthenticated request receives is user-independent: the sign-in machinery is reachable, but nothing in any response reflects who the user is or what exists in their installation.
    The shape of the refusal varies by surface; the invariant is the absence of data, not the refusal's shape.

    @unauthenticated-agent-non-html
    Example: A signed-out programmatic request to an agent origin is refused outright
      Given a request with no valid session that does not accept HTML
      When it reaches an agent origin
      Then it is refused with HTTP 403 and no redirect
      And no backend is contacted

  @sessions-unforgeable
  Rule: Sessions are unforgeable, tamper-evident, and bounded
    Only a session cookie signed by this proxy's persisted signing key -- or one exactly equal to the preauth value the proxy was started with, when it has one -- is accepted.
    Any alteration of a cookie invalidates it.
    A cookie signed under a different state directory is invalid here.
    A cookie older than 30 days is invalid.

  @single-credential
  Rule: The session is the only credential the user ever handles
    One sign-in grants access to every agent the proxy serves, current and future.
    No flow asks the user for a second, per-agent credential; the bridge tokens that carry a session across origins are minted and redeemed without user interaction.

  @credential-not-forwarded
  Rule: The session credential never reaches backend code
    The session cookie is stripped from every request before it is forwarded to a backend.
    Code running behind an agent origin never observes the credential that guards every other agent.

    @only-session-cookie-stripped
    Example: A forwarded request carries no session cookie
      Given a signed-in request to an agent origin
      When the request is forwarded to its backend
      Then the forwarded request carries no mngr_forward session cookie

  @no-open-redirects
  Rule: User-supplied destinations never leave the origin
    Every redirect destination that arrives from outside the proxy is honored only when it is a root-relative path on the same origin -- a single leading "/", no scheme, no host, and not a form a browser resolves as protocol-relative.
    Anything else is ignored and the default destination "/" is used.
    No open redirects.

    @unsafe-next-ignored
    Example: A cross-origin destination is replaced with the default
      Given a signed-in user
      When they follow the goto bridge for an agent with a destination that is not a path on this origin
      Then the destination actually used is "/"
