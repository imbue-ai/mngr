Feature: Session lifetime and integrity
  A successful sign-in establishes a session carried by a signed cookie.
  The proxy persists its cookie-signing key in its state directory, so
  sessions outlive the process that issued them.

  @survives-restart
  Scenario: Sessions survive a forward-proxy restart
    Given a signed-in user
    When the forward proxy is stopped and started again
    And the user reloads the bare-origin home page
    Then they are still signed in
    And they do not need a new login code

  @tampered-token
  Scenario: An altered session cookie is treated as signed out
    Given a signed-in user
    When their session cookie value is modified in any way
    And they request a signed-in page
    Then they are treated as signed out

  @foreign-token
  Scenario: A session minted under a different state directory is not accepted
    Given a session cookie created by a forward proxy with a different state directory
    When it is presented to this proxy
    Then the bearer is treated as signed out

  @expired-token
  Scenario: Sessions expire after 30 days
    Given a session cookie issued more than 30 days ago
    When it is presented
    Then the bearer is treated as signed out

  @signing-key-minted-once
  Rule: The signing identity is minted once and never silently replaced
    The proxy mints its cookie-signing key on first need and reuses it on
    every later run. An unreadable or empty key file is a hard error -- the
    key is never silently re-minted, because that would invalidate every
    live session without explanation. This is what lets valid sessions keep
    working across restarts.
