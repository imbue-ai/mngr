Feature: Forwarding invariants
  These properties hold for every request and connection on every agent origin, across all of this folder's flows.

  @never-serves-host-loopback
  Rule: The proxy never silently serves its own host's loopback in place of an agent
    An agent's registered backend address may be a loopback address that is only meaningful on the host the agent runs on.
    Dialing it from the proxy's own host, when no route to the agent's host is established, would serve whatever happens to be listening on that port locally -- content that is not the agent's, and possibly another program's entirely.
    In that situation the proxy refuses to dial and treats the backend as unavailable.
    An explicit operator opt-in exists for setups that intentionally run agents directly on the proxy's own host.

    @loopback-refused
    Example: A loopback backend with no route to the agent's host is treated as unavailable
      Given a signed-in user
      And a known agent whose registered backend address is a loopback address
      And no route to the agent's own host is established
      When a request arrives for that agent origin
      Then nothing is dialed on the proxy's own host
      And the client is answered as if the backend were unreachable, with HTTP 503
