Feature: Restore verdict
  An in-place restore reports one verdict for the whole operation once it has rewound the data and brought the machine's services back up.
  Full recovery is plain success.
  Recovery of every restore-critical service with any other services still down is success with a warning naming those services.
  A restore-critical service failing to come back is failure.

  @full-recovery
  Scenario: Every service comes back and the restore succeeds without caveats
    Given a machine with a backup snapshot
    When the user restores that snapshot in place
    And every machine service comes back up
    Then the restore reports success
    And the restore reports no warning

  @system-interface-down
  Scenario: The system interface does not come back and the restore fails
    Given a machine with a backup snapshot
    When the user restores that snapshot in place
    And the system interface fails to come back up
    Then the restore reports failure
    And the failure names the system interface

  @restore-critical-set
  Rule: The verdict gates on restore-critical services and on nothing else
    A restore may not fail over a service outside the restore-critical set, however many such services are down.
    A restore may not succeed while any restore-critical service is down.
    The system interface is currently the sole restore-critical service: a restored machine whose system interface is up can be used, inspected, and restored again, whatever else is still converging.

    @backup-service-down
    Example: The backup service itself is not restore-critical
      Given a machine with a backup snapshot
      When the user restores that snapshot in place
      And the system interface comes back up
      But the backup service fails to come back up
      Then the restore reports success
      And the restore's warning names the backup service
