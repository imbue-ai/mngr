Feature: Leaving the start flow and coming back
  The conversation is not kept: whenever the start flow is shown afresh it starts over, while signed-in accounts, verified emails and the reporting answer persist.

  @fresh-conversation
  Scenario: The start flow shown afresh starts the conversation over
    Given the user had answered some of the start flow's questions
    When the start flow is shown afresh
    Then the conversation starts again from the beginning

  @returning-signed-in
  Scenario: Returning with an unverified account already signed in is not a dead end
    Given the account "a@example.com" was created in an earlier visit and its email was never verified
    When the start flow is shown afresh and the user answers "On Imbue Cloud"
    Then the account step names "a@example.com" and offers to use a different account
    And the user can create the account "b@example.com" and have the cloud create run under it
