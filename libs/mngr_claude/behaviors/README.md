# Claude agent behavior corpus

Understanding this behavior corpus calls for the tmr-behaviors skill; consult it when reading this file.

This corpus specifies the externally observable behavior of a *Claude agent*: a Claude Code process that mngr creates and drives.
A client interacts with a Claude agent by delivering messages to it and observing the resulting conversation and whether the agent is ready for more input.
This corpus specifies what that client observes; it never specifies how mngr drives the agent's terminal.

## Corpus-wide terms

A *Claude agent* is a single mngr-managed Claude Code session, addressed as one agent.
A *message* is one unit of input a client delivers to a Claude agent.
The agent is *ready* exactly when it will accept and act on the next delivered message.

## Out of scope for the whole corpus

Anything a client cannot determine by delivering messages and reading the resulting conversation and readiness is out of scope for the whole corpus.
