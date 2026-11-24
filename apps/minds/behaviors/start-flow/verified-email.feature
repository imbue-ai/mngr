Feature: A verified email before sign-in
  An unverified email is verified on the hosted account pages the browser sign-in opens, before the sign-in reaches the app.

  @signed-in-once-verified
  Rule: An account is signed in only once its email is verified
    No account whose email is unverified becomes the landed account, so no first cloud workspace is ever created under one.
    The app holds no verification wait of its own: nothing the page shows or does depends on, checks, or changes whether an email is verified.
    Rationale: verification is settled once, in the browser, so no step after sign-in has to ask for it.

    @unverified-sign-in-refused
    Example: A sign-in refused for an unverified email leaves the sign-in dialog offering to try again
      Given the user pressed "Sign in"
      When the sign-in is refused because the email of "alice@example.com" is not verified
      Then the sign-in dialog says "Your email isn't verified yet. Click the link we sent to alice@example.com, then sign in again."
      And the sign-in dialog offers "Try again"
      And no account lands and no workspace is created

    @try-again-after-refusal
    Example: Trying again starts the browser sign-in over
      Given the sign-in dialog says the email of "alice@example.com" is not verified
      When the user presses "Try again"
      Then the browser sign-in opens again
