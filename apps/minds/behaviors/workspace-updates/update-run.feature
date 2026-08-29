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

  @template-too-old
  Scenario: A workspace whose template cannot update itself is refused
    Given an out-of-date workspace whose template predates the update capability
    When the user starts an update for that workspace
    Then no update agent is started
    And the app explains that the workspace cannot update itself

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
    The update flow is unattended by design: it ends by reporting what changed and offering a rollback, whether or not anyone watched. So a run started by hand, from a schedule, or in bulk carries no consent tier of its own -- only the slash command, and the answers the app already collected at the button that an older workspace's flow would otherwise stop to ask for again.
    Given an out-of-date workspace with backups configured
    When an update is started for that workspace by any path
    Then the update agent's seed message is the update command alone

  @one-run-per-workspace
  Rule: A workspace runs at most one update at a time
    A second request to update a workspace that is already updating starts nothing.

  @run-liveness-is-observed
  Rule: Whether a run is still going is established by asking the workspace, never by elapsed time
    A run is treated as still going unless the workspace positively reports that it is not.

    @unreachable-workspace-stays-updating
    Example: A workspace that cannot be reached is still reported as updating
      Given a workspace with an update in flight
      And that workspace cannot be reached
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

    @hold-is-reported-with-its-detail
    Example: A run that has stopped for the user's decision says what it is waiting on
      A recorded hold is the run's own statement, so it is surfaced without waiting for repeated idleness; its one line of detail, written for the user, is what the app shows.
      Given a workspace with an update in flight
      And the run records that it is holding for the user, with a line naming what it cannot keep
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
