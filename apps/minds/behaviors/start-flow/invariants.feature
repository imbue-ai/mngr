Feature: Start-flow invariants
  These properties hold across every sequence of actions in the start flow, including sequences no scenario in this area describes: answers given in any order, answers taken back, dialogs closed, sign-ins finishing late, emails verified at any moment, creates refused, and the flow shown afresh.

  @never-stranded
  Rule: The user is never stranded
    From every state the flow can reach, a workspace create is reachable through actions available to the user.
    In particular, a cloud workspace under an account the user has just created is always reachable, whatever accounts are already signed in and whether or not their emails are verified.
    Rationale: a first-run user who cannot move forward has no other path into the product.

  @create-under-settled-account
  Rule: A cloud create runs under the account the account step settled on
    The account a cloud create runs under is the account named by the most recent answer to the account step.
    An account that was already signed in when a sign-in was started never settles that sign-in unless the sign-in itself reports it.
    Rationale: a user who abandons one account for another must not end up with a workspace on the account they abandoned.

  @verify-settled-account
  Rule: Only the settled account's email is asked to be verified
    Whenever the flow asks for an email address to be verified, it is the email of the account the account step settled on.

  @one-create-per-answer
  Rule: An answer replaces whatever the flow was still waiting on
    Any answer, and any answer taken back, ends the flow's wait on a dialog or sign-in it started earlier.
    A sign-in that completes after its wait has ended answers nothing and sends no create.

  @change-answer-offered
  Rule: Every changeable answer can be changed until a create goes out
    Every changeable answer carries its "Change answer" control from the moment it is given until the flow sends a create.
    While a cloud create is being sent the controls are withheld, and they return if the create is refused or could not be sent.
