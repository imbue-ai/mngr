Feature: WebSocket forwarding
  Workspace origins forward WebSocket connections the same way they forward
  HTTP: an authenticated connection is connected through to the agent's
  backend and relayed in both directions. There is no bare-origin WebSocket
  surface. Close codes are the contract here -- a programmatic client can
  distinguish a routing failure, an authentication failure, and the two
  kinds of backend failure by code alone.

  @ws-relay
  Scenario: An authenticated WebSocket is relayed both ways
    Given a signed-in user
    And a known agent whose backend is routable
    When the user's client opens a WebSocket to the agent's workspace origin
    Then a connection is established with the agent's backend at the same path and query
    And the subprotocol the backend negotiates is the one offered to the client
    And text and binary messages are relayed unchanged in both directions
    And when either side closes, the other side is closed too

  @ws-unknown-host
  Scenario: A WebSocket to a non-workspace host is closed at once
    When a WebSocket connection is opened with a host that is not a workspace origin
    Then it is closed with code 4004

  @ws-not-authenticated
  Scenario: A WebSocket without a valid session is closed before any backend contact
    Given a known agent
    When a WebSocket without a valid session is opened to that agent's workspace origin
    Then it is closed with code 4003
    And the backend is never contacted

  @ws-backend-not-routable
  Scenario: A WebSocket to an agent whose backend is not yet known is closed as try-again
    Given a signed-in user
    And a known agent whose backend is not yet known
    When a WebSocket is opened to that agent's workspace origin
    Then it is closed with code 1013

  @ws-backend-unreachable
  Scenario: A WebSocket whose backend cannot be reached is closed as a backend failure
    Given a signed-in user
    And a known agent whose backend is routable
    But neither the backend nor the route to the agent's host can actually be reached
    When a WebSocket is opened to that agent's workspace origin
    Then it is closed with code 1011
