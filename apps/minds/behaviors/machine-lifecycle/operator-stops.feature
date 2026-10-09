Feature: Stops the owner did not ask for
  A remote machine can be stopped by the owner from another device, by an operator holding it for maintenance, by an operator freeing capacity, by the account's suspension, or by an operator retiring it for good.
  The app tells a held machine from one the owner may start, and never treats a stop someone asked for as a machine that wedged.

  @held-machine-maintenance
  Scenario: A held machine says so and offers no Start
    Given a remote machine whose stop kind is maintenance
    When the machines list shows it
    Then its badge reads "Maintenance" instead of "Stopped"
    And no Start control is offered for it
    And opening it shows "This machine is undergoing maintenance and will be back shortly." over the machine

  @retired-machine
  Scenario: A retired machine says it never starts again and points at its backups
    A retirement is final: the machine's data was archived for its owner and nobody, the owner included, starts it again.
    Given a remote machine whose stop kind is retired
    When the machines list shows it
    Then its badge reads "Retired" instead of "Stopped"
    And no Start control is offered for it
    And opening it shows "This machine has been retired and cannot be started again. Download its data from its backups, or contact support if it has none." over the machine
    And a start of it the connector refuses shows that same sentence and is not reported as a failed recovery

  @owner-startable-idle
  Scenario: A machine stopped to free capacity is the owner's to start
    Given a remote machine whose stop kind is idle
    When the machines list shows it
    Then its badge reads "Stopped"
    And a Start control is offered for it
    And pressing Start starts the machine

  @no-unattended-start-of-a-requested-stop
  Scenario: The app never starts a remote machine whose stop was requested
    The app starts a machine unasked only when it wedged; a machine the connector reports stopping, stopped or starting is down because someone asked for that.
    Given a remote machine the app was displaying
    And someone else's stop takes the machine down while the app is displaying it
    When the app decides the machine has stopped answering
    Then the app asks the connector for the machine's lifecycle before starting it
    And the app does not start a machine the connector reports stopping, stopped or starting

  @held-start-refused-plainly
  Scenario: A start the connector refuses as a hold is not a failure
    Given a remote machine whose stop kind is maintenance
    When a start of it is refused by the connector
    Then the app shows "This machine is undergoing maintenance and will be back shortly."
    And the app does not report a failed recovery

  @unknown-stop-kind-not-actionable
  Scenario: A stop kind this build does not know is treated as a hold
    Given a remote machine whose stop kind this app version does not recognize
    When the machines list shows it
    Then no Start control is offered for it
    And the app dispatches no start of it, answering a start deeplink with the maintenance sentence
    And a start run through mngr is refused with a message telling the user to update the app
