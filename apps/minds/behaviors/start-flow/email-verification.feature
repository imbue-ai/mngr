Feature: Email verification before a cloud create
  A cloud workspace needs a verified email; the flow waits for it instead of sending a create that would be refused.

  Background:
    Given the user is in the start flow
    And the account step has settled on "alice@example.com"
    And the email of "alice@example.com" is not verified

  @asks-to-verify
  Scenario: An unverified email holds the create behind a verification question
    Then the flow asks for "alice@example.com" to be verified
    And offers "I verified it" and "Send the email again"
    And no cloud create goes out

  @verified-elsewhere
  Scenario: Verifying from the emailed link continues the flow without any press
    When the user opens the verification link from the email, in any browser
    Then the flow notices within a few seconds and the cloud create goes out

  @pressed-too-early
  Scenario: Pressing "I verified it" before verifying asks again
    When the user presses "I verified it" without having opened the link
    Then the flow says the email is not verified yet and asks again
    When the user then opens the link and presses "I verified it"
    Then the cloud create goes out

  @resend-outcome
  Scenario Outline: Asking for the email again says what became of the request
    When the user presses "Send the email again" and the request <outcome>
    Then the flow says "<message>"

    Examples:
      | outcome                          | message                                                                               |
      | sends a new email                | Sent another email to alice@example.com.                                              |
      | finds one was sent moments ago   | An email went out to alice@example.com moments ago. Check your inbox and spam folder. |
      | fails                            | Could not send the email to alice@example.com. Please try again.                      |

  @abandon-unverified
  Scenario: The user can leave an unverified account behind
    When the user changes their answer to the account step
    Then the verification question is gone
    And the account step offers a different account
