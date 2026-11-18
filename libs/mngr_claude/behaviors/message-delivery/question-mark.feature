Feature: Messages that begin with a question mark
  A message a client delivers may begin with `?`, the key Claude Code binds to its shortcuts panel.
  Such a message is conversational input like any other: it contributes a turn carrying its full text.

  Background:
    Given a running Claude agent

  @lone-question-mark
  Scenario: A lone question mark is delivered as a message
    When the message "?" is delivered
    Then the conversation gains one turn reading "?"
    And the agent is ready for the next message

  @leading-question-mark
  Scenario: A message starting with a question mark is delivered whole
    When a message consisting of "?" followed by further text is delivered
    Then the conversation gains one turn carrying that message's full text
