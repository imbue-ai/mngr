Feature: Browser-authorization invariants
  These properties hold across all scenarios, all routes, and all interleavings of requests -- including ones no scenario in this area describes.

  @single-use-codes
  Rule: A one-time code grants at most one session, ever
    Over its whole lifetime, each one-time code is spent at most once, and only by the authentication step that establishes a session.
    Every later presentation of the same code is refused; no sequence or interleaving of requests can spend a code twice or authenticate twice from one code.
    Rationale: the authentication URL is written in plain text to the desktop client's own output; single use bounds the damage of that exposure.

  @no-data-without-session
  Rule: No user data without a session
    No request to the browser authorization component without a valid session may ever observe user data -- any response content specific to this user or this installation.
    Whatever an unauthenticated request receives, from any route or surface, is user-independent: the authentication machinery is reachable, but nothing in any response reflects who the user is or what exists in their installation.
    The shape of the refusal varies by surface; the invariant is the absence of user data, not the refusal's shape.

    @unauthenticated-home
    Example: A visitor with no session sees only the authentication prompt at "/"
      Given the user has no session
      When they visit "/"
      Then they see an authentication prompt directing them to the authentication URL printed in the terminal
      And the page reveals nothing about existing machines

    @unauthenticated-arrival
    Example: An arrival at "/post-login" with no session is sent to authenticate, not to a destination
      Given the user has no session
      When they arrive at "/post-login"
      Then they are redirected toward authentication, not to any destination

  @sessions-unforgeable
  Rule: Sessions are unforgeable, tamper-evident, and bounded
    Only session cookies issued by this installation are accepted.
    Any alteration of a cookie invalidates it.
    Cookies issued by another installation (another data directory) are invalid here.
    Cookies older than 30 days are invalid.

  @signing-key-minted-once
  Rule: The signing identity is minted once and never silently replaced
    An installation mints its session-signing identity once, on first need.
    Concurrent first uses agree on a single identity.
    A corrupted or unreadable identity is a hard startup failure -- it is never silently re-minted, because that would invalidate every live session without explanation.
    This is what lets valid sessions keep working across restarts.

  @no-open-redirects
  Rule: User-supplied destinations never leave the origin
    Every redirect destination that arrives from outside the desktop client is honored only when it is a root-relative path on the same origin -- a single leading "/", no scheme, no host, and not a form a browser would resolve as protocol-relative, e.g. (wlog) "/\host".
    Anything else is ignored and the default destination is used.
    No open redirects.

    @post-login-return-to-confined
    Example: A return destination that would leave the origin is not honored at "/post-login"
      Given an authenticated user
      When they arrive at "/post-login" with a return destination that is not a path on this origin
      Then that destination is not honored

  @single-credential
  Rule: The authenticated session is the only credential for reaching your machines through the browser authorization component
    Authenticating a session once, with the one-time code from the terminal, is all it takes to reach every machine on this origin.
    The browser authorization component never asks the user for a second credential to get there.
    Any other credential the product asks for gates an optional cloud-backed feature, never the path to a machine; those credentials are specified elsewhere.

  @fetch-never-spends
  Rule: Merely fetching a URL never spends a code
    Spending a code requires executing the authentication page's script.
    Every URL the system hands out is inert under plain fetching, so software that fetches URLs without executing their scripts cannot consume a code on the user's behalf.
