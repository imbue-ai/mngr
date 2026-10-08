Feature: Where to run the first workspace
  The first question after the reporting question, and how the user moves between its answers.

  Background:
    Given the user is in the start flow
    And they have answered the reporting question

  @cloud-then-custom
  Scenario: Changing a cloud answer to a custom setup
    When the user answers "On Imbue Cloud"
    And changes that answer to "Custom setup"
    Then the create form opens
    And no cloud create goes out

  @custom-closed-offers-cloud
  Scenario: Closing the create form without submitting asks again, offering both answers
    When the user answers "Custom setup"
    And closes the create form without submitting it
    Then the flow asks again where to run the workspace, offering "Custom setup" and "On Imbue Cloud"

  @refused-cloud-asks-again
  Scenario: A refused cloud create says why and asks again
    Given the user has answered "On Imbue Cloud" and settled the account step
    When the cloud create is refused
    Then the flow says that it did not work and why
    And asks again where to run the workspace
    And every earlier changeable answer can still be changed

  @existing-workspace-sign-in
  Scenario: Signing in to an existing account leaves the start flow for the home page
    When the user presses "I already have a workspace (log in)" and signs in
    Then they land on the home page
    And the start flow is not shown again
