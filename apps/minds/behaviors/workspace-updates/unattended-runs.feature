Feature: Scheduling an update
  A scheduled update runs in a configured window of local hours, usually while nobody is in the workspace.
  The workspace owns its schedule: scheduling writes the window into the workspace, and the workspace starts the run itself, so neither the app nor the user's computer has to be running then.
  The update flow is unattended by design, so a scheduled run is the same run as one started by hand, only later.
  The window is configurable to any hours, so the app describes it as the update window rather than as a night.

  @no-backup-confirmation
  Scenario: An update on a workspace without backups is confirmed at the button
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

  @runs-without-the-app
  Scenario: A scheduled update starts inside its window with the app closed
    Given a running workspace with a scheduled update
    And the app is not running
    When the update window opens in the user's time zone
    Then an update agent is started inside that workspace
    And no second update agent is started there for the rest of that window

  @window-in-the-users-zone
  Scenario: The window is read in the time zone of the computer that scheduled it
    The workspace may run on a machine with any clock; the hours the user chose are the user's.
    Given the user's computer is in one time zone and the workspace's machine in another
    When the user schedules a workspace's update
    Then the update starts within the configured hours of the user's time zone, across daylight-saving changes

  @gone-agent-restarted-in-window
  Scenario: A scheduled update whose agent vanished before it began is started again in the same window
    Given a workspace whose scheduled update agent was started this window
    And that agent is gone without ever recording that its run began
    When the window is still open
    Then another update agent is started inside that workspace

  @skipped-window
  Scenario: A window that closes without the update starting says why, and the next window tries again
    The workspace keeps trying for as long as the window is open; only a window that closes with nothing started is reported.
    Given a workspace with a scheduled update
    And another update is in flight there, or another update flow holds the workspace, or no AI account is signed in
    When the update window closes without the update starting
    Then the update stays scheduled
    And the app reports why, in terms of the update window rather than a night, and that it will try again in the next window

  @stopped-workspace-not-updated
  Scenario: A workspace that is stopped during its window is not updated, and says nothing about it
    The schedule lives in the workspace, so a stopped workspace has nothing running that could start it; the app no longer starts or stops workspaces for a schedule.
    Given a workspace with a scheduled update that is stopped through the whole window
    Then no update agent is started
    And no skipped window is reported
    And the app shows the workspace as scheduled again once it is running

  @schedule-needs-a-running-workspace
  Scenario: Scheduling needs the workspace running
    Given a stopped out-of-date workspace
    When the user schedules that workspace's update
    Then the app says the workspace must be running, during the update window too

  @no-schedule-during-run
  Scenario: A workspace whose update is running cannot be scheduled
    A schedule set while a run is under way would outlive it, and start a fresh run in the next window even after the run ended waiting for the user's decision.
    Given a workspace with an update in flight
    When the user schedules that workspace's update
    Then the update is not scheduled
    And the app says an update is already running in that workspace

  @no-cron
  Scenario: A workspace that cannot run scheduled jobs is told to update now instead
    Given an out-of-date workspace with no scheduler of its own
    When the user schedules that workspace's update
    Then the update is not scheduled
    And the app offers updating it now instead, which works there

  @schedule-follows-the-app
  Scenario: An armed schedule follows the app's settings and release
    Given a workspace with a scheduled update
    When the user changes the update window, or the app moves to a newer release
    Then the workspace's schedule uses the new window and release from the app's next read of it
    And a version the user named for that schedule is kept

  @needs-sign-in
  Scenario: A workspace with no AI account signed in is told so, with a way to sign in
    Given an out-of-date workspace with no AI account signed in
    When the user schedules or starts that workspace's update, or its scheduled window finds no account
    Then the workspace's row says it needs a sign-in
    And the app offers a button that opens a new chat in that workspace with the provider chooser up
    And the row stops saying so once an account is signed in there

  @bulk-unsent-update-is-reported
  Scenario: A workspace a bulk update did not update says why on its row
    The bulk action answers before any workspace is tried, so each workspace's row is the only place its outcome can land.
    Given several out-of-date workspaces, one of which cannot be reached or refuses the update agent
    When the user updates them all now
    Then every other workspace's update starts, whatever is running in it
    And that workspace's row reports that its update didn't start, and why

  @patch-available-schedule-runs
  Scenario: A scheduled update armed by hand on a workspace with only a patch available runs
    No schedule is ever armed for such a workspace except by the user's own press.
    Given a workspace with only a patch available
    And the user has scheduled that workspace's update
    When the update window opens
    Then an update agent is started in that workspace

  @cancel-needs-the-workspace
  Scenario: A cancel that cannot reach the workspace leaves the schedule in place
    Given a workspace with a scheduled update that cannot be reached
    When the user cancels that schedule
    Then the app says the cancel did not go through
    And the workspace's row still shows it as scheduled

  @failure-cancels-the-schedule
  Scenario: An update that fails is not retried unwatched
    A retry needs something to have changed.
    Given a workspace with a scheduled update
    When that update runs and fails
    Then the update is no longer scheduled
