Feature: The bare-origin home page
  "/" on the bare origin is a minimal index of the agents the proxy knows,
  gated by sign-in. It exists for the standalone browser user; an embedding
  host application serves its own UI and does not use it.

  @signed-out-home
  Scenario: Signed-out visitors see the sign-in prompt
    Given the user is not signed in
    When they visit the bare-origin home page "/"
    Then they see a sign-in prompt directing them to the login URL printed in the proxy's terminal
    And the page reveals nothing about existing agents

  @lists-known-agents
  Scenario: Signed-in visitors see every known agent
    Given a signed-in user
    And the proxy knows about one or more agents
    When they visit the bare-origin home page "/"
    Then every known agent is listed
    And each entry links to that agent's workspace through the goto bridge

  @unroutable-still-listed
  Scenario: Agents without a routable backend are listed but marked
    Given a signed-in user
    And a known agent whose backend is not yet known
    When they visit the bare-origin home page "/"
    Then that agent is listed but visibly marked as not yet routable
