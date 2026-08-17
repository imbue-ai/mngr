Feature: Message-delivery invariants

  @stays-ready
  Rule: Delivering a message always leaves the agent ready for the next one
    No message can leave the agent in a state where a subsequent message is silently refused.
    A client that has sent one message must be able to continue the conversation, so a message that quietly blocks the input channel desyncs the client from its agent.
