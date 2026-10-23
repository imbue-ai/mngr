Feature: Running an update
  Most of a run is preparation that leaves the live workspace untouched, so the workspace's lifecycle actions stay available through it; only the apply step, which rewrites the workspace, makes stopping it a question worth asking first.
  A run that ends reports exactly one outcome.

  @attended-dispatch
  Scenario: Starting an update takes the user to the conversation performing it
    The workspace's interface opens the conversation's tab only for clients connected when the agent appears, so the app shows the workspace first.
    Given an out-of-date workspace
    When the user starts an update for that workspace
    Then the app shows the user that workspace
    And an update agent is then started inside that workspace

  @stopped-workspace-started
  Scenario: A stopped workspace is started before its update runs
    Given an out-of-date workspace that is stopped
    When the user starts an update for that workspace
    Then that workspace is started
    And an update agent is started inside that workspace
    And that workspace stays running after the update

  @running-workspace-not-restarted
  Scenario: A running workspace is updated without being started again
    Starting a workspace that is already up takes as long as starting the update itself, and shows the workspace as starting for all of it.
    Given an out-of-date workspace that is running
    When the user starts an update for that workspace
    Then that workspace is not started again
    And an update agent is started inside that workspace

  @template-too-old
  Scenario: A workspace whose template cannot update itself is refused
    Given an out-of-date workspace whose template predates the update capability
    When the user starts an update for that workspace
    Then no update agent is started
    And the app explains that the workspace cannot update itself

  @workspace-refuses-the-agent
  Scenario: A workspace that refuses to start the update agent has its refusal shown
    Whether an agent can run at all is the workspace's own decision, so its refusal is shown in its own words rather than a reason the app composes. That refusal is one the user clears themselves, so the run has to be startable again the moment they have. A workspace with no AI account signed in is reported as needing a sign-in instead (see Scheduling an update).
    Given an out-of-date workspace that refuses to start any agent in it
    When the user starts an update for that workspace
    Then no update agent is started
    And the app shows the workspace's own refusal
    And starting that workspace's update again is not refused as one already in flight

  @unsent-update-is-reported
  Scenario: An update that never went out says why on the workspace
    Reaching a workspace, starting it, and creating the agent can each take long enough that the window that asked is gone by the time the answer comes back, so the answer is kept on the workspace, where every update surface reads it, rather than only in the reply to the press.
    Given an out-of-date workspace that cannot be reached, cannot be started, or refuses the update agent
    When the user starts an update for that workspace
    And the user closes the window that asked before the app hears back
    Then the workspace's row reports that its update didn't start
    And opening that workspace's update again says why, with the underlying error message when there was one
    And that report is gone once an update of that workspace goes out or is scheduled, or once the user dismisses it

  @stop-mid-apply-is-confirmed
  Scenario: Stopping a workspace while its update is being applied asks first
    A run that is only preparing has changed nothing, so it withholds nothing; the apply is the one step a stop can leave half-done.
    Given a workspace with an update in flight that has reached its apply step
    When the user asks to stop or restart that workspace
    Then the app warns that stopping now can leave the workspace half-updated and asks whether to go ahead
    And a workspace whose update is still preparing is stopped without that warning

  @version-override
  Scenario: The user may name the exact version an update targets
    The field that takes the version warns that the app cannot vouch for it, and pressing it is the confirmation.
    Given a workspace that is up to date
    When the user starts an update to a version they name themselves
    Then an update agent is started inside that workspace with that version as its target
    And that agent treats the version as already confirmed rather than asking the user to confirm it again

  @scheduled-version-override
  Scenario: A named version may be scheduled as well as run now
    Pressing the version field is the confirmation whether the run goes out now or in the next update window; scheduling only changes when.
    Given a workspace that is up to date
    When the user schedules an update to a version they name themselves
    Then the schedule records that version
    And the update agent the schedule starts is given that version as its target

  @one-seed-message
  Scenario: Every run is started with the same message
    The update flow is unattended by design: it ends by reporting what changed and offering a rollback, whether or not anyone watched. So a run carries no consent tier of its own, and the agent is never told whether it was scheduled or pressed: only the slash command, the release it targets and the app's ceiling, and the answers the app already collected at the button that an older workspace's flow would otherwise stop to ask for again.
    The release and ceiling travel in the message so the agent never has to reach the app for them.
    Given an out-of-date workspace with backups configured
    When an update is started for that workspace by any path
    Then the update agent's seed message is the update command with the release it targets and the app's ceiling
    And the update chat is named for that release

  @chats-pause-notice
  Scenario: Updating now says that running chats pause while the update lands
    Given an out-of-date workspace
    When the user opens that workspace's update
    Then the app says that running chats pause for a few minutes while the update is applied, then continue

  @needs-decision
  Scenario: An update that cannot keep something the user built ends waiting for their decision
    Nothing is held open waiting: the run ends, and its chat is where the user decides.
    Given a workspace whose update agent found that the update cannot keep something the user built
    Then the run ends without changing the workspace
    And the workspace's row says a decision is pending
    And its update shows the agent's line about what it cannot keep, with a button into the run's chat and no Update now

  @dismiss-decision-skips-release
  Scenario: Dismissing a decision skips the release it was for
    A fresh run of the same release would set aside the work the last one kept for the decision, so dismissing the decision is a choice not to take that release.
    Given a workspace whose update is waiting for a decision
    When the user dismisses it
    Then the app says which release is skipped and what skipping it means, and skips it only once the user confirms
    When the user confirms
    Then the workspace no longer reports a decision pending
    And it is not updated automatically to that release

  @stopped-workspace-quiet
  Scenario: A stopped workspace says nothing about its update until it runs again
    Nothing about a workspace's update can be read from it or done in it while it is stopped, and what the user has not dismissed comes back on the first read after it starts.
    Given a workspace that is stopped or stopping
    Then the app says nothing about whether that workspace is behind, scheduled, or skipped, or about how its last update ended
    And its Updates settings, and an update window open over it, say to start the workspace to see its updates
    But the app still reports an update of it that is landing, an update it is starting that workspace for, an update of it that never went out, and that it is too old to update in place
    When that workspace is running again
    Then the app reports its update state again, without what the user dismissed

  @open-chat-from-outcome
  Scenario: A run's chat is one click away from where its progress or outcome is reported
    A scheduled run shows no window of its own while it runs, so its report is the way in.
    Given a workspace whose last update landed or is waiting for a decision
    Then the outcome on the workspace's row offers a button into the run's chat, itself or in the window it opens
    And the outcome over the workspace offers a button into the run's chat and a way to dismiss it
    Given a workspace whose update is preparing, applying, or waiting for the user, and whose run's chat exists
    Then the report over the workspace offers a button into the run's chat
    And the update on the workspace's row opens the run's chat itself, not a window that only offers it

  @idle-agent-reminded
  Scenario: An update agent that sits idle without a result is reminded
    Given a workspace with an update in flight
    And its agent and the worker it handed off to are both idle, with no result recorded
    Then the workspace reminds the agent that it owes a result
    And it is not reminded again for a while

  @failed-outcome-shows-details
  Scenario: An update that failed is reported with a way to its details
    Whether the run's chat can still be opened is a question its details answer, so the report over the workspace does not promise it.
    Given a workspace whose last update failed
    Then the outcome over the workspace offers a way to see its details and a way to dismiss it
    And its details offer a button into the run's chat only while that chat exists
    And so does the outcome on the workspace's row

  @stopped-agent-revived
  Scenario: An update whose agent stopped carries on
    A workspace restart stops every chat in it, and the user still wants the update that was under way.
    Only the agent can tell a loop from bad luck, so it is told how many times it has been restarted and decides when to give up.
    Given a workspace with a scheduler of its own and an update in flight
    And its agent stopped without recording a result
    Then the workspace starts the agent again, telling it how many times it has been restarted
    And the app still reports that workspace as updating
    And starting another update of that workspace is answered with the one under way

  @gone-agent-closed-out
  Scenario: An update whose chat was deleted is closed out by the workspace
    Deleting the update's chat is how the user cancels an update under way.
    Given a workspace with an update in flight
    And its chat was deleted without recording a result
    Then the workspace records the update as stuck
    And the worker it handed off to is stopped with its work kept
    And the leases that agent held are released
    And the app reports that the update didn't finish, without a button into its chat
    And an update of that workspace can be started again at once

  @stalled-run-resumes
  Scenario: An update reported as failed because its agent stopped is reported as updating once its agent is back
    The app reports a run as failed when its agent stopped and nothing in the workspace would start it again.
    Something can still bring that agent back (the workspace's scheduler returning, or the user starting the chat), and the update then carries on.
    Given the app reports a workspace's update as failed because its agent stopped, with no result recorded
    When the agent is running again, or stopped where the workspace's scheduler will start it again
    Then the app reports that workspace as updating
    But a workspace that cannot be reached leaves the report as it is

  @one-run-per-workspace
  Rule: A workspace runs at most one update at a time
    A second request to update a workspace that is already updating starts nothing.

  @run-liveness-is-observed
  Rule: Whether a run is still going is established by asking the workspace, never by elapsed time
    A run is treated as still going unless the workspace positively reports that it is not.

    @unreachable-workspace-stays-updating
    Example: A running workspace that cannot be reached is still reported as updating
      Given a workspace with an update in flight
      And that workspace is not stopped, but cannot be reached
      Then the app still reports that workspace as updating

    @waiting-run-surfaced
    Example: A run whose agent has stopped to wait is reported as waiting for the user
      A single idle reading can catch an agent between two moments of work, so only repeated idleness is surfaced.
      Given a workspace with an update in flight
      And the workspace repeatedly reports the run's agent as alive but idle
      Then the app reports that update as waiting for the user

    @idle-lead-with-a-working-worker-is-not-waiting
    Example: A run whose agent is idle only because its worker is working is still reported as updating
      Given a workspace with an update in flight
      And the run records the worker it has handed its work to
      And the workspace reports the run's agent as alive but idle and that worker as working
      Then the app still reports that update as preparing

    @idle-lead-on-a-background-task-is-not-waiting
    Example: A run whose agent is idle only because it waits on a background task is still reported as updating
      An agent that ended its turn to wait for a command it started in the background will resume on its own when that command finishes, so nothing is waiting on the user.
      A workspace whose template cannot report background tasks reports none.
      Given a workspace with an update in flight
      And the workspace repeatedly reports the run's agent as alive but idle
      And the workspace reports that agent's conversation as waiting on a background task
      Then the app still reports that update as preparing

    @hold-is-reported-with-its-detail
    Example: A run that has stopped for the user's go-ahead says what it is waiting on
      A recorded hold is the run's own statement, so it is surfaced without waiting for repeated idleness; its one line of detail, written for the user, is what the app shows.
      A run holds only while it waits for the user to confirm a version past the app's; something the update cannot keep ends the run with a decision instead.
      Given a workspace with an update in flight
      And the run records that it is holding for the user to confirm the version, with a line saying so
      Then the app reports that update as waiting for the user at once
      And the app shows the run's own line about what it is waiting on

  @run-is-reported-where-the-reader-is
  Rule: A run in flight is reported both on the workspace's row and over the workspace itself
    A user watching their workspace update is not on the page its row is on.
    Both surfaces read one decision about the run, so they cannot describe it differently.

    @prepare-and-apply-are-named-apart
    Example: The part of a run that changes nothing is not reported as changing something
      Only the apply step changes the workspace, and it is also the step that takes its services away.
      Given a workspace with an update in flight that has not reached its apply step
      Then the app reports that update as preparing
      And the app reports that update over that workspace as preparing
      When that update reaches its apply step
      Then the app reports that workspace as applying its update
      And the app reports that update over that workspace as applying

    @run-outcome-reported-over-the-workspace
    Example: A run that ended with a note for the user says so over the workspace
      The update landed, so the note is reported as news rather than as a fault.
      Given a workspace whose last update ended with a note for the user
      Then the app reports that outcome over that workspace, not as a failure
      And the app offers no update surface that opens itself over that workspace
