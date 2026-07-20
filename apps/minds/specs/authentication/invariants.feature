Feature: Authentication invariants
  These properties hold across all scenarios, all routes, and all
  interleavings of requests -- including ones no scenario in this area
  describes.

  @single-use-codes
  Rule: A one-time code grants at most one session, ever
    Over its whole lifetime, each one-time code is spent at most once, and
    only by the authentication step that establishes a session. Every later
    presentation of the same code is refused; no sequence or interleaving of
    requests can spend a code twice or sign in twice from one code.
    Rationale: the login URL is written in plain text to a terminal and an
    event stream; single use bounds the damage of that exposure.

  @no-data-without-session
  Rule: No user data without a session
    No request without a valid session may ever observe user data: workspace
    names or ids, account details, settings, or any per-user content. The
    only things an unauthenticated request may receive are the sign-in
    machinery itself ("/login", "/authenticate"), a sign-in prompt, a
    redirect toward sign-in, an authentication refusal, or an inert
    application shell with no data in it.
    The shape of the refusal varies by surface today (an HTTP 403, a
    redirect, a placeholder page, an auth-required event on a stream); the
    invariant is the absence of data, not the refusal shape.

  @sessions-unforgeable
  Rule: Sessions are unforgeable, tamper-evident, and bounded
    Only session tokens issued by this installation are accepted. Any
    alteration of a token invalidates it. Tokens issued by another
    installation (another data directory) are invalid here. Tokens older
    than 30 days are invalid.

  @signing-key-minted-once
  Rule: The signing identity is minted once and never silently replaced
    An installation mints its session-signing identity once, on first need.
    Concurrent first uses agree on a single identity. A corrupted or
    unreadable identity is a hard startup failure -- it is never silently
    re-minted, because that would invalidate every live session without
    explanation. This is what lets valid sessions keep working across
    restarts.

  @no-open-redirects
  Rule: User-supplied destinations never leave the origin
    Every redirect destination that arrives from the outside (the
    "return_to" parameter on "/post-login" and the account pages, the "next"
    parameter on the goto bridge) is honored only when it is a root-relative
    path on the same origin -- a single leading "/", no scheme, no host, and
    not a protocol-relative form ("//host", "/\host"). Anything else is
    ignored and the default destination is used. No open redirects.

  @single-credential
  Rule: The session is the only credential the user ever handles
    One sign-in grants access to every workspace the user has, current and
    future. No flow ever asks the user for a second, per-workspace
    credential.

  @fetch-never-spends
  Rule: Merely fetching a URL never spends a code
    Spending a code requires executing the sign-in page's script. Any URL
    the system hands out (the printed login URL, links in rendered pages) is
    inert under plain fetching, so preloaders, unfurlers, and prerenderers
    cannot consume a code on the user's behalf.

  @credential-not-forwarded
  Rule: The session credential never reaches workspace code
    The session cookie is stripped from requests before they are forwarded
    to a workspace's own server. Code running inside a workspace never
    observes the credential that guards all the other workspaces.
