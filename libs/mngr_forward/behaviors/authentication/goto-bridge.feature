Feature: One sign-in opens every agent origin
  Each agent is served on its own origin, and browsers scope cookies per origin, so a bare-origin session does not by itself authenticate an agent origin.
  The goto bridge closes that gap without user interaction.
  The bare origin's "/goto/<coordinate>/" route mints a short-lived token bound to one agent and redirects the browser to that agent, whose token-redemption endpoint verifies the token, sets the agent's domain-scoped session cookie, and redirects on to the destination.

  Background:
    Given a running forward proxy

  @bridge-roundtrip
  Scenario: Following the bridge lands the user signed in on the agent origin
    Given a signed-in user on the bare origin
    And a known agent
    When the user follows the goto bridge for that agent
    Then the browser is redirected to that agent's origin
    And it arrives signed in there, at the agent path "/"
    And the user was never asked for a credential

  @bridge-covers-service-origins
  Scenario: One bridge hop covers the agent's default service and all its service origins
    The agent session cookie the bridge sets is scoped to the whole agent domain shared by the default service and every service origin.

    Given a signed-in user on the bare origin
    When the user follows the goto bridge for an agent
    Then the session cookie it sets covers that agent's default-service origin and every service origin under the same agent domain
    And no service origin requires its own separate bridge hop

  @bridge-destination
  Scenario: A same-origin destination survives the bridge
    Given a signed-in user on the bare origin
    When they follow the goto bridge for an agent with a destination path "/some/page"
    Then after the bridge they land on "/some/page" on that agent's origin

  @bridge-service-destination
  Scenario: The bridge returns to the exact service origin that sent the user
    Given a signed-in user on the bare origin
    But without an agent session on a particular service origin
    When a navigation to that service origin is sent through the goto bridge
    Then after the bridge they land back on that same service origin

  @bridge-canonicalizes-host-coordinate
  Scenario: The bridge canonicalizes a legacy host coordinate to the agent origin
    Given a signed-in user on the bare origin
    When they follow the goto bridge for an agent named by its legacy host coordinate
    Then they are redirected to that agent's agent-keyed origin

  @bridge-signed-out
  Scenario: The bridge sends signed-out visitors to the bare-origin home
    Given a user who is not signed in on the bare origin
    When they request the goto bridge for any agent
    Then they are redirected with HTTP 302 to the bare-origin home page "/"

  @bridge-unparseable-coordinate
  Scenario: A goto path that does not name a well-formed coordinate is not found
    Given a signed-in user on the bare origin
    When they request the goto bridge for a malformed coordinate
    Then the response is HTTP 404

  @token-bound-to-agent
  Scenario: A bridge token is only good for the agent it was minted for
    Given a bridge token minted for one agent
    When it is presented on a different agent's origin
    Then it is refused with HTTP 403
    And no agent session cookie is set

  @token-expires
  Scenario: A bridge token is short-lived
    The token's validity window is seconds long, so a token that leaks in a history entry, a log, or a copied URL is dead by the time anyone could replay it.

    Given a bridge token minted for an agent
    When it is presented on that agent's origin after its short validity window has passed
    Then it is refused with HTTP 403
    And no agent session cookie is set

  @token-forged
  Scenario: A forged or altered bridge token is refused
    When a token not minted by this proxy, or altered in transit, is presented on an agent origin
    Then it is refused with HTTP 403
    And no agent session cookie is set

  @direct-navigation
  Scenario: Direct navigation heals a missing agent session
    Given a user signed in on the bare origin
    But whose browser has no valid session on some agent origin
    When they navigate directly to a page on that agent origin
    Then they are sent through that agent's goto bridge
    And they end up on the requested page without being asked to sign in

  @signed-out-agent
  Scenario: A fully signed-out visitor to an agent origin ends at the sign-in prompt
    Given a browser with no session of any kind
    When it navigates to a page on an agent origin
    Then it is sent through the goto bridge, which redirects it to the bare origin
    And it ends at the sign-in prompt
