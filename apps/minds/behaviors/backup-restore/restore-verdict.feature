Feature: Restore verdict
  An in-place restore reports one verdict for the whole operation once it has rewound the data and brought the workspace's services back up.
  Full recovery is plain success.
  Recovery of every restore-critical service, with some service that was up when the restore started still down, is success with a warning naming those services.
  A restore-critical service failing to come back is failure.

  @full-recovery
  Scenario: Every service comes back and the restore succeeds without caveats
    Given a workspace with a backup snapshot
    When the user restores that snapshot in place
    And every workspace service comes back up
    Then the restore reports success
    And the restore reports no warning

  @system-interface-down
  Scenario: The system interface does not come back and the restore fails
    Given a workspace with a backup snapshot
    When the user restores that snapshot in place
    And the system interface fails to come back up
    Then the restore reports failure
    And the failure names the system interface

  @restore-critical-set
  Rule: The verdict gates on restore-critical services and on nothing else
    A restore may not fail over a service outside the restore-critical set, however many such services are down.
    A restore may not succeed while any restore-critical service is down.
    The system interface is currently the sole restore-critical service: a restored workspace whose system interface is up can be used, inspected, and restored again, whatever else is still converging.

    @backup-service-down
    Example: The backup service itself is not restore-critical
      Given a workspace with a backup snapshot
      When the user restores that snapshot in place
      And the system interface comes back up
      But the backup service fails to come back up
      Then the restore reports success
      And the restore's warning names the backup service

  @only-services-that-were-up
  Rule: Only a service that was up when the restore started can fail to come back
    A service already down when the restore begins is in the state the restore found it in, so the restore neither names it nor waits for it.
    Naming it would attribute a standing condition to the restore, and waiting for it would cost every restore the whole service-recovery wait.
    This does not relax the restore-critical set, whose services must be up after the restore whatever their state before it.

    @down-before-and-after
    Example: A service that was already down is not reported as failing to come back
      Given a workspace with a backup snapshot
      And a workspace service that is not running
      When the user restores that snapshot in place
      And the system interface comes back up
      But that service is still not running
      Then the restore reports success
      And the restore reports no warning

  @services-started-on-their-own
  Rule: The restore brings back and judges only the services the workspace starts on its own
    A restore brings the workspace's services back the way the workspace's own startup would, from the service configuration the restored data holds.
    A service configured not to start on its own stays stopped after a restore, whether or not it was running when the restore started, and its being stopped is neither a failure nor a warning.

    @service-off-by-default
    Example: A service configured not to start on its own stays stopped without a warning
      Given a workspace with a backup snapshot
      And a service configured not to start on its own
      When the user restores that snapshot in place
      And every other workspace service comes back up
      Then the service configured not to start on its own is stopped
      And the restore reports success
      And the restore reports no warning
