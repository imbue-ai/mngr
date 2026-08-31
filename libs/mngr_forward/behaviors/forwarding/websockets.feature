Feature: WebSocket forwarding
  Agent origins forward WebSocket connections the same way they forward HTTP: an authenticated connection is connected through to the agent's backend and relayed in both directions.
  There is no bare-origin WebSocket surface.

  @ws-relay
  Scenario: An authenticated WebSocket is relayed both ways
    Given a signed-in user
    And a known agent whose backend is routable
    When the user's client opens a WebSocket to the agent origin
    Then a connection is established with the backend at the same path and query
    And the subprotocol the backend negotiates is the one offered to the client
    And text and binary messages are relayed unchanged in both directions
    And when either side closes, the other side is closed too

  @ws-backend-drop-closes-client
  Scenario: When the backend leg ends, the client is closed with a code it can read
    The relay has already accepted the client's WebSocket, so this is a real close frame the client observes -- unlike the pre-accept refusals below.

    Given a relayed WebSocket
    When the backend side ends the connection
    Then the client's WebSocket is closed with code 1011

  @ws-refused-at-handshake
  Rule: A WebSocket the proxy cannot forward is refused at the opening handshake
    The refusal happens before the socket is established, so the client observes a failed upgrade rather than an open socket that later closes with a code.
    That refusal carries no reason on the WebSocket layer: a client learns that it was refused, not why.

    @ws-unknown-host
    Example: A WebSocket to a non-agent host is refused
      When a WebSocket is opened with a host that is not an agent origin
      Then the handshake is refused

    @ws-legacy-host-coordinate
    Example: A WebSocket to a legacy host-coordinate origin is refused
      When a WebSocket is opened to a "host-<hex>" origin
      Then the handshake is refused

    @ws-not-authenticated
    Example: A WebSocket without a valid session is refused before any backend contact
      Given a known agent
      When a WebSocket without a valid session is opened to that agent origin
      Then the handshake is refused
      And the backend is never contacted

    @ws-backend-unavailable
    Example: A WebSocket whose backend is not reachable is refused
      Given a signed-in user
      And a known agent whose backend is not reachable
      When a WebSocket is opened to that agent origin
      Then the handshake is refused

  @ws-no-client-headers
  Rule: The backend WebSocket handshake carries none of the client's headers
    The proxy opens a fresh connection to the backend, forwarding no header the client sent, and stamps only the local owner identity, exactly as the HTTP path does.
    A backend therefore never sees the client's headers, and -- since no inbound identity header survives to be trusted -- cannot be made to forge the owner identity.
