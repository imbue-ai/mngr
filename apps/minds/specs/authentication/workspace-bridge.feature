Feature: One sign-in opens every workspace
  Each workspace is served on its own origin. The bare-origin session is
  bridged to each workspace origin automatically: the user signs in once,
  ever, per browser.

  @open-from-landing
  Scenario: Opening a workspace from the landing page needs no second sign-in
    Given a signed-in user with a workspace
    When they open that workspace from the landing page
    Then the workspace UI loads
    And they are not asked to sign in again

  @direct-navigation
  Scenario: Direct navigation to a workspace origin heals a missing workspace session
    Given a user who is signed in on the bare origin
    But whose browser has no (or a stale) session for a workspace's own origin
    When they navigate directly to that workspace's address
    Then they end up in the workspace UI without being asked to sign in

  @signed-out-workspace
  Scenario: A fully signed-out visitor to a workspace address ends at the sign-in prompt
    Given a browser with no session of any kind
    When it navigates to a workspace address
    Then it is redirected to the bare origin's sign-in prompt

  @non-html-refused
  Scenario: Signed-out programmatic requests to a workspace are refused outright
    Given a request with no session that does not accept HTML (an API call, an asset fetch)
    When it reaches a workspace address
    Then it is refused (HTTP 403) with no redirect
