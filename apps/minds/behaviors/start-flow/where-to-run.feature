Feature: Where to run the first workspace
  The first question after the reporting question, and how the user moves between its answers.
  Imbue Cloud is the answer the flow recommends; the others are offered as smaller links beneath it, the same answers the create form offers as smaller cards beneath its Imbue Cloud card.

  Background:
    Given the user is in the start flow
    And they have answered the reporting question

  @cloud-recommended
  Scenario: The question recommends Imbue Cloud and offers the other answers beneath it
    Then the flow recommends running the workspace on Imbue Cloud
    And it offers "Use Imbue Cloud" as the one button
    And it offers "Run it on this computer" and "Use your own cloud" as links beneath that button

  @form-answers-open-form
  Scenario Outline: A smaller answer opens the create form on that answer
    When the user answers "<answer>"
    Then the create form opens with "<card>" selected
    And no cloud create goes out

    Examples:
      | answer                  | card           |
      | Run it on this computer | This computer  |
      | Use your own cloud      | Your own cloud |

  @cloud-then-other
  Scenario: Changing a cloud answer to one of the smaller answers
    When the user answers "Use Imbue Cloud"
    And changes that answer to "Run it on this computer"
    Then the create form opens
    And no cloud create goes out

  @form-closed-offers-cloud
  Scenario: Closing the create form without submitting asks again, offering every answer
    When the user answers "Use your own cloud"
    And closes the create form without submitting it
    Then the flow asks again where to run the workspace, offering "Use Imbue Cloud", "Run it on this computer" and "Use your own cloud"

  @refused-cloud-asks-again
  Scenario: A refused cloud create says why and asks again
    Given the user has answered "Use Imbue Cloud" and settled the account step
    When the cloud create is refused
    Then the flow says that it did not work and why
    And asks again where to run the workspace
    And every earlier changeable answer can still be changed

  @existing-workspace-sign-in
  Scenario: Signing in to an existing account leaves the start flow for the home page
    When the user presses "I already have a workspace (log in)" and signs in
    Then they land on the home page
    And the start flow is not shown again
