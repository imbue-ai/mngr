Feature: Reports an agent opens
  An agent can ask to open a report any number of times, and its asks can arrive faster than the dialog opens.
  However many asks arrived, the user decides what happens to the report, and leaving the dialog returns them to the workspace the report is about.

  @agent-report-closes
  Scenario: A report an agent opened can be closed without sending it
    Given a workspace on screen whose agent has asked to open a report twice in quick succession
    And the report dialog shows the agent's latest description
    When the user closes the dialog with its close button
    Then the dialog is gone and the workspace the report is about is on screen
    And nothing was sent

  @done-returns-to-workspace
  Scenario: Leaving the confirmation of a sent agent report returns to its workspace
    Given an agent report about a workspace that the user has sent
    When the user leaves the confirmation with Done
    Then the workspace the report is about is on screen, whatever was on screen before the report opened
