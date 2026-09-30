Feature: Scheduling an update
  A scheduled or bulk update runs in a configured local window, usually while nobody is in the workspace.
  The update flow is unattended by design, so a scheduled run is the same run as one started by hand, only later; the app's job is the window, the gate on the workspace's state, and putting the workspace back afterwards.
  The window is configurable to any hours, so the app describes it as the update window rather than as a night.

  @no-backup-confirmation
  Scenario: An update on a machine without backups is confirmed at the button
    The go-ahead is collected once, at the button, and carried into the run: a workspace whose update flow predates the unattended one stops at its own missing-restore-point question, and nobody is necessarily watching to answer it.
    Given an out-of-date workspace without backups configured
    When the user presses Update now or Schedule update for that workspace
    Then the app asks in place whether to go ahead without backups
    And only a confirmed press starts or schedules the update
    And the update agent is told the user chose to go ahead without a restore point
    And a workspace that does have backups is told nothing of the sort

  @updated-note
  Scenario: A run that landed leaves a dismissible note on the workspace's row
    The same note whether the run was scheduled or watched: it is news about the workspace, not a report on the user's attention.
    Given a workspace whose update landed
    Then the workspace's row notes the version it was updated to
    And the user can dismiss that note
    And a dismissed note stays dismissed after the app relaunches
    And dismissing it does not clear an unread failure of a later run

  @skipped-window
  Scenario: A workspace that is not in a fit state is skipped and tried again
    Given a workspace with a scheduled update
    And agents are working in that workspace
    When the scheduled attempt comes around
    Then no update agent is started
    And the update stays scheduled
    And the app reports why the attempt was skipped, in terms of the update window rather than a night

  @bulk-unsent-update-is-reported
  Scenario: A workspace a bulk update did not update says why on its row
    The bulk action answers before any workspace is tried, so each workspace's row is the only place its outcome can land. The press was for now, not for a window, so the reason is not put in terms of one.
    Given several out-of-date workspaces, one of which cannot be reached or has agents working in it
    When the user updates them all now
    Then that workspace's row reports that its update didn't start, and why

  @re-arming-replaces
  Scenario: Re-scheduling replaces the previous intent outright
    Given a workspace whose scheduled update was skipped
    When the user schedules that workspace's update again
    Then the app no longer reports the earlier skip

  @prior-run-state-restored
  Scenario: A workspace started for a scheduled update is put back afterwards
    Given a stopped workspace with a scheduled update
    When the scheduled attempt runs that update to completion
    Then that workspace is stopped again

  @failure-cancels-the-schedule
  Scenario: An update that fails is not retried unwatched
    Given a workspace with a scheduled update
    When that update runs and fails
    Then the update is no longer scheduled
