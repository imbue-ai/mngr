Feature: A pre-authorized session for the desktop app
  The desktop app starts the desktop client itself, and its windows arrive already authenticated, so the one-time-code flow never runs in them.
  A desktop client started by the desktop app mints a preauth value at startup and hands it to that app alone; a session cookie exactly equal to it is accepted.
  It mints no one-time code, because nothing would spend one.
  The value is matched exactly rather than verified by signature, so it does not age, and it is not spent by use: it stays valid for the life of the desktop client process and no longer.
  A desktop client started any other way mints none.

  @preauth-no-code
  Scenario: A desktop client started by the desktop app mints a preauth value in place of a one-time code
    Given a desktop client started by the desktop app
    When it has started
    Then it has handed the desktop app a preauth value
    And it has minted no one-time code

  @preauth-accepted
  Scenario: Presenting the exact preauth value counts as authenticated
    Given a desktop client started by the desktop app
    And a browser whose session cookie is exactly its preauth value
    When it requests a page that requires a session
    Then it is treated as authenticated
    And no one-time code is spent

  @preauth-not-replaced
  Scenario: The preauth value is never replaced by a signed cookie
    Given a desktop client started by the desktop app
    And a browser whose session cookie is exactly its preauth value
    When it makes an authenticated request
    Then the response carries no session cookie

  @preauth-near-miss
  Scenario: Anything but the exact preauth value falls back to signature verification
    Given a desktop client started by the desktop app
    And a browser whose session cookie differs from its preauth value in any way
    And that cookie is not a session cookie issued by this installation
    When it requests a page that requires a session
    Then the bearer is treated as unauthenticated

  @preauth-absent
  Scenario: A desktop client not started by the desktop app accepts no preauth value
    Given a desktop client started from a terminal
    When a session cookie that is not a session cookie issued by this installation is presented
    Then the bearer is treated as unauthenticated
