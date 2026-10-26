Feature: Account window invariants
  Properties that hold for every window, whatever it shows and however it got there.

  @one-account-per-window
  Rule: A window shows one account's state and nothing of another's
    Everything a window lists, counts, or alerts about from the installation's state (e.g. (wlog) its workspaces, their permission requests, and the cloud providers on offer) concerns the window's account's workspaces and that account alone; the device's providers are offered in every window, and a home page with no workspaces of its own names the accounts that have some, with how many, only as places to switch the window to.
    Two windows for different accounts therefore show different lists of the same installation's state at the same moment.
    Rationale: a person who works under several accounts must be able to tell, from any window, which account a workspace they are about to create or act on belongs to.

    @device-window-lists-unassociated
    Example: The device's window lists the workspaces associated with no account
      Given the account "alice@example.com" has a workspace on this installation
      And a workspace associated with no account exists on this installation
      When a window for the device shows its home page
      Then the unassociated workspace is listed and Alice's is not

  @switch-moves-only-this-window
  Rule: Choosing an account in the switcher moves the window it was chosen in and no other
    Every other open window keeps its account and what it shows.
    Rationale: the switcher is a navigation of the window in hand; a change that reached across windows would take a workspace out from under the person using it there.
