Feature: One sign-in opens every workspace origin
  Each agent's UI is served on its own workspace origin, and browsers scope
  cookies per origin -- so a bare-origin session does not authenticate a
  workspace origin by itself. The goto bridge closes that gap without user
  interaction: the bare origin's "/goto/<agent-id>/" route mints a
  short-lived token tied to one agent and redirects the browser to that
  agent's workspace origin, whose token-redemption endpoint verifies the
  token, sets the workspace origin's own session cookie, and redirects on
  to the destination.

  Background:
    Given a running forward proxy

  @bridge-roundtrip
  Scenario: Following the bridge lands the user signed in on the workspace origin
    Given a signed-in user on the bare origin
    And a known agent
    When the user follows the goto bridge for that agent
    Then the browser is redirected to that agent's workspace origin
    And arrives signed in there, at the workspace path "/"
    And the user was never asked for a credential

  @bridge-destination
  Scenario: A same-origin destination survives the bridge
    Given a signed-in user on the bare origin
    When they follow the goto bridge for an agent with a destination path "/some/page"
    Then after the bridge they land on "/some/page" on that agent's workspace origin

  @bridge-signed-out
  Scenario: The bridge sends signed-out visitors to the bare-origin home
    Given a user who is not signed in
    When they request the goto bridge for any agent
    Then they are redirected (HTTP 302) to the bare-origin home page "/"

  @bridge-unparseable-agent
  Scenario: A goto path that does not name a well-formed agent id is not found
    Given a signed-in user
    When they request the goto bridge for a malformed agent id
    Then the response is HTTP 404

  @token-bound-to-agent
  Scenario: A bridge token is only good for the agent it was minted for
    Given a bridge token minted for one agent
    When it is presented on a different agent's workspace origin
    Then it is refused (HTTP 403)
    And no workspace-origin session is set

  @token-expires
  Scenario: A bridge token is short-lived
    The token's validity window is seconds long, which keeps it effectively
    one-shot: a token that leaks (in a history entry, a log, a copied URL)
    is dead by the time anyone could replay it.

    Given a bridge token minted for an agent
    When it is presented on that agent's workspace origin after its short validity window has passed
    Then it is refused (HTTP 403)
    And no workspace-origin session is set

  @token-invalid
  Scenario: A forged or altered bridge token is refused
    When a token not minted by this proxy, or altered in transit, is presented on a workspace origin
    Then it is refused (HTTP 403)
    And no workspace-origin session is set

  @direct-navigation
  Scenario: Direct navigation heals a missing workspace-origin session
    Given a user who is signed in on the bare origin
    But whose browser has no (or a stale) session for some workspace origin
    When they navigate directly to that workspace origin in a browser
    Then they are sent through the goto bridge for that agent
    And end up in the workspace without being asked to sign in

  @signed-out-workspace
  Scenario: A fully signed-out visitor to a workspace origin ends at the sign-in prompt
    Given a browser with no session of any kind
    When it navigates to a workspace origin
    Then it is redirected to the bare origin
    And ends at the sign-in prompt

  @non-html-refused
  Scenario: Signed-out programmatic requests to a workspace origin are refused outright
    Given a request with no valid session that does not accept HTML (an API call, an asset fetch)
    When it reaches a workspace origin
    Then it is refused (HTTP 403) with no redirect
