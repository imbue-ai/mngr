Feature: The apply step's outage
  The apply step's outage is indistinguishable from a wedged workspace by probing alone.
  The app stands back for that step only, and for a bounded time, so a workspace that really did wedge is still recovered.
  Once it stops standing back, failure accounting restarts from nothing.

  @apply-outage-is-expected
  Scenario: A workspace mid-apply is not diagnosed as broken
    Given a workspace whose update has reached its apply step
    When that workspace stops answering
    Then the app does not report that workspace as stuck
    And the app does not restart that workspace
    And the app reports that workspace as applying its update
    And the app reports the apply over that workspace rather than a lost connection to it

  @prepare-outage-is-real
  Scenario: A workspace that dies before the apply step is a normal outage
    Given a workspace with an update in flight that has not reached its apply step
    When that workspace stops answering
    Then the app reports that workspace as stuck
    And the app restarts that workspace

  @record-settles-the-race
  Scenario: A workspace that stops answering before its apply was seen is asked directly
    Given a workspace with an update in flight
    And that workspace stops answering before the app has seen its apply begin
    When the app decides whether to restart it
    Then the app asks that workspace whether its apply is under way
    And the app does not restart a workspace that reports an apply under way
    And the app restarts a workspace that reports no apply under way
    And the app does not restart a workspace that cannot answer either way

  @wedged-apply-recovered
  Scenario: An apply that never finishes is recovered once the app stops standing back
    Given a workspace whose update has reached its apply step
    And that workspace never answers again
    When the app has stood back for as long as it will
    Then the app restarts that workspace
