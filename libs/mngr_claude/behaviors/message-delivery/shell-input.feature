Feature: Messages that begin with an exclamation mark
  A message a client delivers may begin with `!`.
  Such a message selects the agent's shell-command behavior instead of contributing a conversational turn.

  Background:
    Given a running Claude agent

  @runs-command
  Scenario: A bang followed by a command runs that command
    When the message "!echo mngr-behaviors-probe" is delivered
    Then "mngr-behaviors-probe" appears in the conversation as the command's output
    And the agent is ready for the next message

  @bare-bang-inert
  Scenario: A lone bang does nothing and leaves the agent ready
    When the message "!" is delivered
    Then no shell command runs
    And the conversation gains no new turn
    And the agent is ready for the next message

  @pending-shell-command-blocks
  Scenario: A shell command left unsubmitted blocks delivery by design
    A `!<command>` a client delivers is always submitted, so it never lingers unsent.
    The agent's input can therefore hold an unsubmitted shell command only when a human typed one directly into the pane and did not submit it.
    Blocking delivery in that state is intended: mngr leaves the command for the human to resolve rather than finish or discard it on their behalf, and it says so plainly instead of failing as a generic timeout.

    Given the agent's input holds an unsubmitted shell command
    When a client delivers a message
    Then the message is refused with an actionable error
    And delivery succeeds again once the command is submitted or cancelled
