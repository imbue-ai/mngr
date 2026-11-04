Feature: Remote-execution invariants
  These properties hold across every flow and every state, for every workspace and every device.

  @switch-off-by-default
  Rule: The switch is off on a fresh install and belongs to this device alone
    A fresh install allows no agent to run commands on this computer until the user turns the switch on here.
    Turning the switch on for one device never turns it on for another of the user's devices, signed in to the same account or not.

  @three-gates
  Rule: A command runs only when every gate is open
    A command runs on this computer only when the switch is on, the asking workspace holds a remote-execution grant for this device, and the request names this device.
    Turning the switch off refuses the next command of every workspace at once, whatever grants their hosts hold; a command already running finishes.
    A request naming another device, or no valid device, never runs anything here, whatever grants the caller holds.

  @grant-is-its-own-kind
  Rule: No other grant can carry remote execution
    A remote-execution grant comes only from approving a remote-execution request.
    No catalog service's "any" switch, no "all workspaces" grant of the cross-workspace verbs, no baseline a host starts with, and no reconciliation of that baseline ever yields one.
    Approving a remote-execution request is refused while the switch is off, and the request stays pending.

  @agents-only
  Rule: Only agents call the route
    The browser session cookie is refused on the route outright; the gateway bearer and the same-machine socket are the only ways in, and both are held to the switch.

  @every-command-recorded
  Rule: Every command leaves a record
    Every command run for an agent leaves a record on this computer that it started, and one that it ended, saying what was run and how it ended, never what it printed.
