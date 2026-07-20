Feature: Forward proxy invariants
  These properties hold across every origin the proxy serves, every route,
  and all interleavings of requests -- including flows no scenario in this
  corpus describes.

  @single-use-codes
  Rule: A one-time code grants at most one session, ever
    Over its whole lifetime, each one-time code is spent at most once, and
    only by the authentication step that establishes a session. Every later
    presentation of the same code is refused; no sequence or interleaving of
    requests can spend a code twice or sign in twice from one code.
    Rationale: the login URL is written in plain text to a terminal (and,
    for an embedding host, to the proxy's stdout stream); single use bounds
    the damage of that exposure.

  @fetch-never-spends
  Rule: Merely fetching a URL never spends a code
    Spending a code requires executing the sign-in page's script. Any URL
    the proxy hands out (the printed login URL, links in rendered pages) is
    inert under plain fetching, so preloaders, unfurlers, and prerenderers
    cannot consume a code on the user's behalf.

  @no-data-without-session
  Rule: No agent data or backend content without a session
    A request without a valid session never observes agent ids, the agent
    list, or any byte of any backend's response. The only things an
    unauthenticated request may receive are the sign-in machinery itself
    ("/login", "/authenticate"), a sign-in prompt, a redirect toward
    sign-in or through the bridge that leads there, or an outright refusal.
    The shape of the refusal varies by surface (a sign-in page, a redirect,
    an HTTP 403, a WebSocket close); the invariant is the absence of data,
    not the refusal shape.

  @sessions-unforgeable
  Rule: Sessions are unforgeable, tamper-evident, and bounded
    Only session cookies signed by this proxy's persisted signing key --
    or exactly equal to the preauth value the proxy was started with, if
    any -- are accepted. Any alteration of a cookie invalidates it. Cookies
    signed under another state directory are invalid here. Cookies older
    than 30 days are invalid.

  @single-credential
  Rule: The session is the only credential the user ever handles
    One sign-in grants access to every agent the proxy serves, current and
    future. No flow ever asks the user for a second, per-agent credential;
    the bridge tokens that carry a session across origins are minted and
    redeemed without user interaction.

  @credential-not-forwarded
  Rule: The session credential never reaches agent code
    The session cookie is stripped from every request before it is
    forwarded to an agent's backend; all other cookies pass through
    untouched. Code running inside an agent never observes the credential
    that guards all the other agents.

    @only-cookie-stripped-entirely
    Example: A lone session cookie leaves no cookie header behind
      Given a signed-in request to a workspace origin whose only cookie is the session cookie
      When the request is forwarded to the agent's backend
      Then the forwarded request carries no cookie header at all

  @no-open-redirects
  Rule: User-supplied destinations never leave the origin
    Every redirect destination that arrives from the outside (the "next"
    parameter on the goto bridge and on the workspace origin's
    token-redemption endpoint) is honored only when it is a root-relative
    path on the same origin -- a single leading "/", no scheme, no host,
    and not a protocol-relative form ("//host", "/\host"). Anything else
    is ignored and the default destination "/" is used. No open redirects.

    @unsafe-next-ignored
    Example: A cross-origin destination is replaced with the default
      Given a signed-in user
      When they follow the goto bridge for an agent with a destination of "//evil.example/path"
      Then the destination actually used is "/"
