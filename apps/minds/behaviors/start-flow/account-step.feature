Feature: The account step
  The question that settles which account a cloud workspace runs under, and how the user moves between accounts.

  Background:
    Given the user is in the start flow
    And they have answered that the workspace should run on Imbue Cloud

  @signed-out
  Scenario: Signed out, the account step offers to create an account or sign in
    Given no account is signed in
    Then the account step offers "Create an account" and "Sign in"

  @signed-in-offers-continue
  Scenario: Signed in, the account step names the account and offers to keep it or use another
    Given the account "alice@example.com" is signed in
    Then the account step says the user is signed in as "alice@example.com", with the email in bold
    And it offers "Continue" and "Use a different account"
    And no create goes out until one of them is answered

  @continue
  Scenario: Continuing settles on the signed-in account
    Given the account "alice@example.com" is signed in
    When the user presses "Continue"
    Then the answer reads "Continue as alice@example.com"
    And the cloud create runs under "alice@example.com"

  @unverified-then-new-account
  Scenario: A second account created after abandoning an unverified first one is the one used
    Given no account is signed in
    When the user creates the account "a@example.com" and does not verify its email
    Then the flow asks for "a@example.com" to be verified
    When the user changes their answer to the account step
    And creates the account "b@example.com"
    Then the flow does not settle until "b@example.com" has signed in
    And it then checks the verification of "b@example.com", not "a@example.com"
    And the cloud create runs under "b@example.com"

  @switch-to-existing-account
  Scenario: Signing in to an existing account from the account step settles on it
    Given the account "alice@example.com" is signed in
    And the user also owns the account "bob@example.com"
    When the user presses "Use a different account" and signs in as "bob@example.com"
    Then the cloud create runs under "bob@example.com"

  @switch-to-new-account
  Scenario: Creating an account from the account step's switch settles on it
    Given the account "alice@example.com" is signed in
    When the user presses "Use a different account" and creates the account "bob@example.com" in the browser
    Then the agent acknowledges the account without claiming it was a sign-in or a sign-up
    And the cloud create runs under "bob@example.com"

  @closed-sign-in-still-counts
  Scenario: A sign-in finished after its dialog was closed still answers the question
    Given no account is signed in
    When the user presses "Create an account" and closes the sign-in dialog
    And then finishes creating the account in the browser
    Then the account step is answered with that account

  @closed-sign-in-replaced
  Scenario: A sign-in finished after a different answer was given answers nothing
    Given the account "alice@example.com" is signed in
    When the user presses "Use a different account" and closes the sign-in dialog
    And presses "Continue"
    And then finishes creating the account "bob@example.com" in the browser
    Then exactly one cloud create goes out, under "alice@example.com"

  @expired-sign-in
  Scenario: An expired sign-in can be started again
    Given no account is signed in
    When the user presses "Sign in" and lets the sign-in expire
    Then the sign-in dialog says it expired and offers to try again
    And trying again starts a new sign-in that can still settle the account step
