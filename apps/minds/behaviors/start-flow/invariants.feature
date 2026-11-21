Feature: Start-flow invariants
  These properties hold across every sequence of actions on the sign-in page, including sequences no scenario in this area describes: sign-ins closed or finishing late, emails verified at any moment, creates refused, and the page shown afresh.

  @never-stranded
  Rule: The user is never stranded
    From every state the page can reach, a workspace create is reachable through actions available to the user.
    Rationale: a first-run user who cannot move forward has no other path into the product.

  @create-under-landed-account
  Rule: A first cloud workspace runs under the landed account
    An account that was already signed in when the sign-in was started never becomes the landed account unless the sign-in itself reports it.
    Rationale: a user who signs in as one account must not end up with a workspace on another.

  @verify-landed-account
  Rule: Only the landed account's email is asked to be verified
    Whenever the page asks for an email address to be verified, it is the email of the landed account.
