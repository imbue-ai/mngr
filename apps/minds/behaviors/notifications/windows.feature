Feature: One main window per workspace
  A workspace has at most one main window, so a notification, a click, or a restored session always has one obvious window to land in.
  Main windows showing no workspace, and pulled-out windows, are not limited.

  @one-main-window-per-workspace
  Rule: No action puts a second main window on a workspace
    Every path onto a workspace that already has a main window raises that window instead.

    @open-in-new-window-raises-existing
    Scenario: Opening a workspace in a new window raises its existing window
      Given a workspace shown in a main window
      When the user opens that workspace in a new window
      Then that main window comes to the front
      And no new window opens

    @navigating-to-a-shown-workspace-raises-it
    Scenario: Navigating a window to a workspace another window shows raises that one
      Given one main window shows a workspace
      And another main window shows a different workspace
      When the user navigates the second window to the first window's workspace
      Then the first window comes to the front showing that workspace
      And the second window still shows its own workspace

    @notification-click-lands-in-its-workspace-window
    Scenario: Clicking a notification lands in its workspace's window
      Given a workspace shown in a main window
      When the user clicks a notification about that workspace
      Then that main window comes to the front showing what the notification is about

    @notification-click-opens-a-window
    Scenario: Clicking a notification for a workspace with no window opens one
      Given a main window shows one workspace
      And a different workspace has no main window
      When the user clicks a banner about the different workspace
      Then a new main window opens showing what the banner is about
      And the first window still shows its own workspace

    @history-step-onto-a-held-workspace
    Scenario: Stepping back onto a workspace another window now holds returns the window
      Given a main window that showed a workspace and moved on to another
      And a second main window that now shows the first workspace
      When the user goes back in the first window
      Then the second window comes to the front
      And the first window returns to where it was

    @restore-collapses-duplicates
    Scenario: A restored session opens one main window per workspace
      Given a saved session with two main windows on the same workspace
      When the app restores the session
      Then one main window opens for that workspace, where the most recently used of the two was
