Feature: Where a new notification surfaces
  A new notification surfaces in exactly one way the user is expected to see.
  A focused main window shows it as a toast, and the banner is the fallback for a toast nobody can see.
  The user's delivery preference limits which surfaces may be used at all; these rules only choose between the allowed ones.

  @watched-shows-nothing
  Scenario: A message from a chat the user is watching arrives already read
    Given the user is watching a chat
    When that chat's agent notifies the user
    Then no toast and no banner appear
    And the bell and dock counts do not change
    And the feed lists the message as read

  @focused-main-window-toasts
  Scenario: With a main window in front, a toast stands in for the banner
    Given a main window has focus
    When a notification arrives that nobody is watching
    Then every main window shows it as a toast
    And no banner appears

  @unfocused-app-banners
  Scenario: With no main window in front, a banner appears
    Given no main window has focus
    When a notification arrives that nobody is watching
    Then a banner appears
    And every main window shows it as a toast for when the user returns

  @focused-pulled-out-window-banners
  Scenario: A pulled-out window in front does not hold back the banner
    Given a pulled-out window has focus
    And no main window has focus
    When a notification arrives that nobody is watching
    Then a banner appears
    And the pulled-out window shows no toast

  @os-only-preference-banners
  Scenario: A user who allows only banners gets one even with a main window in front
    Given the user's delivery preference allows banners and not toasts
    And a main window has focus
    When a notification arrives that nobody is watching
    Then a banner appears
    And no toast appears

  @locked-screen-overrides-watched
  Scenario: A locked screen gets the banner even for a watched chat
    Given the user is watching a chat
    And the operating system reports the screen locked
    When that chat's agent notifies the user
    Then a banner appears
    And the feed lists the message as unread

  @other-workspace-toast-named
  Scenario: A toast about another workspace names that workspace
    Given a main window shows one workspace
    When a notification about a different workspace flashes there as a toast
    Then the toast names that workspace and wears its accent

  @reading-a-chat-resolves-its-messages
  Scenario: Starting to watch a chat reads its messages and takes down its banners
    Given a chat whose agent's messages are unread
    And banners for those messages are still showing
    When the user starts watching that chat
    Then the feed lists those messages as read
    And those banners close
    And permission requests from the same workspace stay unread

  @showing-a-workspace-reads-nothing
  Scenario: Showing a workspace with no chat being watched reads nothing
    Given a workspace whose agents' messages are unread
    When a window shows that workspace without any of its chats being watched
    Then the feed still lists those messages as unread

  @read-messages-stay-as-receipts
  Rule: A read message stays in the feed as a receipt until cleared
    Reading never removes a message from the feed; read messages sort below everything unread and are the first to go when the feed is full.
