Feature: The bare-origin home page
  "/" on the bare origin is a minimal index of the agents the proxy has discovered, gated by sign-in.
  It exists for the standalone browser user; an embedding host serves its own UI instead.

  @signed-out-home
  Scenario: A signed-out visitor sees only the sign-in prompt
    Given the user is not signed in
    When they visit the bare-origin home page "/"
    Then they see a sign-in prompt directing them to the login URL printed in the proxy's terminal
    And the page reveals nothing about which agents exist

  @lists-known-agents
  Scenario: A signed-in visitor sees every discovered agent
    Given a signed-in user
    And the proxy has discovered one or more agents
    When they visit the bare-origin home page "/"
    Then every discovered agent is listed by its agent id
    And each reachable agent links to its own origin through the goto bridge

  @unresolved-still-listed
  Scenario: An agent whose backend is not yet resolved is listed but marked
    Given a signed-in user
    And a discovered agent whose backend has not been resolved yet
    When they visit the bare-origin home page "/"
    Then that agent is listed but visibly marked as not yet reachable
