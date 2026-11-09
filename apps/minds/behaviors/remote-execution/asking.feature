Feature: Asking to run commands on this computer
  An agent asks for one device at a time; the user answers in the request popup, which carries the warning, and the switch decides whether an answer can be yes.

  @approve-with-switch-on
  Scenario: Approving with the switch on grants the workspace this device
    Given the switch is on for this device
    And a workspace's agent has asked to run commands on this device
    When the user approves the request in the popup
    Then the workspace's agents may run commands on this device
    And the asking agent is told the request was granted

  @approve-needs-the-switch
  Scenario: With the switch off the popup offers no approval and points at Settings
    Given the switch is off for this device
    And a workspace's agent has asked to run commands on this device
    When the user opens the request in the popup
    Then the popup offers no way to approve and names the setting to turn on
    And the request stays pending

  @ask-for-one-program
  Scenario: Asking for one program grants that program alone
    Given the switch is on for this device
    And a workspace's agent has asked to run one program on this device, by a bare name or a path it already knows
    When the user opens the request in the popup
    Then the popup names the program and the file it is on this device right now, or says it is not there yet
    And approving admits only calls whose program is exactly that program, with any arguments
    And asking for a shell or a language runtime is shown with the same warning as asking for any command

  @revoke-from-permissions
  Scenario: Revoking from the Permissions tab takes the device away
    Given a workspace's agents may run commands on this device
    When the user turns the device's row off in the workspace's Permissions tab
    Then the workspace's agents may no longer run commands on this device
    And a program grant is its own row, taken away on its own
    And nothing in that tab can turn it back on; only a new request can
