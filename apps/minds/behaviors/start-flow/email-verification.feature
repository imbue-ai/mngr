Feature: Email verification before the first cloud workspace
  The connector refuses a cloud create for an unverified email, so the page checks first and waits on the emailed link rather than letting the create fail.

  Background:
    Given the account "alice@example.com" has landed with no workspaces

  @asks-to-verify
  Scenario: An unverified email holds the create behind a request to verify it
    Given the email of "alice@example.com" is not verified
    Then the page asks the user to open the link sent to "alice@example.com"
    And no workspace is created yet

  @verified-elsewhere
  Scenario: Verifying from the emailed link continues without any press
    Given the page is waiting for "alice@example.com" to be verified
    When the user opens the emailed link in the browser
    Then the app comes to the front and the first cloud workspace is created under "alice@example.com"

  @verified-at-sign-in
  Scenario: An email the sign-in reported verified creates at once
    Given the browser sign-in reported "alice@example.com" as verified
    Then the first cloud workspace is created without asking to verify it

  @resend-outcome
  Scenario Outline: Asking for the email again says what became of the request
    Given the page is waiting for "alice@example.com" to be verified
    When the user asks for the email again and the app <outcome>
    Then the page says "<note>"

    Examples:
      | outcome                          | note                                                                                  |
      | sends a new email                | Sent another email to alice@example.com.                                              |
      | finds one was sent moments ago   | An email went out to alice@example.com moments ago. Check your inbox and spam folder. |
      | fails                            | Could not send the email to alice@example.com. Please try again.                      |
      | finds it already verified        | alice@example.com is already verified.                                                |
